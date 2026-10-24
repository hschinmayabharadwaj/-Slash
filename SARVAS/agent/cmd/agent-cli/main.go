// agent-cli runs the sandboxed plan -> approve -> implement -> approve
// pipeline from a terminal.
//
//	# local checkout: the result is pushed as a new branch into that repo
//	agent-cli -repo ./my-project -task "add a --version flag"
//
//	# GitHub repo (needs github.appId/privateKeyPath/installationId): opens a PR
//	agent-cli -repo owner/name -task "fix the README typo"
//
// Only committed work in a local checkout is used.
package main

import (
	"bufio"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"sarvas/internal/config"
	"sarvas/internal/github"
	"sarvas/internal/gitops"
	"sarvas/internal/guards"
	"sarvas/internal/pipeline"
	"sarvas/internal/sandbox"
)

type console struct{}

func (console) Say(_ pipeline.Task, text string) { fmt.Println("\n" + text) }
func (console) Plan(_ pipeline.Task, p pipeline.Plan) {
	fmt.Printf("\n%s\n📋 PLAN\n%s\n%s\n\nFiles: %s\nRisk: %s — %s\nHow to verify: %s\n",
		strings.Repeat("=", 60), strings.Repeat("=", 60), p.Summary, strings.Join(p.Files, ", "),
		p.Risk, p.RiskReason, p.HowToVerify)
}
func (console) Implementation(_ pipeline.Task, summary, diff, checks string) {
	fmt.Printf("\n%s\n✨ CHANGES\n%s\n%s\n\n--- diff ---\n%s\n--- checks ---\n%s\n",
		strings.Repeat("=", 60), strings.Repeat("=", 60), summary, diff, checks)
}
func (console) PROpened(_ pipeline.Task, url string) { fmt.Println("\n🎉 " + url) }

// localTarget stands in for GitHub: clone from a local path, "PR" = push a branch back.
type localTarget struct{ path string }

func (l localTarget) CloneURL(_, _ string) (string, error)  { return l.path, nil }
func (l localTarget) AuthEnv(_, _ string) ([]string, error) { return nil, nil }
func (l localTarget) CreatePR(_, _, head, base, _, _ string) (string, error) {
	return fmt.Sprintf("Branch '%s' was created in %s (review: git -C %s diff %s...%s)", head, l.path, l.path, base, head), nil
}

func ask(in *bufio.Reader, prompt string) bool {
	fmt.Printf("\n%s [y/N]: ", prompt)
	line, _ := in.ReadString('\n')
	line = strings.ToLower(strings.TrimSpace(line))
	return line == "y" || line == "yes"
}

func main() {
	cfgPath := flag.String("config", "config.json", "path to config.json")
	repoArg := flag.String("repo", "", "local git checkout, or owner/repo on GitHub")
	task := flag.String("task", "", "what you want changed")
	planOnly := flag.Bool("plan-only", false, "stop after showing the plan")
	flag.Parse()
	if *repoArg == "" || strings.TrimSpace(*task) == "" {
		flag.Usage()
		os.Exit(2)
	}
	cfg, err := config.Load(*cfgPath)
	if err != nil {
		fatal("config: %v", err)
	}

	var owner, name string
	var target pipeline.Target
	if info, err := os.Stat(*repoArg); err == nil && info.IsDir() {
		abs, _ := filepath.Abs(*repoArg)
		if _, err := os.Stat(filepath.Join(abs, ".git")); err != nil {
			fatal("%s is not a git repository", abs)
		}
		owner, name = "local", guards.Slugify(filepath.Base(abs), 60)
		branch, err := gitops.CurrentBranch(abs)
		if err != nil {
			fatal("%v", err)
		}
		repo := cfg.Repos[owner+"/"+name]
		repo.DefaultBranch = branch
		cfg.Repos[owner+"/"+name] = repo
		if len(cfg.Security.AllowedRepos) > 0 {
			cfg.Security.AllowedRepos = append(cfg.Security.AllowedRepos, owner+"/"+name)
		}
		target = localTarget{abs}
	} else {
		parts := strings.SplitN(*repoArg, "/", 2)
		if len(parts) != 2 {
			fatal("-repo must be a local path or owner/repo")
		}
		owner, name = parts[0], parts[1]
		if err := github.ValidateRepoName(owner, name); err != nil {
			fatal("%v", err)
		}
		if !cfg.RepoAllowed(*repoArg) {
			fatal("%s is not in security.allowedRepos", *repoArg)
		}
		gh := cfg.GitHub
		if gh.AppID == "" || gh.PrivateKeyPath == "" || gh.InstallationID == "" {
			fatal("github.appId, privateKeyPath and installationId are required for GitHub repos")
		}
		target = &github.App{AppID: gh.AppID, PrivateKeyPath: gh.PrivateKeyPath, InstallationID: gh.InstallationID}
	}

	box := sandbox.New(cfg)
	if problems := box.Preflight(); len(problems) > 0 {
		fatal("%s", strings.Join(problems, "\n"))
	}
	work, err := os.MkdirTemp("", "agent-cli-")
	if err != nil {
		fatal("%v", err)
	}
	defer os.RemoveAll(work)

	store := pipeline.NewStore()
	w := &pipeline.Worker{Cfg: cfg, Store: store, Notify: console{}, Target: target, Sandbox: box, WorkRoot: work}
	t := store.Create(owner, name, *task)
	fmt.Printf("🤖 Task #%d on %s/%s (AI: %s)\n", t.ID, owner, name, cfg.AI.Provider)

	in := bufio.NewReader(os.Stdin)
	for {
		if guards.KillSwitchOn(cfg.Security.KillSwitchFile) {
			fatal("kill switch is on")
		}
		w.Step(t.ID)
		cur := store.Get(t.ID)
		switch cur.Status {
		case pipeline.AwaitingPlanApproval:
			if *planOnly {
				fmt.Println("\n✅ Plan only — stopping here.")
				return
			}
			next := pipeline.PlanRejected
			if ask(in, "Approve this plan and let the agent make the change?") {
				next = pipeline.PlanApproved
			}
			store.Transition(t.ID, pipeline.AwaitingPlanApproval, next)
		case pipeline.AwaitingImplApproval:
			next := pipeline.ImplRejected
			if ask(in, "Approve these exact changes and push them?") {
				next = pipeline.ImplApproved
			}
			store.Transition(t.ID, pipeline.AwaitingImplApproval, next)
		default:
			if cur.Status.Terminal() {
				if cur.Status != pipeline.Completed {
					fmt.Println("\nStopped:", cur.Status)
					os.Exit(1)
				}
				return
			}
		}
	}
}

func fatal(format string, a ...any) {
	fmt.Fprintf(os.Stderr, "❌ "+format+"\n", a...)
	os.Exit(1)
}
