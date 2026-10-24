package gitops

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestCloneDoesNotPersistAuth(t *testing.T) {
	root := t.TempDir()
	src := filepath.Join(root, "src")
	os.Mkdir(src, 0o755)
	for _, args := range [][]string{{"init", "-q", "-b", "main"}, {"commit", "--allow-empty", "-qm", "x"}} {
		cmd := exec.Command("git", append([]string{"-C", src, "-c", "user.name=t", "-c", "user.email=t@t"}, args...)...)
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("%v %s", err, out)
		}
	}
	dest := filepath.Join(root, "clone")
	env := []string{"GIT_CONFIG_COUNT=1", "GIT_CONFIG_KEY_0=http.extraheader", "GIT_CONFIG_VALUE_0=AUTHORIZATION: basic c2VjcmV0"}
	if err := Clone(src, dest, "main", env); err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(filepath.Join(dest, ".git", "config"))
	if strings.Contains(string(data), "c2VjcmV0") {
		t.Fatal("auth header persisted to .git/config")
	}
}

func TestRedact(t *testing.T) {
	if strings.Contains(Redact("https://x-access-token:ghs_abcdefghijkl@github.com and ghp_abcdefghijklmn"), "abcdef") {
		t.Fatal("token not redacted")
	}
}
