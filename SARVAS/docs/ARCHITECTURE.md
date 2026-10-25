# SARVAS architecture and security model

## Starting point (audit, before this change)

| Area | What existed | Kept / changed |
|---|---|---|
| `server/` (FastAPI) | GitHub App OAuth sign-in, encrypted sessions, CSRF/Origin/Host checks, README add/delete with an exact-diff preview → PR, audit log, rate limits, kill switch | **Kept unchanged** (README flow, API contract). The repo page now also opens for read-only users, because Ask/Build don't need write access. README changes still require it |
| `agent/` (Go, stdlib only) | agent-service (README draft/check), and a full **plan → approve → implement → approve → PR** pipeline with a hardened Docker sandbox, guardrails and a GitHub App client. **Only reachable from the terminal** (`agent-cli`) | **Reused** as the Build engine behind new agent-service endpoints |
| AI | Gemini or offline `sim` | Added self-hosted **Ollama** and an explicit consent switch for sending code to a remote provider |
| Repository understanding | Root file names only | Added a **local index** (checkout + SQLite FTS5) with an architecture/dependency overview |
| Roles | Login allowlist only | Added **requester / engineer / admin** |
| Deployment | `make run`, Render + Vercel | Added **Docker Compose**, a guided setup and a validator |

Implementation plan (followed in this order):

1. Agent side: providers (Ollama), consent rules, a per-repo GitHub App client
   with an API allowlist and a branch-protection check, pipeline additions
   (acceptance criteria, base branch, PR body, reviewers, kill switch), and
   service endpoints (`/v1/ask`, `/v1/tasks`, `/v1/capabilities`,
   `-check`).
2. Web side: RBAC, local indexer, dashboard/Ask/task/admin routes, durable
   task records, log redaction.
3. Page: dashboard, repository tabs (Ask, Request a change, README), task
   page, task list, admin page.
4. Docker Compose with an isolated sandbox daemon, setup wizard, docs.
5. Tests at each layer; lint and type checks.

## End-to-end workflow

```
sign in (GitHub App OAuth, user-to-server token, encrypted at rest)
  → dashboard: identity, verified email, orgs, role, repos, AI disclosure
  → pick a repository  (every request re-checks access with the user's token)
  → Ask:  index current? (compare GitHub head SHA; incremental re-index if not)
          → full-text retrieval → too weak? "outside the available context" (no model call)
          → model answers from excerpts only, as JSON with citations
          → citations outside the given excerpts are dropped; none left → "not confident", no answer
  → Build: request + acceptance criteria
          → agent clones fresh (installation token, one repo), plans read-only in the sandbox
          → unclear? questions → requester answers → new task
          → high risk / protected paths → declined
          → scope approval: requester (low risk) or engineer with push (medium)
          → implement in the sandbox (no code execution by the agent)
          → host re-inspects the worktree: policy limits, secret scan, protected paths
          → repo's lint/test commands in a network-less container
          → requester/engineer reviews the exact diff (SHA-256 pinned)
          → base branch must be protected by a PR rule
          → fresh clone, apply the approved diff, re-check, commit, push a NEW branch
          → open PR (task, criteria, files, test results, risks, limitations), request engineer reviewers
  → engineers review and merge on GitHub (SARVAS has no merge or review API)
```

## Components and trust boundaries

| Component | Holds | Never holds |
|---|---|---|
| Browser | HttpOnly session cookie, CSRF token | GitHub tokens, model output as HTML |
| `web` (FastAPI) | encrypted user tokens, SQLite, repository checkouts + index | the GitHub App private key |
| `agent` (Go) | App private key, AI key, task state (memory) | any user's GitHub token |
| sandbox containers | one task's checkout (read-only `.git`), the AI key while the model runs | GitHub credentials; network during checks |
| `sandbox-docker` | runs only sandbox containers | the host's Docker socket (it isn't mounted anywhere) |

## Security controls (new)

| Control | Where |
|---|---|
| Roles. Engineer powers need the role **and** GitHub push access to that repository | `server/sarvas_web/rbac.py` |
| Requesters can approve only their own **low-risk** plans; medium risk needs an engineer; high risk is declined by the pipeline | `rbac.py`, `pipeline.plan` |
| Task visibility: the requester, or engineers with push on that repo. Access is re-verified with GitHub on every view, and losing access hides the task | `workflows.load_task` |
| Agent's GitHub client: allowlist of 8 REST calls. Merge, reviews, branch-protection edits etc. are refused before any request is sent | `agent/internal/github` |
| Installation lookup per repository; tokens cached per `owner/name` and scoped to one repo with `contents` + `pull_requests` write only | `github.RepoToken` |
| PRs only against branches protected by a pull-request rule (classic protection or rulesets); on by default | `pipeline.createPR` |
| The PR is authored by the App bot; engineers are requested as reviewers; the requester's name is neutralized (no @-ping) | `pipeline.PRBody` |
| Remote model gets repository code only with `ai.sendCodeToProvider=true`. Enforced in **both** services; disclosed on the dashboard and on every answer | `config.AI.CodeAllowed`, `workflows.disclosure` |
| Index never contains protected paths (`.env*`, keys, `secrets/`…), symlinks, binaries or vendored dirs; secret-looking values are redacted | `indexer.py` |
| Git runs with hooks off, `protocol.allow=never` except https, symlinks checked out as plain files, credentials only via `GIT_CONFIG_*` env (never argv, URL or `.git/config`); git errors scrubbed of tokens | `indexer._git`, `agent/internal/gitops` |
| Per-repository directories and databases (`<owner>/<repo>/index.db`); names validated against path traversal | `indexer.RepoIndex` |
| Answers: citations must point inside the excerpts the model was given; weak retrieval never reaches the model | `workflows.ask` |
| Log filter redacts GitHub tokens, API keys and `Bearer`/`basic` credentials; httpx request logging off; no access log | `server.RedactSecrets` |
| Audit trail: sign-ins, questions (first 200 chars), index runs, task creation, every status change, plan/diff decisions (with diff SHA-256), refused decisions, PR URLs, kill switch. Admins see everyone's | `store.record`, Admin page |
| Kill switch shared by both services: blocks new tasks, approvals and pipeline steps | `control/KILL_SWITCH` |
| Compose: non-root, read-only root filesystems, all capabilities dropped, `no-new-privileges`; web bound to 127.0.0.1 by default; the sandbox image is built from `agent/` + `sandbox/` only | `docker-compose.yml` |

## What leaves your infrastructure

| Data | Destination | When |
|---|---|---|
| OAuth codes and tokens, API calls as the user | GitHub | always (it's the source of truth) |
| Branch + PR from Build tasks | GitHub (the repository itself) | after a person approves the diff |
| Retrieved excerpts + question (Ask), files the agent reads (Build), root file names (README draft) | the AI provider | **Ollama/sim: never leaves.** Gemini: Ask/Build only if `AI_SEND_CODE=true`; README drafts send top-level file names and the description |
| Nothing else | — | No telemetry, no analytics, no third-party scripts |

## What is tested

`make test` and `make lint` passed for this change. The suites cover:

| Area | Tests |
|---|---|
| Authentication | OAuth state/CSRF/Origin/Host checks, session rotation, token revocation, login allowlist (`server/tests/test_app.py`, existing) |
| Dashboard | identity, verified-only emails, orgs, role, disclosure; email permission not granted (`test_workflows.py`) |
| Indexing | full index; secrets/protected files excluded and redacted; incremental update (changed + deleted files); unchanged short-circuit; results re-read from current files; symlinks not followed |
| Repository isolation | separate directories/DBs per `owner/name`; no cross-repo hits; traversal names and malicious branch names rejected; users without GitHub access refused; tasks hidden from other users and after access is revoked |
| Out-of-context requests | unrelated and vague questions answered honestly **without** calling the model; model "not answerable"; ungrounded answers withheld; fabricated citations dropped; index refreshed after a push |
| Coding workflow | full task lifecycle over HTTP against the real Go service with a real git origin and fake sandbox (`agent/internal/service/tasks_test.go`); acceptance criteria merged; clarification creates a new task; lost tasks after restart; Build unavailable / kill switch |
| PR creation | exact approved diff pushed; unprotected base branch refused; PR body has task, criteria, files, test results, risks, limitations; reviewers requested (`pipeline_test.go`) |
| Approval permissions | RBAC matrix; requester can't approve medium risk; engineer needs push; outsiders can't decide; admin-only audit and kill switch; refused decisions audited |
| Agent can't merge/approve | merge, review, protection and traversal calls refused before sending (`github_test.go`) |
| Providers and privacy | Ollama/Gemini request shapes and errors; remote provider without consent refused (Go) and disclosed (web); prompt-injection tags neutralized |
| Secrets in logs | log filter redacts token shapes |

Plus a live contract run of the Python client against the built Go
`agent-service` (capabilities, ask, task 503/404, auth 401).

## Known limitations and remaining work

These are not done. Some are blocked by what was available while building
this.

- **Not exercised against real services.** No real GitHub App, Docker,
  Ollama or Gemini was available, so the following are covered by fakes and
  unit tests only, and have **not been run end to end**: the Compose stack,
  the Docker-in-Docker sandbox, the TLS cert hand-off, cloning private repos,
  real PR creation, reviewer requests, branch-protection detection on real
  rulesets, and the `-check` validator against GitHub. Run
  `python3 scripts/setup.py` on a machine with Docker to validate.
- **Task state in the agent is in memory.** A restart loses in-flight tasks.
  They are marked "lost" and nothing is half-applied, but they don't resume.
- **Retrieval is lexical** (SQLite FTS5 + identifier splitting), not
  embeddings. Questions that share no words with the code can come back as
  "outside the available context". Adding local embeddings (e.g. Ollama
  `nomic-embed-text`) would fix that.
- **Indexing is per default branch only**, and capped by `index.maxFiles`
  and `maxFileKb`.
- **SQLite only**, single host. There is no PostgreSQL or HA option.
- **Docker-in-Docker is privileged.** It isolates sandboxes from the host's
  Docker daemon, but a kernel escape from a sandbox would reach the dind
  container. For stronger isolation, run `sandbox-docker` with gVisor
  (`runsc`) or on a dedicated VM.
- **Checks run offline** with whatever toolchain the sandbox image has. Repos
  that need network to install dependencies must vendor them or use a derived
  image.
- **Roles come from configuration.** There's no UI for managing them and no
  mapping from GitHub teams yet.
- **The model only reads files during planning.** It can't run code to
  confirm behaviour. Checks are the configured commands only.
- **Render/Vercel demo**: no Build tasks (no Docker), and an ephemeral disk.
