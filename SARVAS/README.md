# SARVAS

SARVAS is a self-hostable AI coding platform. People who don't write code can
ask questions about a repository, or describe a change in plain English. The
engineers who own the code keep the final say: SARVAS only ever opens a pull
request, and GitHub branch protection decides who can approve and merge it.

- **Ask.** Questions about a repository are answered from a local index of its
  code, docs and dependencies, with file paths and line links. If the code
  doesn't support an answer, SARVAS says so instead of guessing.
- **Request a change.** You describe a feature, bug or UI change and how you'll
  know it's done. The agent asks questions if anything is unclear, then shows a
  plan with acceptance criteria and a risk level. Once the plan is approved
  (by an engineer if it's medium risk), it makes the change in an isolated
  sandbox and runs the repository's checks. You then review the diff. Approving
  the diff opens a pull request that lists the task, changed files, test
  results, risks and limitations.
- **README.** Add or delete a `README.md` with an exact-diff preview. This is
  the original workflow and is unchanged.
- **Dashboard.** Shows your GitHub name, verified email, organizations, role
  and authorized repositories. It also says which AI model is used and whether
  code leaves your servers. Each task has its own page tracking its status,
  and there is an audit log.

It never asks for a GitHub password, never pushes to a default branch, and
can't approve or merge anything.

**Run it:** use [Docker Compose on your own infrastructure](docs/SELF_HOSTING.md)
for everything including Build, or run [locally from source](#run-from-source),
or use the [free Vercel + Render](#deploy-free-page-on-vercel-backend-on-render)
setup (Ask and README only). [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
explains how it works and its security model.

## Quick start (Docker Compose)

```bash
cd SARVAS
python3 scripts/setup.py          # guided: GitHub App, model, storage, roles → .env; then validates
docker compose --profile local-ai up -d --build    # drop the profile if you don't use the bundled Ollama
```

Open the address you configured (default <http://localhost:8765>). See
[docs/SELF_HOSTING.md](docs/SELF_HOSTING.md) for the GitHub App settings,
upgrades, backups, troubleshooting and how to revoke access.

## How it fits together

```
 Browser (React) ──► web: FastAPI ─────────────────────────────┐   all state on your disk:
   dashboard, Ask,      GitHub sign-in (OAuth, as the user)      │   SQLite (sessions, tasks, audit),
   tasks, approvals     RBAC + every repo access re-checked      │   repo checkouts + full-text index
                        with the user's own GitHub token         │
                        local index → retrieval → citations      │
                                │  excerpts / task requests only │
                                │  (never a user token)          │
                                ▼                                │
                     agent: Go agent-service ◄───────────────────┘
                        AI provider: Ollama (self-hosted) | Gemini (only with consent) | sim
                        Build pipeline: plan → approve → implement → checks → approve → PR
                        GitHub App: per-repo token, contents + PRs only; no merge/review API
                                │ mutual TLS
                                ▼
                     sandbox-docker: throwaway containers, read-only root, no capabilities,
                        checks without network; the host Docker socket is never mounted
```

| Part | Stack |
|---|---|
| `client/` | React 19 + Vite. No UI library, strict CSP, and model output is never rendered as HTML |
| `server/` | Python 3.12, FastAPI, httpx, cryptography, stdlib `sqlite3` (FTS5 for the index) |
| `agent/` | Go, **standard library only**: AI providers, guardrails, GitHub App client, sandbox, pipeline |
| `sandbox/` | The sandbox image (agent-runner plus git; add your repositories' toolchains) |
| `deploy/compose/`, `docker-compose.yml` | Self-hosted stack |
| `deploy/render/`, `client/vercel.json` | Free hosted demo (no Build tasks) |

## Roles

| | Requester (anyone allowed in) | Engineer | Admin |
|---|---|---|---|
| Ask, request changes, see own tasks | ✓ | ✓ | ✓ |
| Approve the plan of their own **low-risk** task, and its diff (opens a PR) | ✓ | ✓ | ✓ |
| Approve **medium-risk** plans, decide on anyone's task, see all tasks | | ✓ on repos they can push to | ✓ on repos they can push to |
| Full audit log, kill switch, delete a repo's local index | | | ✓ |
| Approve or merge the pull request | GitHub decides (branch protection) | GitHub decides | GitHub decides |

High-risk requests are declined: anything that touches authentication,
payments, infrastructure, CI or credentials. Configure roles with
`SARVAS_ENGINEERS` / `SARVAS_ADMINS`. Engineer powers on a repository also
need GitHub push access to it.

## Run from source

Prerequisites: Python 3.12 with [uv](https://docs.astral.sh/uv/), Node.js 22+,
Go 1.23+, git 2.31+. Docker is needed only for Build tasks and `agent-cli`.

```bash
cp config.example.json config.json
cp .env.example .env              # fill in GitHub App credentials; AI_PROVIDER=sim to try without a model
make install && make run          # agent-service + FastAPI on http://localhost:8765
```

For hot reload, run `make run-service` and `make run-web`, then
`npm --prefix client run dev`. Set `web.baseUrl` and the App callback to
`http://localhost:5173`.

## agent-cli (terminal pipeline)

```bash
make sandbox-image
./agent/bin/agent-cli -config config.json -repo ./my-project -task "add a --version flag"
./agent/bin/agent-cli -config config.json -repo owner/name  -task "fix the README typo"   # needs github.appId etc.
```

## Deploy free: page on Vercel, backend on Render

The browser talks only to the Vercel address. Vercel forwards `/api/*` and
`/auth/*` to Render, so the sign-in cookie is first-party.

1. **Render** (New → Web Service → Docker): Root Directory `SARVAS`, Dockerfile
   `deploy/render/Dockerfile`, build context `.`, Free instance. Set these
   environment variables: `SARVAS_PUBLIC_URL` (your Vercel address),
   `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `GEMINI_API_KEY` (or
   `AI_PROVIDER=sim`), `WEB_ENCRYPTION_KEY` (`openssl rand -base64 32`),
   `SARVAS_ALLOWED_USERS`, and optionally `SARVAS_ENGINEERS`,
   `SARVAS_ADMINS` and `AI_SEND_CODE=true`. Without `AI_SEND_CODE=true`, Ask
   stays off with Gemini.
2. **Vercel:** point the two rewrites in `client/vercel.json` at your Render
   address, then import the repo with Root Directory `SARVAS/client`.
3. **GitHub App** callback: `https://<your-vercel-address>/auth/callback`.

Limits: Render has no Docker, so **Build tasks are off** there. The free
instance sleeps after 15 minutes, so the first request may time out (reload
once). Its disk is wiped on restart, so sessions, tasks and the index start
fresh.

## Tests and checks

```bash
make test    # go vet + go test, pytest (Python), and a production build of the page
make lint    # gofmt, go vet, ruff, mypy
```

The tests don't need network access, credentials or Docker. They use real git
repositories on disk, a fake GitHub API, a fake sandbox and fake models. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#what-is-tested) for coverage.

## Layout

```
SARVAS/
  client/                 React page (src/pages: Dashboard, Repo [Ask/Build/README], Task, Tasks, Admin, …)
  server/sarvas_web/      FastAPI: app.py (auth, README flow), workflows.py (dashboard, Ask, tasks, admin),
                          indexer.py (local index), rbac.py, store.py, github.py, crypto.py
  agent/                  Go: cmd/{agent-service,agent-runner,agent-cli},
                          internal/{ai,config,guards,gitops,github,pipeline,runner,sandbox,service}
  sandbox/                sandbox image
  deploy/compose/         Compose Dockerfiles + config;  docker-compose.yml at the top level
  deploy/render/          Render image and config
  scripts/setup.py        guided setup + validation
  docs/                   SELF_HOSTING.md, ARCHITECTURE.md
```

## License

Apache License 2.0 — see [LICENSE](../LICENSE)
