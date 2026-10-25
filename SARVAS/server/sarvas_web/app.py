"""FastAPI app: GitHub sign-in, JSON API for the React page, static SPA.

Protections on top of GitHub's own permission checks:
 - rejects unexpected Host headers (DNS rebinding)
 - optional allowlist of GitHub logins (required when the page is public)
 - HttpOnly SameSite=Lax session cookie; mutations need the per-session
   X-CSRF-Token header, a JSON content type and a same-origin Origin
 - OAuth state checked in constant time; session ID rotated at sign-in
 - strict CSP, no framing, no-referrer, no caching of API responses
 - every proposal / approval / refusal goes to the audit log
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse

from . import workflows
from .actions import ACTIONS, README, ActionRefused, apply, authorize, fingerprint, propose
from .agent_client import AgentServiceError
from .config import user_allowed
from .crypto import random_token, safe_equal
from .github import GitHubError, GitHubOAuth, GitHubUser, token_expired
from .indexer import Indexes
from .rbac import role_of

log = logging.getLogger("sarvas")

COOKIE = "agent_session"
MAX_BODY = 128 * 1024
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data: https://avatars.githubusercontent.com; connect-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str, signed_out: bool = False):
        super().__init__(message)
        self.status, self.signed_out = status, signed_out


@dataclass
class User:
    id: str
    session: dict
    login: str
    gh: GitHubUser


def _error(status: int, message: str, signed_out: bool = False) -> JSONResponse:
    return JSONResponse({"error": message, **({"signedOut": True} if signed_out else {})}, status_code=status)


def create_app(config: dict, store, agent, http: httpx.Client | None = None, indexes: Indexes | None = None) -> FastAPI:
    http = http or httpx.Client(timeout=30)
    indexes = indexes or Indexes(config)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    web = config["web"]
    base = urlparse(web["baseUrl"])
    secure = base.scheme == "https"
    allowed_hosts = {base.netloc, f"127.0.0.1:{web['port']}", f"localhost:{web['port']}", *web["extraHosts"]}
    oauth = GitHubOAuth(config["github"]["clientId"], config["github"]["clientSecret"],
                        f"{web['baseUrl'].rstrip('/')}/auth/callback", http)

    # ---------------------------------------------------------------- plumbing

    @app.middleware("http")
    async def plumbing(request: Request, call_next):
        if request.headers.get("host") not in allowed_hosts:
            return PlainTextResponse("Bad host", status_code=400)
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled error")
            response = _error(500, "Internal error.")
        response.headers.update(SECURITY_HEADERS)
        if request.url.path == "/api" or request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def set_cookie(response: Response, sid: str) -> None:
        response.set_cookie(COOKIE, sid, httponly=True, samesite="lax", secure=secure, path="/",
                            max_age=int(web["sessionHours"] * 3600))

    def session_of(request: Request) -> tuple[str | None, dict | None]:
        sid = request.cookies.get(COOKIE)
        return sid, store.get_session(sid)

    def signed_in(request: Request) -> User:
        """Require a signed-in (and still allowed) user. Refreshes expired tokens."""
        sid, session = session_of(request)
        tokens = store.tokens(session) if session and session.get("login") else None
        if not tokens or not session or not sid:
            raise ApiError(401, "Not connected to GitHub.", True)
        if not user_allowed(config, session["login"]):
            store.delete_session(sid)
            raise ApiError(401, "This GitHub account isn't allowed to use this page.", True)
        if token_expired(tokens):
            if not tokens["refreshToken"]:
                store.delete_session(sid)
                raise ApiError(401, "Your GitHub session expired.", True)
            tokens = oauth.refresh(tokens["refreshToken"])
            store.update_tokens(sid, tokens)
        return User(sid, session, session["login"], GitHubUser(tokens["accessToken"], http))

    async def csrf(request: Request) -> dict:
        """Same-origin JSON request with the session's CSRF token. Returns the parsed body."""
        if origin := request.headers.get("origin"):
            if urlparse(origin).netloc not in allowed_hosts:
                raise ApiError(403, "Cross-origin request refused.")
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise ApiError(415, "Expected JSON.")
        try:
            declared = int(request.headers.get("content-length") or 0)
        except ValueError:
            declared = 0
        raw = b"" if declared > MAX_BODY else await request.body()
        if declared > MAX_BODY or len(raw) > MAX_BODY:
            raise ApiError(413, "Request too large.")
        _, session = session_of(request)
        if not session or not safe_equal(request.headers.get("x-csrf-token"), session["csrf"]):
            raise ApiError(403, "Invalid CSRF token.")
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            raise ApiError(400, "Invalid JSON.") from None
        return body if isinstance(body, dict) else {}

    def record(login: str | None, event: str, detail: dict) -> None:
        store.record(login, event, detail)

    # ---------------------------------------------------------------- auth

    @app.get("/auth/login")
    def login():
        state = random_token()
        res = RedirectResponse(oauth.authorize_url(state), status_code=302)
        set_cookie(res, store.new_session(state))
        return res

    @app.get("/auth/callback")
    def callback(request: Request):
        sid, session = session_of(request)
        q = request.query_params
        if not session or not session.get("oauth_state") or not safe_equal(q.get("state", ""), session["oauth_state"]):
            return RedirectResponse("/?error=state", status_code=302)
        if q.get("error"):
            store.delete_session(sid)
            return RedirectResponse("/?error=cancelled", status_code=302)
        tokens = oauth.exchange_code(q.get("code", ""))
        user = GitHubUser(tokens["accessToken"], http).get_user()
        if not user_allowed(config, user["login"]):
            oauth.revoke(tokens["accessToken"])
            store.delete_session(sid)
            record(user["login"], "sign_in_refused", {})
            return RedirectResponse("/?error=not_allowed", status_code=302)
        res = RedirectResponse("/", status_code=302)
        set_cookie(res, store.sign_in(sid, user["login"], tokens))
        record(user["login"], "signed_in", {})
        return res

    @app.get("/api/session")
    def session_info(request: Request, response: Response):
        sid, session = session_of(request)
        if not session:
            new_sid = store.new_session()
            session = store.get_session(new_sid)
            if session is None:
                raise ApiError(500, "Could not start a session.")
            set_cookie(response, new_sid)
        login = session.get("login")
        return {"signedIn": bool(login), "login": login, "role": role_of(config, login) if login else None,
                "csrf": session["csrf"], "actions": ACTIONS}

    @app.post("/api/logout")
    def logout(request: Request, _body: dict = Depends(csrf)):
        sid, session = session_of(request)
        if tokens := store.tokens(session):
            oauth.revoke(tokens["accessToken"])
        store.delete_session(sid)
        if session and session.get("login"):
            record(session["login"], "signed_out", {})
        res = JSONResponse({"ok": True})
        res.delete_cookie(COOKIE, path="/")
        return res

    # ---------------------------------------------------------------- repos & changes

    def repo_view(r: dict) -> dict:
        perms = r.get("permissions") or {}
        return {"fullName": r["full_name"], "owner": r["owner"]["login"], "name": r["name"],
                "private": r.get("private", False), "defaultBranch": r["default_branch"],
                "description": r.get("description") or "", "archived": r.get("archived", False),
                "access": "admin" if perms.get("admin") else "write" if perms.get("push") else "read"}

    @app.get("/api/repos")
    def repos(user: User = Depends(signed_in)):
        return {"repos": [repo_view(r) for r in user.gh.list_repos()]}

    @app.get("/api/repos/{owner}/{name}")
    def repo(owner: str, name: str, user: User = Depends(signed_in)):
        r = workflows.authorize_read(user.gh, config, owner, name)
        readme = user.gh.get_file(owner, name, README, r["default_branch"])
        return {"repo": repo_view(r),
                "readme": {"exists": True, "text": readme["text"][:20000]} if readme else {"exists": False}}

    @app.post("/api/repos/{owner}/{name}/draft")
    def draft(owner: str, name: str, user: User = Depends(signed_in), _body: dict = Depends(csrf)):
        if store.events_last_hour(user.login, "draft_readme") >= config["security"]["maxDraftsPerHour"]:
            raise ActionRefused("You've hit the hourly limit for AI drafts. Try again later.")
        r = authorize(user.gh, config, owner, name)
        files = user.gh.list_root(owner, name, r["default_branch"])
        markdown = agent.draft_readme({"name": r["name"], "description": r.get("description") or "",
                                       "language": r.get("language") or "", "files": files})
        record(user.login, "draft_readme", {"repo": r["full_name"]})
        return {"markdown": markdown}

    @app.post("/api/repos/{owner}/{name}/proposals", status_code=201)
    def create_proposal(owner: str, name: str, user: User = Depends(signed_in), body: dict = Depends(csrf)):
        action = str(body.get("action") or "")
        if store.proposals_last_hour(user.login) >= config["security"]["maxChangesPerHour"]:
            raise ActionRefused("You've hit the hourly limit for changes. Try again later.")
        try:
            proposal = propose(user.gh, agent, config, owner, name, action, str(body.get("content") or ""))
        except ActionRefused as e:
            record(user.login, "refused", {"repo": f"{owner}/{name}", "action": action, "reason": str(e)})
            raise
        fp = fingerprint(proposal)
        pid = store.save_proposal(user.login, proposal, fp)
        record(user.login, "proposed", {"repo": f"{owner}/{name}", "action": action, "fingerprint": fp})
        return {"id": pid}

    def proposal_view(rec: dict) -> dict:
        p = rec["proposal"]
        return {"id": rec["id"], "status": rec["status"], "prUrl": rec["pr_url"],
                "action": p["action"], "actionLabel": ACTIONS.get(p["action"]),
                "owner": p["owner"], "name": p["name"], "fullName": f"{p['owner']}/{p['name']}",
                "baseBranch": p["baseBranch"], "diff": p["diff"]}

    @app.get("/api/proposals/{pid}")
    def get_proposal(pid: str, user: User = Depends(signed_in)):
        rec = store.get_proposal(pid, user.login)
        if not rec:
            raise ApiError(404, "Not found.")
        return proposal_view(rec)

    @app.post("/api/proposals/{pid}/approve")
    def approve(pid: str, user: User = Depends(signed_in), _body: dict = Depends(csrf)):
        rec = store.get_proposal(pid, user.login)
        if not rec:
            raise ApiError(404, "Not found.")
        if fingerprint(rec["proposal"]) != rec["fingerprint"]:
            store.finish_proposal(rec["id"], "rejected")
            raise ActionRefused("This change no longer matches its preview.")
        if not store.claim_proposal(rec["id"], user.login):
            raise ActionRefused("This change was already handled or has expired. Start a new one.")
        full_name = proposal_view(rec)["fullName"]
        try:
            pr_url = apply(user.gh, config, rec["proposal"], user.login)
        except Exception as e:
            store.finish_proposal(rec["id"], "failed")
            record(user.login, "apply_failed", {"repo": full_name, "action": rec["proposal"]["action"],
                                                "error": str(e)[:200]})
            raise
        store.finish_proposal(rec["id"], "done", pr_url)
        record(user.login, "approved", {"repo": full_name, "action": rec["proposal"]["action"],
                                        "fingerprint": rec["fingerprint"], "pr": pr_url})
        return {"status": "done", "prUrl": pr_url}

    @app.post("/api/proposals/{pid}/discard")
    def discard(pid: str, user: User = Depends(signed_in), _body: dict = Depends(csrf)):
        if store.claim_proposal(pid, user.login):
            store.finish_proposal(pid, "discarded")
            record(user.login, "discarded", {"proposal": pid})
        return {"ok": True}

    @app.get("/api/activity")
    def activity(user: User = Depends(signed_in)):
        return {"events": store.events(user.login)}

    workflows.register(app, config=config, store=store, agent=agent, indexes=indexes, signed_in=signed_in,
                       csrf=csrf, record=record, ApiError=ApiError)

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                   include_in_schema=False)
    def api_not_found(rest: str):
        raise ApiError(404, "Not found.")

    # ---------------------------------------------------------------- React SPA

    dist = Path(web["clientDist"]).resolve() if web["clientDist"] else None
    index = dist / "index.html" if dist else None
    if dist is not None and index is not None and index.is_file():
        root, index_file = dist, index

        @app.get("/{rest:path}", include_in_schema=False)
        def spa(rest: str):
            if rest.startswith("auth/"):
                return PlainTextResponse("Not found", status_code=404)
            target = (root / rest).resolve()
            hidden = any(part.startswith(".") for part in Path(rest).parts)
            if rest and not hidden and target.is_relative_to(root) and target.is_file():
                return FileResponse(target, headers={"Cache-Control": "public, max-age=3600"})
            return FileResponse(index_file)
    else:
        @app.get("/", include_in_schema=False)
        def not_built():
            if not dist:
                return PlainTextResponse("SARVAS API is running. Open the web page at its own address.")
            return PlainTextResponse("Client not built. Run: npm --prefix client run build")

    # ---------------------------------------------------------------- errors

    @app.exception_handler(ApiError)
    def on_api_error(request: Request, e: ApiError):
        return _error(e.status, str(e), e.signed_out)

    @app.exception_handler(ActionRefused)
    def on_refused(request: Request, e: ActionRefused):
        return _error(400, str(e))

    @app.exception_handler(GitHubError)
    def on_github_error(request: Request, e: GitHubError):
        if e.status == 401:
            store.delete_session(request.cookies.get(COOKIE))
            return _error(401, str(e), True)
        return _error(502, str(e))

    @app.exception_handler(AgentServiceError)
    def on_agent_error(request: Request, e: AgentServiceError):
        # 409 (task moved on) and 503 (paused / unavailable) are meaningful to the page.
        return _error(e.status if e.status in (409, 503) else 502, str(e))

    @app.exception_handler(RequestValidationError)
    def on_invalid(request: Request, e: RequestValidationError):
        return _error(400, "Invalid request.")

    return app
