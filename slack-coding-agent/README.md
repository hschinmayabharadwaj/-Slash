# Coding Agent

A security-hardened AI coding agent that runs entirely on your own machine.
You open a local web page and connect your GitHub account. Then you pick a
repository and ask for a change. The agent shows you the exact diff first, and
once you approve, it opens a pull request. It never pushes to your default
branch.

For now the page supports two changes:

- **Add a `README.md`.** You write it, or the AI drafts it and you edit it.
- **Delete a `README.md`.**

Both work only on repositories where GitHub confirms you have write access.

## How it works

```
 Browser ── http://localhost:8765 (loopback only)
   │ 1. "Connect GitHub" → GitHub's consent screen → back with a code
   ▼
 Web app (Flask + waitress)
   │ 2. exchanges code for a user token (encrypted at rest, auto-refreshed)
   │ 3. lists repos where the App is installed AND you have access
   │ 4. you choose: add README (write it or "Draft with AI") / delete README
   │ 5. authorize: allowlisted repo, not archived, GitHub says you can push
   │ 6. PREVIEW: exact diff shown, nothing changed yet
   │        ── you click Approve ──
   │ 7. re-check repo is unchanged since preview → new branch → commit → PR
   ▼
 GitHub ── pull request opened as you (with the App badge); you merge it
```

## Why a GitHub App (not a personal token or OAuth App)

Signing in gives the page a **user-to-server token**. Its permissions are
limited to what **both** the App was granted (*Contents* and *Pull requests*)
**and** you can do yourself, and only on repos where you installed the App. A
classic OAuth App would need the `repo` scope, which means full access to every
private repo you can see. User-to-server tokens also expire after 8 hours.

## Safety controls

| Control | Where |
|---|---|
| Fixed list of allowed actions (add or delete README only); everything else is refused | `repo_actions.ACTIONS` |
| Write access checked with GitHub for every action; optional repo allowlist | `repo_actions.authorize` |
| Preview → approve. Approval applies the stored preview by ID and fingerprint, can only run once, and expires after 1 h | `web_store`, `webapp.approve` |
| Repo re-checked at approval; refused if README changed since the preview | `repo_actions.apply` |
| Changes go on a new branch with a PR, never to the default branch; branch is cleaned up on failure | `repo_actions.apply` |
| Secret scan of README content; size limit | `guards.scan_secrets` |
| Tokens encrypted at rest (Fernet; key file `0600`); DB stores only a hash of the session ID; session rotated at sign-in; token revoked at sign-out | `web_store`, `webapp` |
| OAuth `state` checked in constant time; CSRF token and Origin check on every POST; HttpOnly SameSite cookie | `webapp` |
| Loopback bind + Host-header check (blocks DNS rebinding); strict CSP, no JavaScript, no framing, `no-referrer` | `webapp` |
| AI output is treated as untrusted: the prompt marks repo info as data, and the page escapes the output; you edit the draft before approving | `repo_actions.draft_readme` |
| Per-user hourly limit; kill switch (`touch data/KILL_SWITCH`) | `security.*` |
| Audit log of every sign-in, proposal, refusal, and approval (Activity page) | `audit_events` |

## Setup

1. **Create a GitHub App** (GitHub → Settings → Developer settings → GitHub Apps → New):
   - **Callback URL:** `http://localhost:8765/auth/callback`
   - Leave *Expire user authorization tokens* **on**. Webhook: off.
   - **Repository permissions:** *Contents: Read and write*, *Pull requests: Read and write*.
   - Create it, then **generate a client secret** and note the **Client ID**.
   - **Install** the App on the repositories the agent may touch.

2. **Install and configure:**

   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -e ".[gemini]"          # drop [gemini] if you only use ai.provider: sim
   cp config.yaml.example config.yaml
   cp .env.example .env                # set GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET (+ GEMINI_API_KEY)
   ```

3. **Run:**

   ```bash
   coding-agent                        # or: python -m slackagent.web
   ```

   Open <http://localhost:8765> and click **Connect GitHub**.

Set `ai.provider: sim` to try everything without an AI key. "Draft with AI"
then produces a template README from the repo's file list.

## Other interfaces (optional)

The repository also contains a more general agent that plans and makes
free-form code changes. It runs in hardened Docker sandboxes with two human
approvals, and you reach it from:

- **CLI:** `slack-coding-agent-cli --repo ./my-project --task "add a --version flag"`
  (needs Docker and `docker build -t slack-coding-agent-sandbox:latest .`)
- **Slack:** `pip install -e ".[slack]"`, add a `slack:` section, then run
  `python -m slackagent.main`

This path is not connected to the web page yet; the page's actions are
deliberately limited to the README operations.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The web tests run against an in-memory fake of the GitHub API, and the
pipeline tests use real git with a local bare repo. No network or credentials
are needed.

## Project layout

```
src/slackagent/
  web.py            web entry point (primary)
  webapp.py         routes, sessions, CSRF/Host/CSP protections
  github_user.py    GitHub OAuth web flow + API calls as the signed-in user
  repo_actions.py   allowed actions: authorize → propose → apply; AI README draft
  web_store.py      encrypted sessions/tokens, pending proposals
  templates/        HTML pages (server-rendered, no JS)
  database.py       SQLite task store + audit log
  config.py         configuration
  guards.py         secret scan, sanitizing, kill switch
  # general agent (CLI / Slack, Docker sandbox):
  cli.py  main.py  slack_handler.py  worker.py  agent_runner.py  sandbox.py  github_ops.py
```

## License

MIT
