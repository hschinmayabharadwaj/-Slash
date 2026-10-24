// Package guards holds the deterministic guardrails. They run in plain code
// on the host, outside the model's control: the agent proposes, these decide.
package guards

import (
	"fmt"
	"os"
	"path"
	"regexp"
	"strings"
	"sync"
)

var (
	globMu    sync.Mutex
	globCache = map[string]*regexp.Regexp{}
)

// globRegex converts a glob (with ** support) to an anchored regexp.
func globRegex(pattern string) *regexp.Regexp {
	globMu.Lock()
	defer globMu.Unlock()
	if re, ok := globCache[pattern]; ok {
		return re
	}
	var b strings.Builder
	b.WriteString("^")
	for i := 0; i < len(pattern); i++ {
		c := pattern[i]
		switch {
		case strings.HasPrefix(pattern[i:], "**/"):
			b.WriteString("(?:.*/)?")
			i += 2
		case strings.HasPrefix(pattern[i:], "**"):
			b.WriteString(".*")
			i++
		case c == '*':
			b.WriteString("[^/]*")
		case c == '?':
			b.WriteString("[^/]")
		default:
			b.WriteString(regexp.QuoteMeta(string(c)))
		}
	}
	b.WriteString("$")
	re := regexp.MustCompile(b.String())
	globCache[pattern] = re
	return re
}

// MatchesAny reports whether path matches any deny pattern (case-insensitive).
// Patterns without "/" match the basename; patterns with "/" may match at any depth.
func MatchesAny(p string, patterns []string) bool {
	p = strings.ToLower(p)
	for strings.HasPrefix(p, "./") {
		p = p[2:]
	}
	for _, pat := range patterns {
		pl := strings.ToLower(strings.TrimSpace(pat))
		if pl == "" {
			continue
		}
		if !strings.Contains(pl, "/") {
			if ok, _ := path.Match(pl, path.Base(p)); ok {
				return true
			}
			continue
		}
		if globRegex(pl).MatchString(p) {
			return true
		}
		if !strings.HasPrefix(pl, "**/") && globRegex("**/"+pl).MatchString(p) {
			return true
		}
	}
	return false
}

// SecretPatterns name -> regexp. Matches report the type, never the secret.
var SecretPatterns = []struct {
	Name string
	Re   *regexp.Regexp
}{
	{"a Google/Gemini API key", regexp.MustCompile(`\bAIza[0-9A-Za-z\-_]{35}\b`)},
	{"an AWS access key", regexp.MustCompile(`\bAKIA[0-9A-Z]{16}\b`)},
	{"a private key", regexp.MustCompile(`-----BEGIN [A-Z ]*PRIVATE KEY-----`)},
	{"a Slack token", regexp.MustCompile(`\bxox[abprs]-[0-9A-Za-z-]{10,}\b`)},
	{"a GitHub token", regexp.MustCompile(`\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[0-9A-Za-z_]{20,}\b`)},
	{"an Anthropic API key", regexp.MustCompile(`\bsk-ant-[0-9A-Za-z_-]{20,}\b`)},
	{"a hard-coded secret", regexp.MustCompile(`(?i)\b(?:api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*['"][^'"\s]{16,}['"]`)},
	{"a database connection string", regexp.MustCompile(`\b(?:postgres|mysql|mongodb)://[^:\s]+:[^@\s]+@[^/\s]+`)},
}

// ScanSecrets returns human-readable findings for added lines.
func ScanSecrets(lines []string) []string {
	var found []string
	for _, sp := range SecretPatterns {
		for _, line := range lines {
			if sp.Re.MatchString(line) {
				found = append(found, "it appears to add "+sp.Name)
				break
			}
		}
	}
	return found
}

var (
	control = regexp.MustCompile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
	ourTags = regexp.MustCompile(`(?i)</?\s*(?:request|slack_thread|repo_conventions|plan|approved_plan|repository)\b[^>]*>`)
	slugRe  = regexp.MustCompile(`[^a-z0-9]+`)
)

// SanitizeUntrusted prepares user/repo text to be embedded as DATA in a prompt.
func SanitizeUntrusted(text string, maxChars int) string {
	text = control.ReplaceAllString(text, "")
	text = ourTags.ReplaceAllString(text, "[tag removed]")
	if r := []rune(text); len(r) > maxChars {
		text = string(r[:maxChars]) + "\n[truncated]"
	}
	return text
}

// NeutralizeMentions stops model-written PR text from @-mentioning people.
func NeutralizeMentions(text string) string {
	return strings.ReplaceAll(text, "@", "@​")
}

// Slugify makes a branch-safe slug.
func Slugify(text string, maxLen int) string {
	s := strings.Trim(slugRe.ReplaceAllString(strings.ToLower(text), "-"), "-")
	if len(s) > maxLen {
		s = strings.Trim(s[:maxLen], "-")
	}
	if s == "" {
		return "change"
	}
	return s
}

// Tail returns the last maxChars characters of text.
func Tail(text string, maxChars int) string {
	text = strings.TrimSpace(text)
	r := []rune(text)
	if len(r) <= maxChars {
		return text
	}
	return "…" + string(r[len(r)-maxChars:])
}

// KillSwitchOn is true if BOT_DISABLED is set or the kill-switch file exists.
func KillSwitchOn(file string) bool {
	switch strings.ToLower(os.Getenv("BOT_DISABLED")) {
	case "1", "true", "yes":
		return true
	}
	if file == "" {
		return false
	}
	_, err := os.Stat(file)
	return err == nil
}

// FileChange is one changed path in a diff.
type FileChange struct {
	Path    string
	Status  string
	Mode    string
	Added   int
	Deleted int
	Binary  bool
}

// DiffReport summarizes the staged changes.
type DiffReport struct {
	Files      []FileChange
	AddedLines []string
}

func (r DiffReport) TotalChanged() int {
	n := 0
	for _, f := range r.Files {
		n += f.Added + f.Deleted
	}
	return n
}

// CheckPolicy returns violations (empty = OK).
func CheckPolicy(r DiffReport, maxFiles, maxLines int, deny []string) []string {
	var problems []string
	if len(r.Files) > maxFiles {
		problems = append(problems, fmt.Sprintf("it changes %d files (limit is %d)", len(r.Files), maxFiles))
	}
	if t := r.TotalChanged(); t > maxLines {
		problems = append(problems, fmt.Sprintf("it changes %d lines (limit is %d)", t, maxLines))
	}
	var protected, special, binary []string
	for _, f := range r.Files {
		if MatchesAny(f.Path, deny) {
			protected = append(protected, "`"+f.Path+"`")
		}
		if f.Mode == "120000" || f.Mode == "160000" {
			special = append(special, f.Path)
		}
		if f.Binary {
			binary = append(binary, f.Path)
		}
	}
	if len(protected) > 0 {
		if len(protected) > 5 {
			protected = protected[:5]
		}
		problems = append(problems, "it touches protected paths: "+strings.Join(protected, ", "))
	}
	if len(special) > 0 {
		problems = append(problems, fmt.Sprintf("it adds a symlink or submodule: `%s`", special[0]))
	}
	if len(binary) > 0 {
		problems = append(problems, fmt.Sprintf("it includes a binary file: `%s`", binary[0]))
	}
	return problems
}
