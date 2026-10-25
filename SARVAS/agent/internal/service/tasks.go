package service

import (
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
	"time"

	"sarvas/internal/github"
	"sarvas/internal/guards"
	"sarvas/internal/pipeline"
)

const maxDiffChars = 200_000

// notifier keeps the latest progress message on the task for the web page.
type notifier struct{ store *pipeline.Store }

func (n notifier) Say(t pipeline.Task, text string) { n.store.SetMessage(t.ID, text) }
func (n notifier) Plan(t pipeline.Task, _ pipeline.Plan) {
	n.store.SetMessage(t.ID, "📋 The plan is ready for approval.")
}
func (n notifier) Implementation(t pipeline.Task, _, _, _ string) {
	n.store.SetMessage(t.ID, "✨ The change is ready. Review the diff and test results.")
}
func (n notifier) PROpened(t pipeline.Task, url string) {
	n.store.SetMessage(t.ID, "🎉 Pull request opened: "+url+" — an engineer must review and merge it.")
}

// BuildProblems lists why Build tasks can't run now (empty = ready).
func (s *Server) BuildProblems() []string {
	var problems []string
	if !s.Cfg.AI.CodeAllowed() {
		problems = append(problems, "sending repository code to the remote AI provider isn't authorized "+
			"(set ai.sendCodeToProvider, or use a self-hosted provider such as Ollama)")
	}
	if s.Worker == nil {
		return append(problems, "GitHub App installation access isn't configured (github.appId and github.privateKeyPath)")
	}
	if s.Preflight != nil {
		s.mu.Lock()
		if time.Since(s.checkedAt) > 30*time.Second {
			s.lastProblem, s.checkedAt = s.Preflight(), time.Now()
		}
		problems = append(problems, s.lastProblem...)
		s.mu.Unlock()
	}
	return problems
}

func (s *Server) capabilities(w http.ResponseWriter, _ *http.Request) {
	problems := s.BuildProblems()
	writeJSON(w, 200, map[string]any{
		"provider": s.Cfg.AI.Provider, "model": s.Cfg.AI.Model, "remote": s.Cfg.AI.Remote(),
		"codeAllowed":            s.Cfg.AI.CodeAllowed(),
		"build":                  map[string]any{"enabled": len(problems) == 0, "problems": problems},
		"requireProtectedBranch": s.Cfg.Security.RequireProtectedBranch,
	})
}

// drive runs the task until it waits for a human or finishes.
func (s *Server) drive(id int) {
	s.mu.Lock()
	if s.sem == nil {
		s.sem = make(chan struct{}, max(1, s.Cfg.Agent.MaxConcurrentTasks))
	}
	sem := s.sem
	s.mu.Unlock()
	sem <- struct{}{}
	defer func() { <-sem }()
	for s.Worker.Step(id) {
	}
}

type taskRequest struct {
	Owner              string   `json:"owner"`
	Name               string   `json:"name"`
	BaseBranch         string   `json:"baseBranch"`
	Request            string   `json:"request"`
	AcceptanceCriteria []string `json:"acceptanceCriteria"`
	Requester          string   `json:"requester"`
	Reviewers          []string `json:"reviewers"`
}

func cleanList(items []string, max, maxLen int) []string {
	var out []string
	for _, it := range items {
		it = strings.TrimSpace(guards.SanitizeUntrusted(it, maxLen))
		if it != "" && len(out) < max {
			out = append(out, it)
		}
	}
	return out
}

func (s *Server) createTask(w http.ResponseWriter, r *http.Request) {
	var req taskRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || strings.TrimSpace(req.Request) == "" {
		writeJSON(w, 400, map[string]string{"error": "invalid request"})
		return
	}
	if err := github.ValidateRepoName(req.Owner, req.Name); err != nil {
		writeJSON(w, 400, map[string]string{"error": err.Error()})
		return
	}
	if req.BaseBranch == "" || strings.ContainsAny(req.BaseBranch, " ~^:?*[\\") || strings.Contains(req.BaseBranch, "..") {
		writeJSON(w, 400, map[string]string{"error": "invalid base branch"})
		return
	}
	if !s.Cfg.RepoAllowed(req.Owner + "/" + req.Name) {
		writeJSON(w, 403, map[string]string{"error": "This repository isn't in the agent's allowed repositories."})
		return
	}
	if problems := s.BuildProblems(); len(problems) > 0 {
		writeJSON(w, 503, map[string]any{"error": "Build tasks aren't available: " + strings.Join(problems, "; "), "problems": problems})
		return
	}
	if guards.KillSwitchOn(s.Cfg.Security.KillSwitchFile) {
		writeJSON(w, 503, map[string]string{"error": "The agent is paused by an administrator (kill switch)."})
		return
	}
	t := s.Tasks.Add(pipeline.Task{
		Owner: req.Owner, Name: req.Name, BaseBranch: req.BaseBranch,
		Request:            guards.SanitizeUntrusted(req.Request, s.Cfg.Security.MaxRequestChars),
		AcceptanceCriteria: cleanList(req.AcceptanceCriteria, 10, 300),
		Requester:          guards.Slugify(req.Requester, 39),
		Reviewers:          cleanList(req.Reviewers, 10, 39),
	})
	s.Tasks.Record(t.ID, "task_created", map[string]any{"repo": t.FullName(), "requester": t.Requester})
	go s.drive(t.ID)
	writeJSON(w, 202, taskView(s.Tasks, s.Tasks.Get(t.ID)))
}

func (s *Server) lookup(w http.ResponseWriter, r *http.Request) (pipeline.Task, bool) {
	id, err := strconv.Atoi(r.PathValue("id"))
	if err != nil {
		writeJSON(w, 404, map[string]string{"error": "not found"})
		return pipeline.Task{}, false
	}
	t, ok := s.Tasks.Lookup(id)
	if !ok {
		writeJSON(w, 404, map[string]string{"error": "not found"})
	}
	return t, ok
}

func (s *Server) getTask(w http.ResponseWriter, r *http.Request) {
	if t, ok := s.lookup(w, r); ok {
		writeJSON(w, 200, taskView(s.Tasks, t))
	}
}

// decide records a human decision. The web server checks WHO may decide.
func (s *Server) decide(w http.ResponseWriter, r *http.Request) {
	t, ok := s.lookup(w, r)
	if !ok {
		return
	}
	var req struct {
		Stage   string `json:"stage"`
		Approve bool   `json:"approve"`
		By      string `json:"by"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, 400, map[string]string{"error": "invalid request"})
		return
	}
	var from, next pipeline.Status
	switch req.Stage {
	case "plan":
		from, next = pipeline.AwaitingPlanApproval, pipeline.PlanRejected
		if req.Approve {
			next = pipeline.PlanApproved
		}
	case "changes":
		from, next = pipeline.AwaitingImplApproval, pipeline.ImplRejected
		if req.Approve {
			next = pipeline.ImplApproved
		}
	default:
		writeJSON(w, 400, map[string]string{"error": "stage must be plan or changes"})
		return
	}
	if req.Approve && guards.KillSwitchOn(s.Cfg.Security.KillSwitchFile) {
		writeJSON(w, 503, map[string]string{"error": "The agent is paused by an administrator (kill switch)."})
		return
	}
	if !s.Tasks.Transition(t.ID, from, next) {
		writeJSON(w, 409, map[string]string{"error": "This task isn't waiting for that decision any more."})
		return
	}
	s.Tasks.Record(t.ID, req.Stage+"_"+map[bool]string{true: "approved", false: "rejected"}[req.Approve],
		map[string]any{"by": guards.Slugify(req.By, 39)})
	if req.Approve {
		go s.drive(t.ID)
	}
	writeJSON(w, 200, taskView(s.Tasks, s.Tasks.Get(t.ID)))
}

func taskView(store *pipeline.Store, t pipeline.Task) map[string]any {
	diff := t.Diff
	truncated := len(diff) > maxDiffChars
	if truncated {
		diff = diff[:maxDiffChars]
	}
	var events []map[string]any
	for _, e := range store.TaskEvents(t.ID) {
		events = append(events, map[string]any{"name": e.Name, "detail": e.Detail, "at": e.At.Format(time.RFC3339)})
	}
	return map[string]any{
		"id": t.ID, "owner": t.Owner, "name": t.Name, "status": string(t.Status), "terminal": t.Status.Terminal(),
		"request": t.Request, "requester": t.Requester, "baseBranch": t.BaseBranch,
		"acceptanceCriteria": t.AcceptanceCriteria, "plan": t.Plan, "branch": t.Branch,
		"summary": t.Summary, "diff": diff, "diffTruncated": truncated, "diffSha256": t.DiffSHA256,
		"checks": t.Checks, "checksOk": t.ChecksOK, "files": t.Files, "prUrl": t.PRURL, "message": t.Message,
		"created": t.Created.Format(time.RFC3339), "updated": t.Updated.Format(time.RFC3339), "events": events,
	}
}
