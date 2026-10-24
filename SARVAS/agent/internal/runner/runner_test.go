package runner

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func workspace(t *testing.T) string {
	root := filepath.Join(t.TempDir(), "ws")
	os.MkdirAll(filepath.Join(root, ".git"), 0o755)
	os.WriteFile(filepath.Join(root, "README.md"), []byte("# Hi\n"), 0o644)
	os.WriteFile(filepath.Join(root, ".git", "config"), []byte("[core]\n"), 0o644)
	os.WriteFile(filepath.Join(root, ".env"), []byte("SECRET=1\n"), 0o644)
	return root
}

func TestSimPlanAndImplement(t *testing.T) {
	root := workspace(t)
	in := Input{Request: "say hello", Provider: "sim", DenyPatterns: []string{".env*"}, MaxTurns: 5}
	in.Mode = "plan"
	plan := Run(context.Background(), in, root, nil)
	if !plan.OK || plan.Plan["feasible"] != true || !plan.Sim {
		t.Fatalf("plan: %+v", plan)
	}
	if _, err := os.Stat(filepath.Join(root, "AGENT_SIM.md")); err == nil {
		t.Fatal("plan mode wrote a file")
	}
	in.Mode, in.Plan = "implement", plan.Plan
	impl := Run(context.Background(), in, root, nil)
	if !impl.OK || len(impl.ChangedFiles) != 1 || impl.ChangedFiles[0] != "AGENT_SIM.md" {
		t.Fatalf("impl: %+v", impl)
	}
	data, _ := os.ReadFile(filepath.Join(root, "AGENT_SIM.md"))
	if !strings.Contains(string(data), "say hello") {
		t.Fatal("missing request in sim output")
	}
}

func TestWorkspaceRefusesEscapes(t *testing.T) {
	root := workspace(t)
	ws := &Workspace{Root: root, Deny: []string{".env*"}, Writable: true, MaxFileBytes: 1000}
	for _, p := range []string{"../outside.txt", "/etc/passwd", ".git/config", ".env", "sub/../../x", "~/x"} {
		if got := ws.Run(map[string]any{"tool": "read_file", "path": p}); !strings.HasPrefix(got, "denied") && !strings.HasPrefix(got, "error") {
			t.Errorf("read %s: %q", p, got)
		}
		if got := ws.Run(map[string]any{"tool": "write_file", "path": p, "content": "x"}); !strings.HasPrefix(got, "denied") {
			t.Errorf("write %s: %q", p, got)
		}
	}
	if _, err := os.Stat(filepath.Join(filepath.Dir(root), "outside.txt")); err == nil {
		t.Fatal("escaped the workspace")
	}
}

func TestSymlinkEscape(t *testing.T) {
	root := workspace(t)
	secret := filepath.Join(t.TempDir(), "secret.txt")
	os.WriteFile(secret, []byte("top secret"), 0o644)
	os.Symlink(secret, filepath.Join(root, "link.txt"))
	os.Symlink(filepath.Dir(secret), filepath.Join(root, "linkdir"))
	ws := &Workspace{Root: root, Writable: true, MaxFileBytes: 1000}
	if got := ws.Run(map[string]any{"tool": "read_file", "path": "link.txt"}); strings.Contains(got, "top secret") {
		t.Fatal("read through symlink")
	}
	if got := ws.Run(map[string]any{"tool": "write_file", "path": "link.txt", "content": "x"}); !strings.HasPrefix(got, "denied") {
		t.Fatalf("write through symlink: %q", got)
	}
	if got := ws.Run(map[string]any{"tool": "write_file", "path": "linkdir/new.txt", "content": "x"}); !strings.HasPrefix(got, "denied") {
		t.Fatalf("write through dir symlink: %q", got)
	}
	data, _ := os.ReadFile(secret)
	if string(data) != "top secret" {
		t.Fatal("secret modified")
	}
}

func TestReadOnlyAndLimits(t *testing.T) {
	root := workspace(t)
	ro := &Workspace{Root: root, MaxFileBytes: 10}
	if !strings.HasPrefix(ro.Run(map[string]any{"tool": "write_file", "path": "a", "content": "x"}), "denied") {
		t.Fatal("plan mode allowed write")
	}
	rw := &Workspace{Root: root, Writable: true, MaxFileBytes: 10}
	if !strings.HasPrefix(rw.Run(map[string]any{"tool": "write_file", "path": "big", "content": strings.Repeat("x", 11)}), "denied") {
		t.Fatal("size limit not enforced")
	}
	if strings.Contains(rw.Run(map[string]any{"tool": "list_dir", "path": "."}), ".git") {
		t.Fatal(".git listed")
	}
}

func TestParseReply(t *testing.T) {
	if ParseReply(`{"final": 1}`)["final"] != float64(1) {
		t.Error("plain")
	}
	if ParseReply("```json\n{\"final\": 2}\n```")["final"] != float64(2) {
		t.Error("fenced")
	}
	if ParseReply("nope") != nil {
		t.Error("garbage")
	}
}

type fakeModel struct{ replies []string }

func (f *fakeModel) Name() string { return "fake" }
func (f *fakeModel) Generate(_ context.Context, _ string, _ bool) (string, int, error) {
	r := f.replies[0]
	f.replies = f.replies[1:]
	return r, 1000, nil
}

func TestTokenBudget(t *testing.T) {
	root := workspace(t)
	m := &fakeModel{replies: []string{`{"actions":[{"tool":"list_dir","path":"."}]}`, `{"actions":[{"tool":"list_dir","path":"."}]}`}}
	out := Run(context.Background(), Input{Mode: "plan", Provider: "gemini", TokenBudget: 500, MaxTurns: 5}, root, m)
	if out.OK || !strings.Contains(out.Error, "budget") {
		t.Fatalf("%+v", out)
	}
}
