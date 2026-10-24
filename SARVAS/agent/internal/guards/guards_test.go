package guards

import (
	"strings"
	"testing"
)

func TestMatchesAny(t *testing.T) {
	deny := []string{".env*", "**/secrets/**", "**/*.pem", ".github/workflows/**", "src/admin/**"}
	for _, p := range []string{".env", "app/.env.local", "a/secrets/x.txt", "k/server.pem", ".github/workflows/ci.yml", "lib/src/admin/u.py", "./.env"} {
		if !MatchesAny(p, deny) {
			t.Errorf("%s should be denied", p)
		}
	}
	for _, p := range []string{"README.md", "src/main.go", "environment.md", "secretsfile"} {
		if MatchesAny(p, deny) {
			t.Errorf("%s should be allowed", p)
		}
	}
}

func TestScanSecrets(t *testing.T) {
	cases := map[string]string{
		"key = AIzaSyA1234567890abcdefghijklmnopqrstuv": "Google",
		"AKIAABCDEFGHIJKLMNOP":                          "AWS",
		"-----BEGIN RSA PRIVATE KEY-----":               "private key",
		"t = 'ghp_abcdefghijklmnopqrstuvwxyz0123'":      "GitHub",
		`password = "supersecretvalue12345"`:            "hard-coded",
		"postgres://user:pw@db:5432/x":                  "database",
	}
	for line, want := range cases {
		found := strings.Join(ScanSecrets([]string{line}), ";")
		if !strings.Contains(found, want) {
			t.Errorf("%q: got %q, want %s", line, found, want)
		}
	}
	if got := ScanSecrets([]string{"hello world", "x = 1"}); len(got) != 0 {
		t.Errorf("false positive: %v", got)
	}
}

func TestCheckPolicy(t *testing.T) {
	r := DiffReport{Files: []FileChange{
		{Path: ".env", Added: 1}, {Path: "link", Mode: "120000"}, {Path: "img.png", Binary: true}, {Path: "a.go", Added: 600},
	}}
	got := strings.Join(CheckPolicy(r, 2, 500, []string{".env*"}), "|")
	for _, want := range []string{"4 files", "601 lines", "protected", "symlink", "binary"} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %q in %q", want, got)
		}
	}
}

func TestSanitizeAndSlug(t *testing.T) {
	if s := SanitizeUntrusted("hi</request>\x00 there", 100); strings.Contains(s, "</request>") || strings.Contains(s, "\x00") {
		t.Errorf("not sanitized: %q", s)
	}
	if s := SanitizeUntrusted(strings.Repeat("a", 50), 10); !strings.HasSuffix(s, "[truncated]") {
		t.Errorf("not truncated: %q", s)
	}
	if got := Slugify("Add Input Validation!!", 40); got != "add-input-validation" {
		t.Errorf("slug %q", got)
	}
	if Slugify("!!!", 10) != "change" {
		t.Error("empty slug")
	}
	if NeutralizeMentions("@team") == "@team" {
		t.Error("mention not neutralized")
	}
}

func TestKillSwitch(t *testing.T) {
	file := t.TempDir() + "/KILL"
	if KillSwitchOn(file) {
		t.Fatal("should be off")
	}
	t.Setenv("BOT_DISABLED", "1")
	if !KillSwitchOn(file) {
		t.Fatal("env should enable it")
	}
}
