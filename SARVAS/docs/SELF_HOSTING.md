# Self-hosting SARVAS

This guide runs the whole platform on a machine you control: the web app, the
agent, the sandbox and, optionally, the AI model. Everything SARVAS stores
stays in one directory on that machine. That covers the database, repository
checkouts and their index, task workspaces and model files.

- [Requirements](#requirements)
- [1. Create the GitHub App](#1-create-the-github-app)
- [2. Run the guided setup](#2-run-the-guided-setup)
- [3. Start, open, sign in](#3-start-open-sign-in)
- [4. Protect your branches](#4-protect-your-branches)
- [Configuration reference](#configuration-reference)
- [Upgrades](#upgrades)
- [Backups and restore](#backups-and-restore)
- [Revoking credentials and deleting data](#revoking-credentials-and-deleting-data)
- [Troubleshooting](#troubleshooting)

## Requirements

| | |
|---|---|
| Docker Engine 24+ (or Docker Desktop) with Compose v2.20+ | `docker compose version` |
| Python 3.9+ on the host | only to run `scripts/setup.py` (standard library only) |
| 2 CPU / 4 GB RAM minimum | 16 GB+ and ideally a GPU if you run a local model with Ollama |
| Outbound HTTPS to `github.com` / `api.github.com` | plus `generativelanguage.googleapis.com` only if you choose Gemini |
| An HTTPS reverse proxy (Caddy, nginx, Traefik…) | if anyone other than you will use it |

## 1. Create the GitHub App

GitHub → **Settings → Developer settings → GitHub Apps → New GitHub App**
(create it under your organization if the repositories belong to one).

| Setting | Value |
|---|---|
| Callback URL | `https://<your SARVAS address>/auth/callback` (or `http://localhost:8765/auth/callback`) |
| Expire user authorization tokens | **On** (8-hour tokens with refresh) |
| Request user authorization during installation | Off |
| Webhook | **Off** |
| Repository permissions | **Contents: Read & write**, **Pull requests: Read & write**, Metadata: Read (automatic) |
| Account permissions | **Email addresses: Read** (only to show verified emails on the dashboard) |
| Everything else | **No access**. SARVAS never needs Administration, Workflows, Actions or Secrets |
| Where can it be installed | Only on this account |

Then:

1. Note the **App ID** and **Client ID**. Generate a **client secret**, and
   generate a **private key** (`.pem`, only needed for Build tasks).
2. **Install App**. Choose **Only select repositories** and pick those SARVAS
   may work on. This installation is the main access boundary: SARVAS can't
   see or touch anything else.

Least privilege in practice:

- **Sign-in** uses user-to-server tokens. Each one can only do what both the
  App and that person are allowed to, only on repositories where the App is
  installed.
- **Build tasks** use installation tokens minted for one repository at a time,
  with only `contents:write` and `pull_requests:write`.
- The agent's GitHub client has an allowlist of API calls. It has no code path
  for merging, reviewing or changing branch protection.

## 2. Run the guided setup

```bash
cd SARVAS
python3 scripts/setup.py
```

It asks for:

- **Public URL.** It shows the exact callback URL to put in the App.
- **GitHub App credentials.** The client secret and API keys are read without
  echo. The private key is copied to `./secrets/github-app.pem` with mode 600
  (that folder is git-ignored and never sent to Docker builds).
- **Who may sign in, plus engineers and admins.** Allowed users are required
  unless the URL is localhost.
- **Optional repository allowlist.**
- **AI model.**
  - `ollama`: self-hosted (recommended). The bundled server runs with
    `--profile local-ai`, or point it at your own.
  - `gemini`: remote. You must explicitly authorize sending code to it.
  - `sim`: offline placeholder.
- **Data directory**, where everything is stored.

It writes `.env` with mode 600. Any previous `.env` is kept as `.env.bak`.
Then it creates the data directories and validates the setup:

- prerequisites (Docker running, Compose version);
- configuration (URL, roles, key formats, storage writable);
- then, inside the real agent container (`agent-service -check`):
  - GitHub accepts the client ID/secret;
  - the App ID + private key authenticate;
  - which accounts the App is installed on, and whether each installation has
    the right permissions (it warns if it has more than needed);
  - the AI model answers, or the Ollama model is pulled (it pulls it for
    you);
  - the sandbox daemon and image work;
  - the work directory is writable.

Re-run the checks at any time with `python3 scripts/setup.py --check`, or
`make check`.

## 3. Start, open, sign in

```bash
docker compose --profile local-ai up -d --build   # without the bundled Ollama: docker compose up -d --build
docker compose ps                                  # web and agent should be "healthy"
```

Open the public URL and click **Connect GitHub**. The dashboard shows your
name, verified emails, organizations, role, the repositories you can use,
which AI model is configured and whether code leaves your infrastructure.

By default the web port binds to `127.0.0.1` only. To serve other people, put
an HTTPS reverse proxy in front of it. Set `SARVAS_PUBLIC_URL` to the
`https://` address, and set `SARVAS_BIND=0.0.0.0` only if the proxy runs on
another host. Example with Caddy:

```
sarvas.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

The proxy must pass the original `Host` header (Caddy and nginx's
`proxy_set_header Host $host` do). SARVAS rejects unknown hosts.

## 4. Protect your branches

Build tasks only open a pull request against a branch that GitHub protects
with a pull-request rule. This is on by default
(`security.requireProtectedBranch`). On each repository, go to **Settings →
Rules → Rulesets** (or **Branches → Branch protection rules**) for the default
branch and enable:

- **Require a pull request before merging**, with at least 1 approval;
- **Require review from Code Owners** (optional; add a `CODEOWNERS` file
  naming your engineers);
- **Require status checks to pass** (your CI);
- **Do not allow bypassing the above settings**;
- no bypass entry for the SARVAS App.

The PR is authored by the App (`<app-name>[bot]`), so it can't count as an
engineer's approval. SARVAS also asks the configured engineers to review. The
agent has no merge or review API at all.

## Configuration reference

Everything is driven by `.env` (read by Compose) and
`deploy/compose/config.json` (shared by both services; `${VAR}` is
substituted from the environment).

| `.env` | Meaning |
|---|---|
| `SARVAS_PUBLIC_URL` | Where people open SARVAS; the OAuth callback is derived from it |
| `SARVAS_BIND`, `SARVAS_PORT` | Host interface/port for the web container (default `127.0.0.1:8765`) |
| `SARVAS_ALLOWED_USERS` | GitHub logins allowed to sign in (required unless localhost); engineers and admins are always allowed |
| `SARVAS_ENGINEERS`, `SARVAS_ADMINS` | Roles (see README → Roles) |
| `SARVAS_ALLOWED_REPOS` | Optional `owner/name,…` allowlist on top of the App installation |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | GitHub App OAuth credentials (sign-in) |
| `GITHUB_APP_ID`, `GITHUB_PRIVATE_KEY_FILE` | Installation access for Build tasks (leave the ID empty to disable Build) |
| `AI_PROVIDER` | `ollama`, `gemini` or `sim` |
| `AI_MODEL` | e.g. `qwen2.5-coder:7b` (Ollama) or `gemini-3.8-flash` |
| `OLLAMA_URL` | `http://ollama:11434` for the bundled server, or your own |
| `GEMINI_API_KEY` | Only for Gemini |
| `AI_SEND_CODE` | `true` authorizes sending repository code (Ask excerpts, Build files) to a **remote** provider. Off by default; never needed for Ollama |
| `SARVAS_DATA_DIR` | Host directory for all state (default `./data`) |
| `AGENT_SERVICE_TOKEN`, `WEB_ENCRYPTION_KEY` | Generated by setup; see [rotation](#revoking-credentials-and-deleting-data) |

In `deploy/compose/config.json`:

- **Per-repository checks.** Under `repos`, a key like `"acme/web"` takes
  `{"lintCommand": "npm run lint", "testCommand": "npm test",
  "extraDenyPaths": ["migrations/**"], "maxFiles": 10, "maxLines": 300}`.
  Checks run in the sandbox without network, so add the toolchains they need
  to `sandbox/Dockerfile`. For example, `apt-get install nodejs npm` or
  `python3-pytest`. Dependencies must be vendored or installable offline.
- **Limits.** `security.maxQuestionsPerHour`, `maxTasksPerHour`, `maxFiles`,
  `maxChangedLines`, `protectedPaths`, and `agent.*` timeouts and token
  budgets.
- **Index.** `index.maxFileKb` and `index.maxFiles`.

**Database.** SARVAS uses SQLite (WAL mode) at
`$SARVAS_DATA_DIR/web/web.db`. That's fine for a team-sized installation on
one host. External databases (PostgreSQL) are **not supported** yet.

### Where data lives

| Path under `SARVAS_DATA_DIR` | Contents | Sensitive? |
|---|---|---|
| `web/web.db` | sessions (session-ID hashes, AES-256-GCM-encrypted GitHub tokens), tasks, audit log | yes |
| `web/repos/<owner>/<repo>/` | shallow checkout + `index.db` (full-text index), one directory and database per repository | yes (source code) |
| `work/` | per-task checkouts while a task runs; deleted after each phase | yes, transient |
| `control/KILL_SWITCH` | exists = agent paused | no |
| `ollama/` | downloaded model weights | no |

`secrets/github-app.pem` (outside the data directory) is the App's private
key.

## Upgrades

```bash
git pull                                   # or check out a release tag
python3 scripts/setup.py --check           # new settings show up as warnings/errors
docker compose --profile local-ai up -d --build
docker compose exec sandbox-docker docker image prune -f   # optional: old sandbox images
```

The database schema is created and extended automatically on start, using
`CREATE TABLE IF NOT EXISTS`. Tasks still running in the agent are lost when
it restarts. Their pages show "lost", nothing is changed on GitHub, and the
requester can start them again. Back up first (below) before major upgrades.

## Backups and restore

Back up `SARVAS_DATA_DIR/web` and `.env`. Also back up
`secrets/github-app.pem`, unless you'd rather generate a new key. Everything
else can be rebuilt: repository indexes re-clone, and work directories are
temporary.

```bash
# consistent online copy of the database
docker compose exec web python -c "import sqlite3; s=sqlite3.connect('/data/web/web.db'); d=sqlite3.connect('/data/web/backup.db'); s.backup(d)"
tar czf sarvas-backup-$(date +%F).tgz .env secrets/ "${SARVAS_DATA_DIR:-./data}/web/backup.db"
```

To restore: stop the stack and put `.env`, `secrets/` and the database back
(`backup.db` → `web/web.db`). Then start the stack. The encrypted tokens in
the database can only be read with the same `WEB_ENCRYPTION_KEY`. Without it,
people just sign in again.

Treat backups like the live data: they contain encrypted tokens and audit
history.

## Revoking credentials and deleting data

| To… | Do this |
|---|---|
| Stop the agent immediately | Admin page → **Pause the agent**, or `touch $SARVAS_DATA_DIR/control/KILL_SWITCH`. New tasks, approvals and PRs are refused |
| Sign one person out everywhere | They click **Disconnect** (revokes their token at GitHub), or revoke at GitHub → Settings → Applications → Authorized GitHub Apps |
| Remove a person | Delete them from `SARVAS_ALLOWED_USERS` / roles and restart `web`; their next request is refused and their session deleted |
| Cut off a repository | Remove it from the App installation (GitHub → Settings → Applications → Installed GitHub Apps → Configure), or from `SARVAS_ALLOWED_REPOS`; then an admin uses `POST /api/admin/repos/<owner>/<name>/forget` (or delete `web/repos/<owner>/<repo>/`) |
| Revoke all sign-ins | GitHub App settings → Advanced → **Revoke all user tokens**; then delete `web/web.db` (or rotate `WEB_ENCRYPTION_KEY`) to drop stored tokens. If the client secret may have leaked, generate a new one, delete the old one, update `GITHUB_CLIENT_SECRET` and restart |
| Revoke Build access | Delete the App's private key in its settings, generate a new one, replace `secrets/github-app.pem`, and restart `agent` |
| Rotate internal secrets | New `AGENT_SERVICE_TOKEN` (`openssl rand -hex 32`); new `WEB_ENCRYPTION_KEY` (`openssl rand -base64 32`; everyone signs in again); restart |
| Rotate the Gemini key | Revoke it in Google AI Studio, set the new `GEMINI_API_KEY`, restart `agent` |
| Uninstall completely | `docker compose --profile local-ai down -v`, delete `SARVAS_DATA_DIR`, `.env*` and `secrets/`, then uninstall the App from every account and delete it |

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| GitHub says *redirect_uri is not associated* | The App's callback URL must be exactly `SARVAS_PUBLIC_URL` + `/auth/callback` |
| "The sign-in link expired" after GitHub | The callback reached a different address than the page (check `SARVAS_PUBLIC_URL`), or cookies are blocked |
| `Bad host` | The proxy isn't forwarding the original `Host` header, or `SARVAS_PUBLIC_URL` doesn't match the address people use |
| Web container exits: *allowedUsers … must list* | Set `SARVAS_ALLOWED_USERS` (required for non-localhost URLs) |
| `Permission denied` under `/data` (Linux) | The containers run as uid 1000: `sudo chown -R 1000:1000 $SARVAS_DATA_DIR/{web,work,control}` |
| Dashboard: *Build tasks: Off* | The reason is shown. Usually `GITHUB_APP_ID` or the key is missing, the sandbox image isn't built (`docker compose logs sandbox-image`), or Gemini lacks `AI_SEND_CODE=true` |
| Ask: *sending repository code … is NOT authorized* | You're using Gemini without `AI_SEND_CODE=true`. Authorize it, or switch to Ollama |
| Ask keeps saying "reading the repository" | First index of a large repo. Watch `docker compose logs web`. Very large repos hit `index.maxFiles` (raise it) |
| Ask says "outside the available context" | Nothing in the indexed files matches. Rephrase using names from the code, or the answer really isn't in this repository |
| Task declined: *branch isn't protected* | Add the rule in [step 4](#4-protect-your-branches), or (not recommended) set `security.requireProtectedBranch` to `false` |
| Task fails: *checks failed* | The repo's `lintCommand`/`testCommand` failed in the sandbox. Often the sandbox image lacks the toolchain, or the tests need network |
| Task page says *lost* | The agent restarted mid-task. Start it again |
| `agent-service -check`: installation missing permissions | Edit the App's permissions, then accept the new permissions on each installation (GitHub asks the owner) |
| Ollama is slow or times out | Use a smaller model, give Docker more RAM/GPU, or raise `agent.planTimeoutS` / `implementTimeoutS` |
| Something else | `docker compose logs web agent sandbox-docker`. Tokens and keys are redacted from logs. Run `python3 scripts/setup.py --check` |
