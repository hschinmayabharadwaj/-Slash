// Package pipeline drives a task: plan -> human approval -> implement ->
// verify -> human approval -> PR.
//
// The agent only ever proposes. Policy limits, secret scanning, protected
// paths, trusted checks and both human approvals run here, in plain code.
// The PR contains exactly the diff the approver saw (checked by SHA-256);
// the agent is never re-run after approval.
package pipeline

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"sarvas/internal/config"
	"sarvas/internal/gitops"
	"sarvas/internal/guards"
	"sarvas/internal/runner"
	"sarvas/internal/sandbox"
)

type Status string

const (
	Pending              Status = "pending"
	Planning             Status = "planning"
	AwaitingPlanApproval Status = "awaiting_plan_approval"
	PlanApproved         Status = "plan_approved"
	PlanRejected         Status = "plan_rejected"
	Implementing         Status = "implementing"
	Verifying            Status = "verifying"
	AwaitingImplApproval Status = "awaiting_impl_approval"
	ImplApproved         Status = "impl_approved"
	ImplRejected         Status = "impl_rejected"
	CreatingPR           Status = "creating_pr"
	Completed            Status = "completed"
	Failed               Status = "failed"
	Cancelled            Status = "cancelled"
	NeedsInfo            Status = "needs_info"
	Declined             Status = "declined"
)

// Terminal reports whether no further work will happen.
func (s Status) Terminal() bool {
	switch s {
	case Completed, Failed, Cancelled, NeedsInfo, Declined, PlanRejected, ImplRejected:
		return true
	}
	return false
}

type Plan struct {
	Feasible    bool     `json:"feasible"`
	Summary     string   `json:"summary"`
	Files       []string `json:"files"`
	Risk        string   `json:"risk"`
	RiskReason  string   `json:"risk_reason"`
	Questions   []string `json:"questions"`
	HowToVerify string   `json:"how_to_verify"`
}

type Task struct {
	ID          int
	Owner, Name string
	Request     string
	Status      Status
	Plan        *Plan
	Branch      string
	Summary     string
	Diff        string
	DiffSHA256  string
	Checks      string
	PRURL       string
	Message     string
}

func (t *Task) FullName() string { return t.Owner + "/" + t.Name }

// Store keeps tasks with atomic status transitions (in memory; safe for goroutines).
type Store struct {
	mu     sync.Mutex
	tasks  map[int]*Task
	nextID int
	Events []Event
}

type Event struct {
	TaskID int
	Name   string
	Detail map[string]any
	At     time.Time
}

func NewStore() *Store { return &Store{tasks: map[int]*Task{}} }

func (s *Store) Create(owner, name, request string) *Task {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.nextID++
	t := &Task{ID: s.nextID, Owner: owner, Name: name, Request: request, Status: Pending}
	s.tasks[t.ID] = t
	return t
}

// Get returns a copy of the task.
func (s *Store) Get(id int) Task {
	s.mu.Lock()
	defer s.mu.Unlock()
	return *s.tasks[id]
}

// Transition moves expected -> next atomically; false if the task is elsewhere.
func (s *Store) Transition(id int, expected, next Status) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	t := s.tasks[id]
	if t == nil || t.Status != expected {
		return false
	}
	t.Status = next
	return true
}

func (s *Store) update(id int, fn func(*Task)) {
	s.mu.Lock()
	defer s.mu.Unlock()
	fn(s.tasks[id])
}

func (s *Store) Record(id int, name string, detail map[string]any) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.Events = append(s.Events, Event{id, name, detail, time.Now().UTC()})
}

// Notifier reports progress (console in the CLI).
type Notifier interface {
	Say(t Task, text string)
	Plan(t Task, p Plan)
	Implementation(t Task, summary, diffPreview, checks string)
	PROpened(t Task, url string)
}

// Target is where code comes from and goes to (GitHub, or a local repo).
type Target interface {
	CloneURL(owner, name string) (string, error)
	AuthEnv(owner, name string) ([]string, error)
	CreatePR(owner, name, head, base, title, body string) (string, error)
}

// Abort is an expected stop with a user-facing message.
type Abort struct {
	Msg    string
	Status Status
}

func (a *Abort) Error() string { return a.Msg }

var errSuperseded = errors.New("task changed state")

type Worker struct {
	Cfg      config.Config
	Store    *Store
	Notify   Notifier
	Target   Target
	Sandbox  sandbox.Runner
	WorkRoot string
}

func SHA256(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:])
}

// Step advances the task by one phase. Returns false if nothing to do.
func (w *Worker) Step(id int) bool {
	t := w.Store.Get(id)
	var err error
	switch t.Status {
	case Pending:
		err = w.plan(t)
	case PlanApproved:
		err = w.implement(t)
	case ImplApproved:
		err = w.createPR(t)
	default:
		return false
	}
	var abort *Abort
	switch {
	case err == nil, errors.Is(err, errSuperseded):
	case errors.As(err, &abort):
		w.end(t, abort.Msg, abort.Status)
	default:
		w.end(t, fmt.Sprintf("Something went wrong (task #%d): %v. Nothing was merged.", t.ID, err), Failed)
	}
	return true
}

func (w *Worker) advance(t *Task, expected, next Status) error {
	if !w.Store.Transition(t.ID, expected, next) {
		return errSuperseded
	}
	t.Status = next
	return nil
}

func (w *Worker) end(t Task, msg string, status Status) {
	cur := w.Store.Get(t.ID)
	if cur.Status == Cancelled || cur.Status == Completed {
		return
	}
	w.Store.update(t.ID, func(x *Task) { x.Status, x.Message = status, msg })
	w.Store.Record(t.ID, string(status), map[string]any{"message": guards.Tail(msg, 500)})
	w.Notify.Say(t, msg)
}

func (w *Worker) checkRepo(t *Task) error {
	if !w.Cfg.RepoAllowed(t.FullName()) {
		return &Abort{fmt.Sprintf("I'm not allowed to work on %s.", t.FullName()), Declined}
	}
	return nil
}

func (w *Worker) workDir(id int) (string, func(), error) {
	dir := filepath.Join(w.WorkRoot, fmt.Sprintf("task-%d", id))
	_ = os.RemoveAll(dir)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", nil, err
	}
	return dir, func() { _ = os.RemoveAll(dir) }, nil
}

func (w *Worker) clone(t *Task, dest string) error {
	url, err := w.Target.CloneURL(t.Owner, t.Name)
	if err != nil {
		return err
	}
	env, err := w.Target.AuthEnv(t.Owner, t.Name)
	if err != nil {
		return err
	}
	return gitops.Clone(url, dest, w.Cfg.Repo(t.FullName()).DefaultBranch, env)
}

func (w *Worker) agentInput(t *Task, mode string) runner.Input {
	budget := w.Cfg.Agent.PlanningBudget
	if mode == "implement" {
		budget = w.Cfg.Agent.ImplementationBudget
	}
	in := runner.Input{
		Request:      guards.SanitizeUntrusted(t.Request, w.Cfg.Security.MaxRequestChars),
		Mode:         mode,
		Provider:     w.Cfg.AI.Provider,
		Model:        w.Cfg.AI.Model,
		MaxTurns:     w.Cfg.Agent.MaxTurns,
		TokenBudget:  budget,
		DenyPatterns: w.Cfg.DenyFor(t.FullName()),
		MaxFileBytes: w.Cfg.Security.MaxFileSizeMB * 1024 * 1024,
	}
	if t.Plan != nil {
		b, _ := json.Marshal(t.Plan)
		_ = json.Unmarshal(b, &in.Plan)
	}
	return in
}

func (w *Worker) apiKey() string {
	if w.Cfg.AI.Provider == "gemini" {
		return w.Cfg.AI.APIKey
	}
	return ""
}

// runAgent writes input.json, runs the sandbox, and validates output.json.
func (w *Worker) runAgent(t *Task, mode, repoDir, ioDir string, timeoutS int) (runner.Output, error) {
	var out runner.Output
	if err := os.MkdirAll(ioDir, 0o700); err != nil {
		return out, err
	}
	in, _ := json.MarshalIndent(w.agentInput(t, mode), "", "  ")
	if err := os.WriteFile(filepath.Join(ioDir, "input.json"), in, 0o600); err != nil {
		return out, err
	}
	res, err := w.Sandbox.RunAgent(mode, repoDir, ioDir, time.Duration(timeoutS)*time.Second, w.apiKey())
	if err != nil {
		return out, err
	}
	if res.TimedOut {
		return out, &Abort{"That took longer than my time limit, so I stopped. Try a smaller request.", Failed}
	}
	data, err := os.ReadFile(filepath.Join(ioDir, "output.json"))
	if err != nil {
		return out, &Abort{"The coding sandbox failed to start or crashed. This is a problem on my side.", Failed}
	}
	if json.Unmarshal(data, &out) != nil {
		return out, &Abort{"The agent returned something I couldn't read.", Failed}
	}
	if !out.OK {
		return out, &Abort{fmt.Sprintf("I couldn't complete that (%s).", guards.Tail(out.Error, 300)), Failed}
	}
	return out, nil
}

// NormalizePlan validates the agent's plan and clamps every field.
func NormalizePlan(raw map[string]any) (*Plan, error) {
	if raw == nil {
		return nil, &Abort{"Couldn't produce a clear plan. Try rephrasing with more detail.", Failed}
	}
	if _, ok := raw["feasible"]; !ok {
		return nil, &Abort{"Plan missing required field 'feasible'. Try rephrasing.", Failed}
	}
	str := func(k string, max int) string {
		s, _ := raw[k].(string)
		s = strings.TrimSpace(s)
		if len(s) > max {
			s = s[:max]
		}
		return s
	}
	list := func(k string, max int) []string {
		var out []string
		items, _ := raw[k].([]any)
		for _, it := range items {
			if s := strings.TrimSpace(fmt.Sprint(it)); s != "" && len(out) < max {
				out = append(out, s)
			}
		}
		return out
	}
	feasible, _ := raw["feasible"].(bool)
	risk, _ := raw["risk"].(string)
	if risk != "low" && risk != "medium" && risk != "high" {
		risk = "medium"
	}
	return &Plan{
		Feasible: feasible, Summary: str("summary", 1500), Files: list("files", 20), Risk: risk,
		RiskReason: str("risk_reason", 300), Questions: list("questions", 5), HowToVerify: str("how_to_verify", 600),
	}, nil
}

func (w *Worker) plan(t Task) error {
	if err := w.advance(&t, Pending, Planning); err != nil {
		return err
	}
	if err := w.checkRepo(&t); err != nil {
		return err
	}
	w.Notify.Say(t, "🔍 Reading the code and drafting a plan...")
	dir, cleanup, err := w.workDir(t.ID)
	if err != nil {
		return err
	}
	defer cleanup()
	repoDir := filepath.Join(dir, "repo")
	if err := w.clone(&t, repoDir); err != nil {
		return err
	}
	out, err := w.runAgent(&t, "plan", repoDir, filepath.Join(dir, "io"), w.Cfg.Agent.PlanTimeoutS)
	if err != nil {
		return err
	}
	plan, err := NormalizePlan(out.Plan)
	if err != nil {
		return err
	}
	w.Store.Record(t.ID, "plan_generated", map[string]any{"risk": plan.Risk, "files": plan.Files,
		"tokens": out.TokensUsed, "provider": out.Provider})

	if !plan.Feasible || len(plan.Questions) > 0 {
		msg := "🤔 I can't start on this yet.\n" + plan.Summary
		for _, q := range plan.Questions {
			msg += "\n• " + q
		}
		return &Abort{msg, NeedsInfo}
	}
	for _, f := range plan.Files {
		if guards.MatchesAny(f, w.Cfg.DenyFor(t.FullName())) {
			return &Abort{"⛔ I won't make this change myself (it touches protected files). Please ask an engineer.", Declined}
		}
	}
	if plan.Risk == "high" {
		return &Abort{"⛔ I won't make this change myself (" + plan.RiskReason + "). Please ask an engineer.", Declined}
	}
	w.Store.update(t.ID, func(x *Task) { x.Plan = plan })
	t.Plan = plan
	if err := w.advance(&t, Planning, AwaitingPlanApproval); err != nil {
		return err
	}
	w.Notify.Plan(t, *plan)
	return nil
}

func (w *Worker) enforcePolicy(t *Task, repoDir string, report guards.DiffReport) error {
	sec, repo := w.Cfg.Security, w.Cfg.Repo(t.FullName())
	maxFiles, maxLines := sec.MaxFiles, sec.MaxChangedLines
	if repo.MaxFiles > 0 {
		maxFiles = repo.MaxFiles
	}
	if repo.MaxLines > 0 {
		maxLines = repo.MaxLines
	}
	problems := guards.CheckPolicy(report, maxFiles, maxLines, w.Cfg.DenyFor(t.FullName()))
	limit := int64(sec.MaxFileSizeMB) * 1024 * 1024
	for _, f := range report.Files {
		if info, err := os.Stat(filepath.Join(repoDir, f.Path)); err == nil && info.Size() > limit {
			problems = append(problems, fmt.Sprintf("`%s` is larger than %d MB", f.Path, sec.MaxFileSizeMB))
		}
	}
	if sec.ScanSecrets {
		problems = append(problems, guards.ScanSecrets(report.AddedLines)...)
	}
	if len(problems) > 0 {
		w.Store.Record(t.ID, "policy_blocked", map[string]any{"problems": problems})
		return &Abort{"⛔ I stopped without opening a PR because:\n• " + strings.Join(problems, "\n• ") +
			"\nTry a smaller, more specific request.", Declined}
	}
	return nil
}

func (w *Worker) runChecks(t *Task, repoDir string) (bool, string) {
	repo := w.Cfg.Repo(t.FullName())
	var commands []string
	for _, c := range []string{repo.LintCommand, repo.TestCommand} {
		if c != "" {
			commands = append(commands, c)
		}
	}
	if len(commands) == 0 {
		return true, "No checks configured for this repository."
	}
	ok := true
	var report []string
	for _, c := range commands {
		res, err := w.Sandbox.RunChecks(repoDir, c, time.Duration(w.Cfg.Agent.CheckTimeoutS)*time.Second)
		status := "passed"
		switch {
		case err != nil:
			status, ok = "error: "+err.Error(), false
		case res.TimedOut:
			status, ok = fmt.Sprintf("timed out after %ds", w.Cfg.Agent.CheckTimeoutS), false
		case res.ExitCode != 0:
			status, ok = fmt.Sprintf("failed (exit %d)", res.ExitCode), false
		}
		report = append(report, fmt.Sprintf("$ %s\n%s\n%s", c, status, guards.Tail(res.Stdout+"\n"+res.Stderr, 1200)))
	}
	return ok, strings.Join(report, "\n\n")
}

func (w *Worker) implement(t Task) error {
	if err := w.advance(&t, PlanApproved, Implementing); err != nil {
		return err
	}
	if err := w.checkRepo(&t); err != nil {
		return err
	}
	w.Notify.Say(t, "🛠 Approved. Working on the change now...")
	branch := fmt.Sprintf("agent/%d-%s-%04x", t.ID, guards.Slugify(t.Plan.Summary, 30), time.Now().UnixNano()&0xffff)
	w.Store.update(t.ID, func(x *Task) { x.Branch = branch })

	dir, cleanup, err := w.workDir(t.ID)
	if err != nil {
		return err
	}
	defer cleanup()
	repoDir := filepath.Join(dir, "repo")
	if err := w.clone(&t, repoDir); err != nil {
		return err
	}
	out, err := w.runAgent(&t, "implement", repoDir, filepath.Join(dir, "io"), w.Cfg.Agent.ImplementTimeoutS)
	if err != nil {
		return err
	}
	// Verify on the host -- never trust the sandbox's own account.
	if err := w.advance(&t, Implementing, Verifying); err != nil {
		return err
	}
	report, err := gitops.InspectWorktree(repoDir)
	if err != nil {
		return err
	}
	if len(report.Files) == 0 {
		return &Abort{"I didn't end up changing any files.\n" + guards.Tail(out.Text, 600), Failed}
	}
	if err := w.enforcePolicy(&t, repoDir, report); err != nil {
		return err
	}
	diff, err := gitops.StagedDiff(repoDir)
	if err != nil {
		return err
	}
	checksOK, checks := w.runChecks(&t, repoDir)
	files := make([]string, 0, len(report.Files))
	for _, f := range report.Files {
		files = append(files, f.Path)
	}
	w.Store.Record(t.ID, "implementation_verified", map[string]any{"files": files,
		"lines": report.TotalChanged(), "checks_ok": checksOK, "diff_sha256": SHA256(diff)})
	if !checksOK && w.Cfg.Security.BlockOnFailedChecks {
		return &Abort{"❌ The change broke the repository's checks, so I stopped before opening a PR.\n" +
			guards.Tail(checks, 1500), Failed}
	}
	summary := guards.Tail(out.Text, 2000)
	w.Store.update(t.ID, func(x *Task) {
		x.Summary, x.Diff, x.DiffSHA256, x.Checks = summary, diff, SHA256(diff), checks
	})
	if err := w.advance(&t, Verifying, AwaitingImplApproval); err != nil {
		return err
	}
	preview := diff
	if len(preview) > 4000 {
		preview = preview[:4000] + "\n... (truncated)"
	}
	w.Notify.Implementation(t, summary, preview, checks)
	return nil
}

func (w *Worker) createPR(t Task) error {
	if err := w.advance(&t, ImplApproved, CreatingPR); err != nil {
		return err
	}
	if err := w.checkRepo(&t); err != nil {
		return err
	}
	if strings.TrimSpace(t.Diff) == "" {
		return &Abort{"There is no approved change to open a PR with.", Failed}
	}
	if t.DiffSHA256 == "" || SHA256(t.Diff) != t.DiffSHA256 {
		return &Abort{"⛔ The stored change doesn't match what was approved. Stopping.", Declined}
	}
	dir, cleanup, err := w.workDir(t.ID)
	if err != nil {
		return err
	}
	defer cleanup()
	repoDir := filepath.Join(dir, "repo")
	if err := w.clone(&t, repoDir); err != nil {
		return err
	}
	if err := gitops.CreateBranch(repoDir, t.Branch); err != nil {
		return err
	}
	if err := gitops.ApplyPatch(repoDir, t.Diff); err != nil {
		return err
	}
	// Defense in depth: re-check exactly what is about to be committed.
	report, err := gitops.InspectWorktree(repoDir)
	if err != nil {
		return err
	}
	if err := w.enforcePolicy(&t, repoDir, report); err != nil {
		return err
	}
	summary := t.Request
	if t.Plan != nil && t.Plan.Summary != "" {
		summary = t.Plan.Summary
	}
	title := summary
	if len(title) > 72 {
		title = title[:72]
	}
	if err := gitops.Commit(repoDir, fmt.Sprintf("%s\n\nAgent task #%d", title, t.ID),
		w.Cfg.GitHub.AuthorName, w.Cfg.GitHub.AuthorEmail); err != nil {
		return err
	}
	env, err := w.Target.AuthEnv(t.Owner, t.Name)
	if err != nil {
		return err
	}
	if err := gitops.Push(repoDir, t.Branch, env); err != nil {
		return err
	}
	safe := guards.NeutralizeMentions
	body := strings.Join([]string{
		"## Summary", safe(summary), "",
		"## How to verify", safe(t.Plan.HowToVerify), "",
		"## Checks", "```", guards.Tail(t.Checks, 3000), "```", "",
		"**Request:** " + safe(t.Request),
		"**Risk:** " + t.Plan.Risk,
		fmt.Sprintf("**Agent task:** #%d (plan and diff approved by a human)", t.ID),
	}, "\n")
	url, err := w.Target.CreatePR(t.Owner, t.Name, t.Branch, w.Cfg.Repo(t.FullName()).DefaultBranch,
		safe("[agent] "+title), body)
	if err != nil {
		return err
	}
	w.Store.update(t.ID, func(x *Task) { x.PRURL = url })
	if err := w.advance(&t, CreatingPR, Completed); err != nil {
		return err
	}
	w.Store.Record(t.ID, "pr_created", map[string]any{"url": url, "branch": t.Branch})
	w.Notify.PROpened(t, url)
	return nil
}
