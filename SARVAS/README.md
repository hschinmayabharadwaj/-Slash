# SARVAS

SARVAS is a security-hardened AI coding agent. It runs on your own machine,
or for free with the page on Vercel and the backend on Render. You open the
web page and connect your GitHub account. Then you pick a repository and ask
for a change. The page shows you the exact diff, and once you approve it, it
opens a pull request. It never pushes to your default branch.

For now the page supports two changes:

- **Add a `README.md`.** You write it, or the AI drafts it and you edit it.
- **Delete a `README.md`.**

Both work only on repositories where GitHub confirms you have write access.

## Architecture

```
 Browser (React SPA) ─── http://localhost:8765 ───► server/ (Python + FastAPI)
                                                      │  GitHub sign-in (GitHub App, OAuth web flow)
                                                      │  encrypted sessions/tokens, proposals, audit (SQLite)
                                                      │  README actions via the GitHub API, as YOU
                                                      │
                                                      │  repo facts + proposed content only
                                                      │  (never your GitHub token), bearer-token auth
                                                      ▼
                                          agent/ agent-service (Go, 127.0.0.1:8766)
                                                      │  AI README drafts (Gemini REST, or offline "sim")
                                                      │  content checks (secret scan, size)
                                                      ▼
                                              Gemini API

 agent/ agent-cli (Go) ── general plan → approve → implement → approve → PR pipeline
        └── runs agent/ agent-runner (Go) inside a hardened Docker sandbox per step
```

| Part | Stack | Why |
|---|---|---|
| `client/` | React 19 + Vite | The page. No router or UI library; strict CSP, no inline scripts |
| `server/` | Python 3.10+ and FastAPI (deps: `fastapi`, `uvicorn`, `httpx`, `cryptography`, all pinned) | GitHub OAuth, sessions and API. Storage is the standard-library `sqlite3`; deploys free on Render (page on Vercel) |
| `agent/` | Go, **standard library only** (zero third-party modules) | AI calls, guards, Docker sandbox, git and the pipeline. Builds to static binaries, so the sandbox image stays small |

## Safety controls

**Web page**

| Control | Where |
|---|---|
| Fixed list of allowed actions (add or delete README only) | `server/sarvas_web/actions.py` |
| Write access checked with GitHub for every action; optional repo allowlist; archived repos refused | `actions.authorize` |
| Preview → approve. Approval applies the stored preview by ID and fingerprint, can only run once, and expires after 1 h | `store.py`, `app.py` |
| Repo re-checked at approval; refused if README changed since the preview | `actions.apply` |
| New branch and PR only, never the default branch; the branch is deleted if anything fails | `actions.apply` |
| GitHub App user-to-server tokens: limited to the App's permissions **and** yours, only on repos where the App is installed, expire after 8 h | `github.py` |
| Tokens encrypted with AES-256-GCM; key file `0600` (or `WEB_ENCRYPTION_KEY`); DB stores only a hash of the session ID; session rotated at sign-in; token revoked at sign-out | `crypto.py`, `store.py` |
| OAuth `state` compared in constant time; every mutation needs the CSRF header, a JSON body and a same-origin `Origin`; HttpOnly SameSite cookie | `app.py` |
| Host-header allowlist (blocks DNS rebinding), loopback bind, strict CSP, `frame-ancestors 'none'`, `no-referrer` | `app.py` |
| Allowlist of GitHub logins (`security.allowedUsers`), **required** whenever the page isn't on localhost; checked at sign-in and on every request | `config.py`, `app.py` |
| The Go service never sees GitHub tokens; it gets only repo facts and proposed content, behind a 32+ character bearer token, on loopback | `agent_client.py`, `internal/service` |
| Secret scan of README content (fails closed if the service is down); per-user hourly limits on changes and AI drafts; kill switch; no access log (the OAuth code stays out of logs); audit log (Activity page) | |

**Agent pipeline (`agent-cli`)**

| Control | Where |
|---|---|
| Two human approvals: the plan, then the exact diff (SHA-256). The agent is never re-run after approval | `internal/pipeline` |
| The agent can't run code. Its tools are list, read, write and delete, confined to the repo; `.git`, protected paths, symlink escapes and absolute paths are refused | `internal/runner` |
| Containers: read-only root, `--cap-drop ALL`, `no-new-privileges`, pid/memory/CPU limits, timeout. Checks always run with `--network none` | `internal/sandbox` |
| No GitHub credentials in containers. Installation tokens are scoped to one repo and passed via `GIT_CONFIG_*`, never in URLs or `.git/config` | `internal/github`, `internal/gitops` |
| Policy limits (files, lines, size, symlinks, binaries, protected paths) and a secret scan, checked again before commit | `internal/guards` |

## Setup

Prerequisites: Python ≥ 3.10 with [uv](https://docs.astral.sh/uv/), Node.js ≥ 22 (only to build the page),
Go ≥ 1.23, git ≥ 2.31. Docker is needed only
for `agent-cli`.

1. **Create a GitHub App** (GitHub → Settings → Developer settings → GitHub Apps → New):
   - **Callback URL:** `http://localhost:8765/auth/callback`
   - Keep *Expire user authorization tokens* **on**. Webhook: off.
   - **Repository permissions:** *Contents: Read and write*, *Pull requests: Read and write*.
   - Generate a **client secret** and note the **Client ID**, then **install** the App on your repos.

2. **Configure:**

   ```bash
   cp config.example.json config.json
   cp .env.example .env        # fill in GITHUB_CLIENT_ID/SECRET, GEMINI_API_KEY, and:
   openssl rand -hex 32        # → AGENT_SERVICE_TOKEN
   ```

   Set `"ai": {"provider": "sim"}` to try everything without an AI key.

3. **Install, build, run:**

   ```bash
   make install
   set -a; source .env; set +a
   make run                    # starts agent-service + FastAPI server
   ```

   Open <http://localhost:8765> and click **Connect GitHub**.

**Development with hot reload:** run `make run-service` and `make run-web`,
then `npm --prefix client run dev`. Set `web.baseUrl` (and the App's
callback URL) to `http://localhost:5173`.

## agent-cli (free-form changes, sandboxed)

```bash
make sandbox-image
./agent/bin/agent-cli -config config.json -repo ./my-project -task "add a --version flag"
./agent/bin/agent-cli -config config.json -repo owner/name  -task "fix the README typo"   # needs github.appId etc.
```

For a local checkout, the CLI pushes the result as a new branch into that
repo. For GitHub repos, it opens a pull request. Lint and test commands come
only from `repos.<owner/name>` in `config.json` and run network-less. Add the
toolchains they need to the sandbox image.

## Deploy free: page on Vercel, backend on Render

```
 Browser ── https://sarvas.vercel.app ──► Vercel (React page, static)
                                            │  /api/* and /auth/* are rewritten (proxied) to:
                                            ▼
                                 Render (FastAPI + Go agent-service, Docker)
```

The browser only ever talks to the Vercel address, so the sign-in cookie is
first-party and the CSRF and Origin checks work unchanged. GitHub sign-in
also returns to the Vercel address.

### 1. Backend on Render

1. Push this repo to GitHub (Render and Vercel both deploy from a branch).
2. <https://dashboard.render.com> → **New → Web Service** → connect the repo:

   | Setting | Value |
   |---|---|
   | Language | **Docker** |
   | Branch | the branch with SARVAS (e.g. `main`) |
   | Root Directory | `SARVAS` |
   | Dockerfile Path (Advanced) | `deploy/render/Dockerfile` |
   | Docker Build Context Directory (Advanced) | `.` |
   | Instance Type | **Free** |

3. Environment variables:

   | Name | Value |
   |---|---|
   | `SARVAS_PUBLIC_URL` | your Vercel address, e.g. `https://sarvas.vercel.app` |
   | `GITHUB_CLIENT_ID` | from the GitHub App |
   | `GITHUB_CLIENT_SECRET` | from the GitHub App |
   | `GEMINI_API_KEY` | your key (or set `AI_PROVIDER=sim`) |
   | `WEB_ENCRYPTION_KEY` | `openssl rand -base64 32` |
   | `SARVAS_ALLOWED_USERS` | comma-separated GitHub logins allowed to sign in (required) |

   Render sets `PORT` and `RENDER_EXTERNAL_HOSTNAME` itself, and
   `AGENT_SERVICE_TOKEN` is generated fresh at every start. Note the
   service's address, e.g. `https://sarvas.onrender.com`; opening it shows
   "SARVAS API is running".

### 2. Page on Vercel

1. In `client/vercel.json`, replace `sarvas.onrender.com` (both rewrites) with
   your Render address, then commit and push.
2. <https://vercel.com/new> → import the repo → set **Root Directory** to
   `SARVAS/client`. Vercel reads the framework, build command and output
   folder from `vercel.json`. Deploy.
3. If the Vercel address differs from what you put in `SARVAS_PUBLIC_URL`,
   update that variable on Render.

### 3. GitHub App

Set the callback URL to `https://<your-vercel-address>/auth/callback`
(you can keep `http://localhost:8765/auth/callback` as a second one).

### Free-tier limits

The Render service sleeps after 15 minutes without traffic and takes about a
minute to wake; the first request may time out at Vercel, so reload once.
Render's disk isn't persistent: a restart or redeploy clears sessions,
previews and the activity log. Nothing on GitHub is lost; you just connect
again.

## Tests

```bash
make test        # go vet + go test ./... and the FastAPI tests (pytest)
```

The Go pipeline tests use real git with a bare local origin, plus a fake
sandbox and target. The FastAPI tests run the app against an in-memory fake
of the GitHub API. Neither needs network access or credentials.

## Layout

Everything lives in the `SARVAS/` folder, one subfolder per job:

```
SARVAS/
  client/                     web page: React + Vite (src/pages/, vercel.json)
  server/                     web backend: Python + FastAPI (sarvas_web/, tests/)
  agent/                      AI agent: Go module, stdlib only
    cmd/agent-service/          localhost service for the web server
    cmd/agent-runner/           in-sandbox agent
    cmd/agent-cli/              terminal pipeline
    internal/{ai,config,guards,gitops,github,runner,sandbox,pipeline,service}
  sandbox/                    Docker sandbox image (Dockerfile + its .dockerignore)
  deploy/render/              backend on Render: Dockerfile, config, app.py (starts agent-service + FastAPI)
  docs/                       notes and session logs
  data/                       local state: database, key file, kill switch (git-ignored)
  config.example.json         shared by Go and Python → copy to config.json
  .env.example                secrets → copy to .env
  Makefile                    install / build / test / run
```

Run every command from inside `SARVAS/`.

## License

Apache License 2.0 — see [LICENSE](../LICENSE)
