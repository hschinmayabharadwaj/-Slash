"""The local web page: connect GitHub, pick a repo, preview, approve, PR.

    python -m slackagent.web            # then open http://localhost:8765

Protections on top of GitHub's own permission checks:
  * binds to 127.0.0.1 and rejects unexpected Host headers (DNS rebinding);
  * HttpOnly SameSite=Lax session cookie + CSRF token on every POST + Origin check;
  * OAuth ``state`` checked in constant time; session ID rotated at sign-in;
  * strict Content-Security-Policy, no JavaScript, no framing;
  * every proposal/approval/refusal is written to the audit log.
"""
from __future__ import annotations

import hmac
import logging
import secrets
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

from flask import Flask, abort, make_response, redirect, render_template, request, url_for

from .config import Config
from .database import Database
from .github_user import GitHubOAuth, GitHubUserAPI, GitHubUserError
from .repo_actions import ACTIONS, ActionRefused, apply, authorize, draft_readme, propose
from .web_store import WebStore, load_or_create_key

logger = logging.getLogger(__name__)

COOKIE = "agent_session"


def create_app(
    config: Config,
    *,
    http: Any = None,
    store: Optional[WebStore] = None,
    oauth: Optional[GitHubOAuth] = None,
) -> Flask:
    """Build the Flask app. ``http`` (a requests-like session) is injectable for tests."""
    app = Flask(__name__)
    base = urlparse(config.web.base_url)
    secure_cookie = base.scheme == "https"
    allowed_hosts = {base.netloc, f"127.0.0.1:{config.web.port}", f"localhost:{config.web.port}"}

    store = store or WebStore(
        config.database.path,
        load_or_create_key(config.web.encryption_key_file),
        config.web.session_hours,
    )
    oauth = oauth or GitHubOAuth(
        config.github.client_id,
        config.github.client_secret,
        config.web.base_url.rstrip("/") + "/auth/callback",
        http=http,
    )
    audit = Database(config.database.path)

    # ------------------------------------------------------------------ helpers

    def session() -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        sid = request.cookies.get(COOKIE)
        return sid, store.get_session(sid)

    def set_cookie(response, sid: str):
        response.set_cookie(
            COOKIE, sid, httponly=True, samesite="Lax", secure=secure_cookie,
            max_age=config.web.session_hours * 3600, path="/",
        )
        return response

    def signed_in() -> Tuple[str, Dict[str, Any], GitHubUserAPI]:
        """The current user's session and API client, refreshing the token if needed."""
        sid, sess = session()
        if not sess or not sess.get("github_login"):
            abort(redirect(url_for("index")))
        tokens = store.tokens(sess)
        if tokens is None:
            abort(redirect(url_for("index")))
        if tokens.expired():
            if not tokens.refresh_token:
                store.delete_session(sid)
                abort(redirect(url_for("index")))
            tokens = oauth.refresh(tokens.refresh_token)
            store.update_tokens(sid, tokens)
        return sid, sess, GitHubUserAPI(tokens.access_token, http=http)

    def check_csrf(sess: Dict[str, Any]) -> None:
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc not in allowed_hosts:
            abort(403)
        sent = request.form.get("csrf", "")
        if not sent or not hmac.compare_digest(sent, sess["csrf"]):
            abort(403)

    def log(login: str, event: str, **detail: Any) -> None:
        audit.record_event(None, f"web.{event}", login, detail)

    def page(template: str, status: int = 200, **ctx: Any):
        _, sess = session()
        ctx.setdefault("login", sess.get("github_login") if sess else None)
        ctx.setdefault("csrf", sess["csrf"] if sess else "")
        return render_template(template, actions=ACTIONS, ai=config.ai.provider, **ctx), status

    def guarded(fn: Callable) -> Callable:
        """Turn expected refusals/GitHub errors into friendly pages."""
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ActionRefused as e:
                return page("message.html", 400, title="Not allowed", message=str(e))
            except GitHubUserError as e:
                if e.status == 401:
                    sid, _ = session()
                    if sid:
                        store.delete_session(sid)
                return page("message.html", 502, title="GitHub problem", message=str(e))
        wrapper.__name__ = fn.__name__
        return wrapper

    # ------------------------------------------------------------------ security plumbing

    @app.before_request
    def host_check():
        if request.host not in allowed_hosts:
            abort(400)

    @app.after_request
    def headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' https://avatars.githubusercontent.com; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    # ------------------------------------------------------------------ auth

    @app.get("/")
    @guarded
    def index():
        _, sess = session()
        if not sess or not sess.get("github_login"):
            return page("index.html", repos=None)
        _, sess, api = signed_in()
        return page("index.html", repos=api.list_repos())

    @app.get("/auth/login")
    def login():
        state = secrets.token_urlsafe(32)
        response = make_response(redirect(oauth.authorize_url(state)))
        return set_cookie(response, store.new_session(oauth_state=state))

    @app.get("/auth/callback")
    @guarded
    def callback():
        sid, sess = session()
        state = request.args.get("state", "")
        if not sess or not sess.get("oauth_state") or not hmac.compare_digest(state, sess["oauth_state"]):
            return page("message.html", 400, title="Sign-in failed",
                        message="The sign-in link expired or didn't come from this page. Please try again.")
        if request.args.get("error"):
            store.delete_session(sid)
            return page("message.html", 400, title="Not connected",
                        message="GitHub authorization was cancelled.")
        tokens = oauth.exchange_code(request.args.get("code", ""))
        user = GitHubUserAPI(tokens.access_token, http=http).get_user()
        new_sid = store.sign_in(sid, user["login"], tokens)
        log(user["login"], "signed_in")
        return set_cookie(make_response(redirect(url_for("index"))), new_sid)

    @app.post("/auth/logout")
    def logout():
        sid, sess = session()
        if sess:
            check_csrf(sess)
            tokens = store.tokens(sess)
            if tokens:
                oauth.revoke(tokens.access_token)
            store.delete_session(sid)
            log(sess.get("github_login") or "", "signed_out")
        response = make_response(redirect(url_for("index")))
        response.delete_cookie(COOKIE, path="/")
        return response

    # ------------------------------------------------------------------ repo + changes

    @app.get("/repos/<owner>/<name>")
    @guarded
    def repo(owner: str, name: str):
        _, sess, api = signed_in()
        repo = authorize(api, config, owner, name)
        readme = api.get_file(owner, name, "README.md", repo["default_branch"])
        return page("repo.html", repo=repo, readme=readme, draft="")

    @app.post("/repos/<owner>/<name>/draft")
    @guarded
    def draft(owner: str, name: str):
        _, sess, api = signed_in()
        check_csrf(sess)
        repo = authorize(api, config, owner, name)
        text = draft_readme(api, repo, config.ai)
        log(sess["github_login"], "draft_readme", repo=f"{owner}/{name}", provider=config.ai.provider)
        return page("repo.html", repo=repo, readme=None, draft=text)

    @app.post("/repos/<owner>/<name>/propose")
    @guarded
    def make_proposal(owner: str, name: str):
        _, sess, api = signed_in()
        check_csrf(sess)
        login_name = sess["github_login"]
        if store.proposals_last_hour(login_name) >= config.security.max_changes_per_hour:
            raise ActionRefused("You've hit the hourly limit for changes. Try again later.")
        action = request.form.get("action", "")
        try:
            proposal = propose(api, config, owner, name, action, request.form.get("content", ""))
        except ActionRefused as e:
            log(login_name, "refused", repo=f"{owner}/{name}", action=action, reason=str(e))
            raise
        proposal_id = store.save_proposal(login_name, proposal)
        log(login_name, "proposed", repo=proposal.full_name, action=action, fingerprint=proposal.fingerprint())
        return redirect(url_for("show_proposal", proposal_id=proposal_id), code=303)

    @app.get("/changes/<proposal_id>")
    @guarded
    def show_proposal(proposal_id: str):
        _, sess, _ = signed_in()
        record = store.get_proposal(proposal_id, sess["github_login"])
        if not record:
            abort(404)
        return page("proposal.html", record=record, p=record["proposal"])

    @app.post("/changes/<proposal_id>/approve")
    @guarded
    def approve(proposal_id: str):
        _, sess, api = signed_in()
        check_csrf(sess)
        login_name = sess["github_login"]
        record = store.get_proposal(proposal_id, login_name)
        if not record:
            abort(404)
        proposal = record["proposal"]
        if proposal.fingerprint() != record["fingerprint"]:
            store.finish_proposal(proposal_id, "rejected")
            raise ActionRefused("This change no longer matches its preview.")
        if not store.claim_proposal(proposal_id, login_name):
            raise ActionRefused("This change was already handled or has expired. Start a new one.")
        try:
            pr_url = apply(api, config, proposal, login_name)
        except Exception as e:
            store.finish_proposal(proposal_id, "failed")
            log(login_name, "apply_failed", repo=proposal.full_name, action=proposal.action,
                error=type(e).__name__)
            raise
        store.finish_proposal(proposal_id, "done", pr_url)
        log(login_name, "approved", repo=proposal.full_name, action=proposal.action,
            fingerprint=record["fingerprint"], pr=pr_url)
        return redirect(url_for("show_proposal", proposal_id=proposal_id), code=303)

    @app.post("/changes/<proposal_id>/discard")
    def discard(proposal_id: str):
        _, sess, _ = signed_in()
        check_csrf(sess)
        if store.claim_proposal(proposal_id, sess["github_login"]):
            store.finish_proposal(proposal_id, "discarded")
            log(sess["github_login"], "discarded", proposal=proposal_id)
        return redirect(url_for("index"), code=303)

    @app.get("/activity")
    def activity():
        _, sess, _ = signed_in()
        events = [e for e in audit.list_web_events(sess["github_login"])]
        return page("activity.html", events=events)

    return app
