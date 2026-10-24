// Package gitops runs git with hooks disabled, a protocol allowlist,
// timeouts, and credentials passed only through the child's environment.
package gitops

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"time"

	"sarvas/internal/guards"
)

var safeFlags = []string{
	"-c", "core.hooksPath=/dev/null",
	"-c", "protocol.allow=never",
	"-c", "protocol.https.allow=always",
	"-c", "protocol.file.allow=always",
}

var (
	tokenInURL = regexp.MustCompile(`(x-access-token:)[^@\s]+`)
	ghToken    = regexp.MustCompile(`\bgh[spousr]_[A-Za-z0-9_]{10,}`)
)

// Redact removes anything that looks like a token from git output.
func Redact(s string) string {
	return ghToken.ReplaceAllString(tokenInURL.ReplaceAllString(s, "${1}***"), "gh*_***")
}

// Run executes git in dir. env entries ("K=V") are added to the environment.
func Run(dir string, env []string, stdin string, timeout time.Duration, args ...string) (string, error) {
	if timeout == 0 {
		timeout = 60 * time.Second
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, "git", append(append([]string{}, safeFlags...), args...)...)
	cmd.Dir = dir
	cmd.Env = append(append(os.Environ(), "GIT_TERMINAL_PROMPT=0"), env...)
	if stdin != "" {
		cmd.Stdin = strings.NewReader(stdin)
	}
	var out, errb bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &errb
	if err := cmd.Run(); err != nil {
		if ctx.Err() != nil {
			return "", fmt.Errorf("git %s timed out", args[0])
		}
		msg := Redact(strings.TrimSpace(errb.String()))
		if len(msg) > 500 {
			msg = msg[:500]
		}
		return "", fmt.Errorf("git %s failed: %s", args[0], msg)
	}
	return out.String(), nil
}

// Clone makes a shallow clone without submodules or LFS smudging.
func Clone(url, dest, branch string, env []string) error {
	args := []string{"clone", "--depth", "1", "--no-recurse-submodules"}
	if branch != "" {
		args = append(args, "--branch", branch)
	}
	_, err := Run("", append(env, "GIT_LFS_SKIP_SMUDGE=1"), "", 3*time.Minute, append(args, url, dest)...)
	return err
}

func CreateBranch(dir, branch string) error {
	if _, err := Run(dir, nil, "", 0, "check-ref-format", "--branch", branch); err != nil {
		return err
	}
	_, err := Run(dir, nil, "", 0, "checkout", "-q", "-b", branch)
	return err
}

// ApplyPatch applies a patch to index and worktree (must apply cleanly).
func ApplyPatch(dir, patch string) error {
	_, err := Run(dir, nil, patch, 0, "apply", "--index", "--whitespace=nowarn", "-")
	return err
}

func Commit(dir, message, authorName, authorEmail string) error {
	if _, err := Run(dir, nil, "", 0, "add", "-A"); err != nil {
		return err
	}
	_, err := Run(dir, nil, "", 0, "-c", "user.name="+authorName, "-c", "user.email="+authorEmail,
		"commit", "-q", "--no-verify", "-m", message)
	return err
}

// Push pushes HEAD to a new branch on origin (never force).
func Push(dir, branch string, env []string) error {
	_, err := Run(dir, env, "", 2*time.Minute, "push", "--no-verify", "origin", "HEAD:refs/heads/"+branch)
	return err
}

// StagedDiff returns the binary-safe diff of the index.
func StagedDiff(dir string) (string, error) {
	return Run(dir, nil, "", 0, "diff", "--cached", "--no-color", "--no-ext-diff", "--no-textconv", "--binary")
}

func CurrentBranch(dir string) (string, error) {
	out, err := Run(dir, nil, "", 0, "rev-parse", "--abbrev-ref", "HEAD")
	return strings.TrimSpace(out), err
}

// InspectWorktree stages everything and summarizes it for the policy checks.
func InspectWorktree(dir string) (guards.DiffReport, error) {
	var report guards.DiffReport
	if _, err := Run(dir, nil, "", 0, "add", "-A"); err != nil {
		return report, err
	}
	common := []string{"diff", "--cached", "--no-renames", "--no-ext-diff", "--no-textconv"}
	raw, err := Run(dir, nil, "", 0, append(common, "--raw", "-z")...)
	if err != nil {
		return report, err
	}
	stat, err := Run(dir, nil, "", 0, append(common, "--numstat", "-z")...)
	if err != nil {
		return report, err
	}
	patch, err := Run(dir, nil, "", 0, append(common, "-U0", "--no-color")...)
	if err != nil {
		return report, err
	}

	files := map[string]*guards.FileChange{}
	var order []string
	tokens := strings.Split(raw, "\x00")
	for i := 0; i < len(tokens); i++ {
		if strings.HasPrefix(tokens[i], ":") && i+1 < len(tokens) {
			meta := strings.Fields(tokens[i][1:]) // oldmode newmode oldsha newsha status
			p := tokens[i+1]
			if len(meta) >= 5 {
				files[p] = &guards.FileChange{Path: p, Status: meta[4][:1], Mode: meta[1]}
				order = append(order, p)
			}
			i++
		}
	}
	for _, entry := range strings.Split(stat, "\x00") {
		parts := strings.SplitN(entry, "\t", 3)
		if len(parts) != 3 {
			continue
		}
		fc, ok := files[parts[2]]
		if !ok {
			fc = &guards.FileChange{Path: parts[2]}
			files[parts[2]] = fc
			order = append(order, parts[2])
		}
		if parts[0] == "-" || parts[1] == "-" {
			fc.Binary = true
		} else {
			fc.Added, _ = strconv.Atoi(parts[0])
			fc.Deleted, _ = strconv.Atoi(parts[1])
		}
	}
	for _, p := range order {
		report.Files = append(report.Files, *files[p])
	}
	if len(patch) > 2_000_000 {
		patch = patch[:2_000_000]
	}
	for _, line := range strings.Split(patch, "\n") {
		if strings.HasPrefix(line, "+") && !strings.HasPrefix(line, "+++") {
			report.AddedLines = append(report.AddedLines, line[1:])
		}
	}
	return report, nil
}
