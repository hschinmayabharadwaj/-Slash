package pipeline

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"sarvas/internal/config"
	"sarvas/internal/runner"
	"sarvas/internal/sandbox"
)

func git(t *testing.T, dir string, args ...string) string {
	t.Helper()
	out, err := exec.Command("git", append([]string{"-C", dir}, args...)...).CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
	return string(out)
}

// origin creates a bare repo with a README on main.
func origin(t *testing.T) string {
	root := t.TempDir()
	bare := filepath.Join(root, "origin.git")
	exec.Command("git", "init", "--bare", "-q", "-b", "main", bare).Run()
	seed := filepath.Join(root, "seed")
	os.MkdirAll(seed, 0o755)
	git(t, seed, "init", "-q", "-b", "main")
	os.WriteFile(filepath.Join(seed, "README.md"), []byte("# Origin\n"), 0o644)
	git(t, seed, "add", "-A")
	git(t, seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
	git(t, seed, "push", "-q", bare, "main")
	return bare
}

type fakeTarget struct {
	path string
	prs  []string
}

func (f *fakeTarget) CloneURL(_, _ string) (string, error)  { return f.path, nil }
func (f *fakeTarget) AuthEnv(_, _ string) ([]string, error) { return nil, nil }
func (f *fakeTarget) CreatePR(_, _, head, base, _, _ string) (string, error) {
	f.prs = append(f.prs, head+"->"+base)
	return "https://github.com/o/r/pull/1", nil
}

type fakeSandbox struct {
	plan     map[string]any
	edit     func(repo string)
	checksRC int
	runs     []string
}

func (f *fakeSandbox) RunAgent(mode, repo, io string, _ time.Duration, _ string) (sandbox.Result, error) {
	f.runs = append(f.runs, mode)
	out := runner.Output{OK: true, Mode: mode, Text: "Made changes"}
	if mode == "plan" {
		out.Plan = f.plan
	} else if f.edit != nil {
		f.edit(repo)
	}
	data, _ := json.Marshal(out)
	return sandbox.Result{}, os.WriteFile(filepath.Join(io, "output.json"), data, 0o600)
}

func (f *fakeSandbox) RunChecks(_, cmd string, _ time.Duration) (sandbox.Result, error) {
	f.runs = append(f.runs, "checks:"+cmd)
	return sandbox.Result{ExitCode: f.checksRC, Stdout: "test output"}, nil
}

type quiet struct{ plans, impls int }

func (q *quiet) Say(Task, string)                            {}
func (q *quiet) Plan(Task, Plan)                             { q.plans++ }
func (q *quiet) Implementation(Task, string, string, string) { q.impls++ }
func (q *quiet) PROpened(Task, string)                       {}

func plan(over map[string]any) map[string]any {
	p := map[string]any{"feasible": true, "summary": "Add a greeting", "files": []any{"README.md"},
		"risk": "low", "risk_reason": "docs", "questions": []any{}, "how_to_verify": "Read it"}
	for k, v := range over {
		p[k] = v
	}
	return p
}

func writeFile(name, content string) func(string) {
	return func(repo string) { os.WriteFile(filepath.Join(repo, name), []byte(content), 0o644) }
}

func setup(t *testing.T, box *fakeSandbox) (*Worker, *fakeTarget, *Task, *quiet) {
	cfg := config.Default()
	cfg.AI.Provider = "sim"
	cfg.Repos["o/r"] = config.Repo{LintCommand: "lint", TestCommand: "test", ExtraDenyPaths: []string{"src/admin/**"}}
	target := &fakeTarget{path: origin(t)}
	store := NewStore()
	q := &quiet{}
	w := &Worker{Cfg: cfg, Store: store, Notify: q, Target: target, Sandbox: box, WorkRoot: t.TempDir()}
	return w, target, store.Create("o", "r", "Add a greeting"), q
}

func toImplApproval(t *testing.T, w *Worker, id int) {
	w.Step(id)
	if s := w.Store.Get(id).Status; s != AwaitingPlanApproval {
		t.Fatalf("after plan: %s (%s)", s, w.Store.Get(id).Message)
	}
	w.Store.Transition(id, AwaitingPlanApproval, PlanApproved)
	w.Step(id)
}

func TestFullFlowAppliesExactApprovedDiff(t *testing.T) {
	box := &fakeSandbox{plan: plan(nil), edit: writeFile("README.md", "# Origin\nHello!\n")}
	w, target, task, q := setup(t, box)
	toImplApproval(t, w, task.ID)
	got := w.Store.Get(task.ID)
	if got.Status != AwaitingImplApproval || got.DiffSHA256 != SHA256(got.Diff) || !strings.Contains(got.Diff, "+Hello!") {
		t.Fatalf("impl: %s %q", got.Status, got.Message)
	}
	w.Store.Transition(task.ID, AwaitingImplApproval, ImplApproved)
	w.Step(task.ID)
	done := w.Store.Get(task.ID)
	if done.Status != Completed || len(target.prs) != 1 {
		t.Fatalf("pr: %s %q", done.Status, done.Message)
	}
	if strings.Join(box.runs, ",") != "plan,implement,checks:lint,checks:test" {
		t.Fatalf("agent must not be re-run after approval: %v", box.runs)
	}
	shown := git(t, target.path, "show", done.Branch+":README.md")
	if shown != "# Origin\nHello!\n" || q.plans != 1 || q.impls != 1 {
		t.Fatalf("pushed content %q", shown)
	}
}

func TestTamperedDiffRefused(t *testing.T) {
	w, target, task, _ := setup(t, &fakeSandbox{plan: plan(nil), edit: writeFile("README.md", "x\n")})
	toImplApproval(t, w, task.ID)
	w.Store.update(task.ID, func(x *Task) { x.Diff += "+evil\n" })
	w.Store.Transition(task.ID, AwaitingImplApproval, ImplApproved)
	w.Step(task.ID)
	if s := w.Store.Get(task.ID).Status; s != Declined || len(target.prs) != 0 {
		t.Fatalf("status %s", s)
	}
}

func TestPolicyBlocks(t *testing.T) {
	for name, edit := range map[string]func(string){
		"protected": writeFile(".env", "A=1\n"),
		"secret":    writeFile("cfg.go", "token = 'abcdefghijklmnopqrstuvwxyz'\n"),
	} {
		w, _, task, q := setup(t, &fakeSandbox{plan: plan(nil), edit: edit})
		toImplApproval(t, w, task.ID)
		got := w.Store.Get(task.ID)
		if got.Status != Declined || !strings.Contains(got.Message, name) || q.impls != 0 {
			t.Errorf("%s: %s %q", name, got.Status, got.Message)
		}
	}
}

func TestFailedChecksBlock(t *testing.T) {
	w, _, task, q := setup(t, &fakeSandbox{plan: plan(nil), edit: writeFile("README.md", "x\n"), checksRC: 1})
	toImplApproval(t, w, task.ID)
	if s := w.Store.Get(task.ID).Status; s != Failed || q.impls != 0 {
		t.Fatalf("status %s", s)
	}
}

func TestPlanDeclines(t *testing.T) {
	for _, over := range []map[string]any{{"risk": "high"}, {"files": []any{"src/admin/x.py"}}, {"feasible": false}} {
		w, _, task, q := setup(t, &fakeSandbox{plan: plan(over)})
		w.Step(task.ID)
		if s := w.Store.Get(task.ID).Status; (s != Declined && s != NeedsInfo) || q.plans != 0 {
			t.Errorf("%v -> %s", over, s)
		}
	}
}

func TestCancelMidImplementation(t *testing.T) {
	var w *Worker
	var id int
	box := &fakeSandbox{plan: plan(nil)}
	box.edit = func(repo string) {
		writeFile("README.md", "x\n")(repo)
		w.Store.Transition(id, Implementing, Cancelled)
	}
	w, _, task, q := setup(t, box)
	id = task.ID
	toImplApproval(t, w, task.ID)
	if s := w.Store.Get(task.ID).Status; s != Cancelled || q.impls != 0 {
		t.Fatalf("status %s", s)
	}
}

func TestDisallowedRepo(t *testing.T) {
	w, _, task, _ := setup(t, &fakeSandbox{plan: plan(nil)})
	w.Cfg.Security.AllowedRepos = []string{"other/repo"}
	w.Step(task.ID)
	if s := w.Store.Get(task.ID).Status; s != Declined {
		t.Fatalf("status %s", s)
	}
}

func TestTransitionIsAtomic(t *testing.T) {
	s := NewStore()
	task := s.Create("o", "r", "x")
	if !s.Transition(task.ID, Pending, Planning) || s.Transition(task.ID, Pending, Planning) {
		t.Fatal("transition not atomic")
	}
}
