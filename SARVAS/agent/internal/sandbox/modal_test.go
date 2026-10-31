package sandbox

import (
	"bytes"
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"sarvas/internal/config"
)

type fakeModalConn struct {
	policy     modalNetPolicy
	env        map[string]string
	lifetime   time.Duration
	terminated int
	cmds       [][]string
	execErr    error
	outputJSON string
	patch      string
}

func (f *fakeModalConn) Exec(_ context.Context, cmd []string, env map[string]string, _ string, _ time.Duration) (int, string, string, error) {
	f.cmds = append(f.cmds, cmd)
	if f.execErr != nil {
		return 0, "", "", f.execErr
	}
	return 0, "", "", nil
}

func (f *fakeModalConn) CopyFromLocal(_ context.Context, _, _ string) error { return nil }

func (f *fakeModalConn) CopyToLocal(_ context.Context, remotePath, localPath string) error {
	switch remotePath {
	case modalIODir + "/output.json":
		return os.WriteFile(localPath, []byte(f.outputJSON), 0o600)
	case modalPatch:
		return os.WriteFile(localPath, []byte(f.patch), 0o600)
	}
	return os.WriteFile(localPath, []byte{}, 0o600)
}

func (f *fakeModalConn) Terminate(_ context.Context) error {
	f.terminated++
	return nil
}

func testModal(mutate func(*Modal)) (*Modal, *fakeModalConn) {
	fc := &fakeModalConn{outputJSON: `{"ok":true}`}
	m := NewModal(config.Default())
	m.Dial = func(_ context.Context, pol modalNetPolicy, env map[string]string, lifetime time.Duration) (modalConn, error) {
		fc.policy, fc.env, fc.lifetime = pol, env, lifetime
		return fc, nil
	}
	m.LookupImage = func(context.Context) error { return nil }
	if mutate != nil {
		mutate(m)
	}
	return m, fc
}

func writeFile(t *testing.T, path, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestModalTarSkipsGitAndRoundTrips(t *testing.T) {
	repo := t.TempDir()
	writeFile(t, filepath.Join(repo, "a.txt"), "one\n")
	writeFile(t, filepath.Join(repo, "sub", "b.txt"), "two\n")
	writeFile(t, filepath.Join(repo, ".git", "HEAD"), "ref: refs/heads/main\n")
	writeFile(t, filepath.Join(repo, ".git", "objects", "x"), "history\n")
	var buf bytes.Buffer
	if err := tarRepo(repo, &buf); err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(buf.Bytes(), []byte("history\n")) {
		t.Fatal("bundle contains .git history")
	}
	dest := t.TempDir()
	if err := untarTo(&buf, dest); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dest, ".git")); !os.IsNotExist(err) {
		t.Fatal(".git crossed the tar boundary")
	}
	for _, tc := range [][2]string{{"a.txt", "one\n"}, {"sub/b.txt", "two\n"}} {
		got, err := os.ReadFile(filepath.Join(dest, tc[0]))
		if err != nil || string(got) != tc[1] {
			t.Fatalf("%s: %q %v", tc[0], got, err)
		}
	}
}

func git(t *testing.T, dir string, args ...string) {
	t.Helper()
	cmd := exec.Command("git", append([]string{"-C", dir}, args...)...)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("git %v: %v %s", args, err, out)
	}
}

func TestModalApplyPatchRoundTrip(t *testing.T) {
	repo := t.TempDir()
	git(t, repo, "init", "-q", "-b", "main")
	writeFile(t, filepath.Join(repo, "a.txt"), "one\n")
	git(t, repo, "add", "-A")
	git(t, repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
	patch := filepath.Join(t.TempDir(), "c.patch")
	writeFile(t, patch, "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+two\n")
	if err := applyPatch(repo, patch); err != nil {
		t.Fatal(err)
	}
	got, _ := os.ReadFile(filepath.Join(repo, "a.txt"))
	if string(got) != "two\n" {
		t.Fatalf("patch not applied: %q", got)
	}
	empty := filepath.Join(t.TempDir(), "e.patch")
	writeFile(t, empty, "\n")
	if err := applyPatch(repo, empty); err != nil {
		t.Fatalf("empty patch must be a no-op: %v", err)
	}
	evil := filepath.Join(t.TempDir(), "evil.patch")
	writeFile(t, evil, "diff --git a/.git/config b/.git/config\n--- a/.git/config\n+++ b/.git/config\n@@ -0,0 +1 @@\n+evil\n")
	if err := applyPatch(repo, evil); err == nil {
		t.Fatal("patch touching .git must be refused")
	}
}

func TestModalChecksBlockNetworkAndTerminate(t *testing.T) {
	m, fc := testModal(nil)
	res, err := m.RunChecks(t.TempDir(), "go test ./...", time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if !fc.policy.BlockNetwork || len(fc.policy.AllowDomains) != 0 {
		t.Fatalf("checks must be fully offline: %+v", fc.policy)
	}
	last := fc.cmds[len(fc.cmds)-1]
	if len(last) != 3 || last[0] != "sh" || last[1] != "-c" || last[2] != "go test ./..." {
		t.Fatalf("checks must run the trusted command via sh -c: %v", fc.cmds)
	}
	if res.ExitCode != 0 {
		t.Fatalf("fake exit: %+v", res)
	}
	if fc.terminated != 1 {
		t.Fatalf("sandbox must be terminated, got %d", fc.terminated)
	}
}

func TestModalTerminateOnError(t *testing.T) {
	m, fc := testModal(func(m *Modal) {
		m.TokenID, m.TokenSecret = "tid-secret", "ts-secret"
	})
	fc.execErr = errors.New("boom")
	_, err := m.RunChecks(t.TempDir(), "go test ./...", time.Second)
	if err == nil {
		t.Fatal("expected error")
	}
	if fc.terminated != 1 {
		t.Fatal("sandbox must be terminated on error")
	}
	for _, secret := range []string{"tid-secret", "ts-secret"} {
		if strings.Contains(err.Error(), secret) {
			t.Fatalf("secret leaked in error: %v", err)
		}
	}
}

func TestModalSecretsNeverInArgv(t *testing.T) {
	const apiKey, tid, ts = "AIza-SECRET-KEY", "tid-SECRET", "ts-SECRET"
	m, fc := testModal(func(m *Modal) {
		m.TokenID, m.TokenSecret = tid, ts
	})
	ioDir := t.TempDir()
	writeFile(t, filepath.Join(ioDir, "input.json"), `{}`)
	if _, err := m.RunAgent("plan", t.TempDir(), ioDir, time.Second, apiKey); err != nil {
		t.Fatal(err)
	}
	for _, cmd := range fc.cmds {
		for _, arg := range cmd {
			for _, secret := range []string{apiKey, tid, ts} {
				if strings.Contains(arg, secret) {
					t.Fatalf("secret in argv %v", cmd)
				}
			}
		}
	}
	if fc.env["AI_API_KEY"] != apiKey {
		t.Fatalf("API key must travel via env: %v", fc.env)
	}
	for k := range fc.env {
		if strings.Contains(k, "MODAL") || strings.Contains(k, "TOKEN") {
			t.Fatalf("modal token must never enter a sandbox: %v", fc.env)
		}
	}
	if _, err := os.Stat(filepath.Join(ioDir, "output.json")); err != nil {
		t.Fatalf("output.json must come back: %v", err)
	}
	if fc.terminated != 1 {
		t.Fatal("sandbox must be terminated even on success")
	}
	// Error paths must not echo secrets either.
	fc.execErr = errors.New("boom")
	_, err := m.RunAgent("plan", t.TempDir(), ioDir, time.Second, apiKey)
	if err == nil {
		t.Fatal("expected error")
	}
	for _, secret := range []string{apiKey, tid, ts} {
		if strings.Contains(err.Error(), secret) {
			t.Fatalf("secret in error: %v", err)
		}
	}
	if fc.terminated != 2 {
		t.Fatal("sandbox must be terminated on error too")
	}
}

func TestModalNetPolicy(t *testing.T) {
	open := modalNetPolicy{}
	for _, tc := range []struct {
		provider, baseURL string
		want              modalNetPolicy
	}{
		{"sim", "", modalNetPolicy{BlockNetwork: true}},
		{"openai", "https://inference.baseten.co/v1", modalNetPolicy{AllowDomains: []string{"inference.baseten.co"}}},
		{"ollama", "http://127.0.0.1:11434", modalNetPolicy{BlockNetwork: true}},
		{"gemini", "", open},
	} {
		m := NewModal(config.Default())
		m.Provider, m.BaseURL = tc.provider, tc.baseURL
		got := m.agentNetPolicy()
		if got.BlockNetwork != tc.want.BlockNetwork || len(got.AllowDomains) != len(tc.want.AllowDomains) ||
			(len(got.AllowDomains) > 0 && got.AllowDomains[0] != tc.want.AllowDomains[0]) {
			t.Errorf("%s %s: got %+v want %+v", tc.provider, tc.baseURL, got, tc.want)
		}
	}
}

func TestModalResourceMapping(t *testing.T) {
	if req, lim := parseModalMemory("1g"); req != 1024 || lim != 1024 {
		t.Fatalf("1g: %d %d", req, lim)
	}
	if req, lim := parseModalMemory("512m"); req != 512 || lim != 512 {
		t.Fatalf("512m: %d %d", req, lim)
	}
	if req, lim := parseModalMemory("bogus"); req != 0 || lim != 0 {
		t.Fatalf("bogus must fall back to Modal defaults: %d %d", req, lim)
	}
	if req, lim := parseModalCPU("1.0"); req != 1.0 || lim != 1.0 {
		t.Fatalf("cpu: %v %v", req, lim)
	}
	if req, lim := parseModalCPU("nope"); req != 0 || lim != 0 {
		t.Fatalf("bad cpu must fall back to Modal defaults: %v %v", req, lim)
	}
}

func TestModalPreflight(t *testing.T) {
	m, _ := testModal(nil)
	m.TokenID, m.TokenSecret = "", ""
	if problems := m.Preflight(); len(problems) == 0 {
		t.Fatal("missing tokens must be reported")
	}
	m, _ = testModal(func(m *Modal) {
		m.TokenID, m.TokenSecret = "tid", "ts"
		m.LookupImage = func(context.Context) error { return errors.New("not found") }
	})
	problems := m.Preflight()
	if len(problems) == 0 || !strings.Contains(problems[0], m.imageRef()) {
		t.Fatalf("missing image must name the image: %v", problems)
	}
	for _, p := range problems {
		if strings.Contains(p, "tid") || strings.Contains(p, "ts") {
			t.Fatalf("token in preflight output: %v", problems)
		}
	}
	m, _ = testModal(func(m *Modal) {
		m.TokenID, m.TokenSecret = "tid", "ts"
	})
	if problems := m.Preflight(); len(problems) != 0 {
		t.Fatalf("ready backend must be clean: %v", problems)
	}
}

func TestNewRunnerSelection(t *testing.T) {
	if _, ok := NewRunner(config.Default()).(*Docker); !ok {
		t.Fatal("default backend must stay Docker")
	}
	cfg := config.Default()
	cfg.Sandbox.Backend = "modal"
	if _, ok := NewRunner(cfg).(*Modal); !ok {
		t.Fatal("modal backend must return *Modal")
	}
	if NewPreflight(cfg) == nil {
		t.Fatal("modal preflight must exist")
	}
}

func TestModalUnknownMode(t *testing.T) {
	m, fc := testModal(nil)
	if _, err := m.RunAgent("rm", ".", ".", time.Second, ""); err == nil {
		t.Fatal("expected error")
	}
	if fc.terminated != 0 {
		t.Fatal("no sandbox may start for an unknown mode")
	}
}
