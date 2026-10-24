import base64
import json
import re
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from sarvas_web.app import create_app
from sarvas_web.config import load_config
from sarvas_web.store import Store

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"

# ------------------------------------------------------------------ fakes


class FakeGitHub:
    def __init__(self):
        self.push, self.archived = True, False
        self.files: dict[str, str] = {}
        self.branches = {"main": "sha-main"}
        self.prs: list[dict] = []
        self.revoked: list[str] = []
        self.users = {"ghu_alice": "alice", "ghu_bob": "bob"}
        self.next_token = "ghu_alice"
        self.description = "A demo"
        self.http = httpx.Client(transport=httpx.MockTransport(self.handle))

    def repo(self):
        return {"name": "proj", "full_name": "octo/proj", "owner": {"login": "octo"}, "default_branch": "main",
                "private": False, "archived": self.archived, "description": self.description,
                "language": "JavaScript", "permissions": {"push": self.push}}

    def handle(self, req: httpx.Request) -> httpx.Response:
        def res(status, body=None):
            return httpx.Response(status, json=body) if body is not None else httpx.Response(status)
        p, method = req.url.path, req.method
        body = json.loads(req.content) if req.content and req.headers.get("content-type") == "application/json" else None
        if req.url.host == "github.com" and p == "/login/oauth/access_token":
            return res(200, {"access_token": self.next_token, "refresh_token": "ghr_x", "expires_in": 28800})
        if p.startswith("/applications/"):
            self.revoked.append(body["access_token"])
            return res(204)
        token = req.headers.get("authorization", "").replace("Bearer ", "")
        if token not in self.users:
            return res(401, {"message": "Bad credentials"})
        if p == "/user":
            return res(200, {"login": self.users[token]})
        if p == "/user/installations":
            return res(200, {"installations": [{"id": 1}]})
        if p == "/user/installations/1/repositories":
            return res(200, {"repositories": [self.repo()]})
        if p == "/repos/octo/proj":
            return res(200, self.repo())
        if p == "/repos/octo/proj/contents/":
            return res(200, [{"name": "app.js", "type": "file"}, {"name": "src", "type": "dir"}])
        if p == "/repos/octo/proj/contents/README.md":
            branch = req.url.params.get("ref") or (body or {}).get("branch")
            key = f"{branch}:README.md"
            if method == "GET":
                if key not in self.files:
                    return res(404, {"message": "Not Found"})
                text = self.files[key]
                return res(200, {"type": "file", "sha": f"blob-{len(text)}-{ord(text[2])}", "encoding": "base64",
                                 "content": base64.b64encode(text.encode()).decode()})
            if method == "PUT":
                self.files[key] = base64.b64decode(body["content"]).decode()
                return res(201, {})
            if method == "DELETE":
                self.files.pop(key, None)
                return res(200, {})
        if p == "/repos/octo/proj/git/ref/heads/main":
            return res(200, {"object": {"sha": self.branches["main"]}})
        if p == "/repos/octo/proj/git/refs" and method == "POST":
            branch = body["ref"].removeprefix("refs/heads/")
            self.branches[branch] = body["sha"]
            for k, v in list(self.files.items()):
                if k.startswith("main:"):
                    self.files[k.replace("main:", f"{branch}:", 1)] = v
            return res(201, {})
        if p.startswith("/repos/octo/proj/git/refs/heads/") and method == "DELETE":
            self.branches.pop(p.rsplit("/", 1)[-1], None)
            return res(204)
        if p == "/repos/octo/proj/pulls":
            self.prs.append(body)
            return res(201, {"html_url": f"https://github.com/octo/proj/pull/{len(self.prs)}"})
        return res(404, {"message": f"unhandled {method} {p}"})


class FakeAgent:
    def draft_readme(self, facts):
        return f"# {facts['name']}\n\n{facts['description']}\n\n" + "\n".join(f"- {f}" for f in facts["files"]) + "\n"

    def check_readme(self, content):
        problems = []
        if not content.strip():
            problems.append("the README can't be empty")
        if re.search(r"AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}", content):
            problems.append("it appears to add a secret")
        return problems


# ------------------------------------------------------------------ client


class Client:
    def __init__(self, app, gh):
        self.c = TestClient(app, base_url=BASE, follow_redirects=False)
        self.gh = gh

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def csrf(self):
        return self.get("/api/session").json()["csrf"]

    def post(self, url, body=None, headers=None):
        return self.c.post(url, json=body if body is not None else {},
                           headers={"X-CSRF-Token": self.csrf(), **(headers or {})})

    def sign_in(self, token="ghu_alice"):
        self.gh.next_token = token
        login = self.get("/auth/login")
        state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
        return self.get(f"/auth/callback?code=abc&state={state}")

    def propose(self, action="create_readme", content="# proj\nHello\n"):
        return self.post("/api/repos/octo/proj/proposals", {"action": action, "content": content})


# ------------------------------------------------------------------ setup


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_DISABLED", raising=False)
    (tmp_path / "config.json").write_text(json.dumps({
        "web": {"port": PORT, "baseUrl": f"http://localhost:{PORT}", "dataDir": str(tmp_path),
                "clientDist": str(tmp_path / "nope")},
        "github": {"clientId": "Iv1.test", "clientSecret": "shh"},
        "agentService": {"token": "x" * 32},
        "security": {"killSwitchFile": str(tmp_path / "KILL")},
    }))
    config = load_config(tmp_path / "config.json")
    gh = FakeGitHub()
    store = Store(str(tmp_path), bytes([7]) * 32)

    class Env:
        pass
    e = Env()
    e.config, e.gh, e.store, e.tmp = config, gh, store, tmp_path
    e.client = lambda agent=None: Client(create_app(config, store, agent or FakeAgent(), gh.http), gh)
    e.c = e.client()
    return e


# ------------------------------------------------------------------ auth & plumbing


def test_security_headers_and_session_bootstrap(env):
    res = env.c.get("/api/session")
    assert res.json()["signedIn"] is False
    assert len(res.json()["csrf"]) > 30
    assert "script-src 'self'" in res.headers["content-security-policy"]
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["cache-control"] == "no-store"
    assert "httponly" in res.headers["set-cookie"].lower()


def test_unexpected_host_is_rejected(env):
    assert env.c.get("/api/session", headers={"Host": "evil.example:8765"}).status_code == 400


def test_login_redirects_to_github_with_state_and_callback(env):
    url = urlparse(env.c.get("/auth/login").headers["location"])
    q = parse_qs(url.query)
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://github.com/login/oauth/authorize"
    assert q["client_id"] == ["Iv1.test"]
    assert q["redirect_uri"] == [f"http://localhost:{PORT}/auth/callback"]
    assert len(q["state"][0]) > 30


def test_callback_with_wrong_state_is_refused(env):
    env.c.get("/auth/login")
    res = env.c.get("/auth/callback?code=abc&state=forged")
    assert res.headers["location"] == "/?error=state"
    assert env.c.get("/api/session").json()["signedIn"] is False


def test_sign_in_rotates_session_and_encrypts_token(env):
    env.c.get("/auth/login")
    before = env.c.c.cookies.get("agent_session")
    env.c.sign_in()
    assert env.c.c.cookies.get("agent_session") != before
    assert env.c.get("/api/session").json()["login"] == "alice"
    rows = env.store.db.execute("SELECT tokens FROM sessions").fetchall()
    assert all("ghu_alice" not in str(r["tokens"] or "") for r in rows)


def test_logout_revokes_token(env):
    env.c.sign_in()
    env.c.post("/api/logout")
    assert env.gh.revoked == ["ghu_alice"]
    assert env.c.get("/api/session").json()["signedIn"] is False


def test_mutations_need_csrf_json_and_same_origin(env):
    env.c.sign_in()
    no_token = env.c.c.post("/api/repos/octo/proj/proposals", json={"action": "create_readme", "content": "x"})
    assert no_token.status_code == 403
    cross = env.c.post("/api/repos/octo/proj/proposals", {"action": "create_readme", "content": "x"},
                       {"Origin": "https://evil.example"})
    assert cross.status_code == 403
    not_json = env.c.c.post("/api/repos/octo/proj/proposals", content="action=create_readme",
                            headers={"X-CSRF-Token": env.c.csrf(), "Content-Type": "application/x-www-form-urlencoded"})
    assert not_json.status_code == 415
    too_big = env.c.post("/api/repos/octo/proj/proposals", {"action": "create_readme", "content": "x" * 200_000})
    assert too_big.status_code == 413


def test_api_requires_sign_in(env):
    res = env.c.get("/api/repos")
    assert res.status_code == 401
    assert res.json()["signedOut"] is True
    assert env.c.get("/api/nope").status_code == 404


def test_allowed_users_blocks_other_accounts(env):
    env.config["security"]["allowedUsers"] = ["Alice"]
    bob = env.client()
    assert bob.sign_in("ghu_bob").headers["location"] == "/?error=not_allowed"
    assert env.gh.revoked == ["ghu_bob"]
    assert bob.get("/api/session").json()["signedIn"] is False
    assert env.c.sign_in().headers["location"] == "/"
    assert env.c.get("/api/repos").status_code == 200
    env.config["security"]["allowedUsers"] = ["carol"]  # removed while signed in
    assert env.c.get("/api/repos").status_code == 401


def test_public_base_url_requires_allowed_users(env, tmp_path):
    raw = json.loads((tmp_path / "config.json").read_text())
    raw["web"]["baseUrl"] = "https://sarvas.onrender.com"
    (tmp_path / "config.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="allowedUsers"):
        load_config(tmp_path / "config.json")
    raw["security"]["allowedUsers"] = "alice, bob"
    (tmp_path / "config.json").write_text(json.dumps(raw))
    assert load_config(tmp_path / "config.json")["security"]["allowedUsers"] == ["alice", "bob"]


# ------------------------------------------------------------------ README changes


def test_create_flow_opens_pr_on_new_branch(env):
    c, gh = env.c, env.gh
    c.sign_in()
    assert c.get("/api/repos").json()["repos"][0]["fullName"] == "octo/proj"
    assert c.get("/api/repos/octo/proj").json()["readme"]["exists"] is False

    created = c.propose()
    assert created.status_code == 201
    pid = created.json()["id"]
    preview = c.get(f"/api/proposals/{pid}").json()
    assert "+# proj" in preview["diff"]
    assert preview["status"] == "pending"
    assert gh.prs == []

    approved = c.post(f"/api/proposals/{pid}/approve")
    assert approved.json()["status"] == "done"
    assert len(gh.prs) == 1 and gh.prs[0]["base"] == "main"
    assert gh.files[f"{gh.prs[0]['head']}:README.md"] == "# proj\nHello\n"
    assert "main:README.md" not in gh.files
    assert "@​alice" in gh.prs[0]["body"]

    again = c.post(f"/api/proposals/{pid}/approve")
    assert again.status_code == 400
    assert len(gh.prs) == 1

    events = [e["event"] for e in c.get("/api/activity").json()["events"]]
    assert "proposed" in events and "approved" in events


def test_delete_flow(env):
    env.gh.files["main:README.md"] = "# Old\n"
    env.c.sign_in()
    assert env.c.get("/api/repos/octo/proj").json()["readme"]["exists"] is True
    pid = env.c.propose("delete_readme").json()["id"]
    assert "-# Old" in env.c.get(f"/api/proposals/{pid}").json()["diff"]
    env.c.post(f"/api/proposals/{pid}/approve")
    assert f"{env.gh.prs[0]['head']}:README.md" not in env.gh.files
    assert env.gh.files["main:README.md"] == "# Old\n"


def test_no_write_permission_is_refused(env):
    env.gh.push = False
    env.c.sign_in()
    res = env.c.get("/api/repos/octo/proj")
    assert res.status_code == 400
    assert "write access" in res.json()["error"]


def test_archived_and_non_allowlisted_repos_are_refused(env):
    env.c.sign_in()
    env.gh.archived = True
    assert env.c.get("/api/repos/octo/proj").status_code == 400
    env.gh.archived = False
    env.config["security"]["allowedRepos"] = ["octo/other"]
    assert env.c.get("/api/repos/octo/proj").status_code == 400


def test_bad_content_and_unknown_actions_are_refused(env):
    env.c.sign_in()
    secret = env.c.propose("create_readme", "key AKIAABCDEFGHIJKLMNOP")
    assert secret.status_code == 400
    assert "secret" in secret.json()["error"]
    assert env.c.propose("create_readme", "   ").status_code == 400
    assert env.c.propose("delete_repo").status_code == 400
    env.gh.files["main:README.md"] = "# x\n"
    assert "already has" in env.c.propose().json()["error"]


def test_stale_preview_is_refused_and_leaves_no_branch(env):
    env.c.sign_in()
    pid = env.c.propose().json()["id"]
    env.gh.files["main:README.md"] = "# Someone else added one\n"
    res = env.c.post(f"/api/proposals/{pid}/approve")
    assert res.status_code == 400
    assert "changed since the preview" in res.json()["error"]
    assert env.gh.prs == []
    assert list(env.gh.branches) == ["main"]


def test_other_user_cannot_see_or_approve_proposal(env):
    env.c.sign_in()
    pid = env.c.propose().json()["id"]
    bob = env.client()
    bob.sign_in("ghu_bob")
    assert bob.get(f"/api/proposals/{pid}").status_code == 404
    assert bob.post(f"/api/proposals/{pid}/approve").status_code == 404
    assert env.gh.prs == []


def test_kill_switch_blocks_apply(env):
    env.c.sign_in()
    pid = env.c.propose().json()["id"]
    (env.tmp / "KILL").write_text("")
    res = env.c.post(f"/api/proposals/{pid}/approve")
    assert "paused" in res.json()["error"]
    assert env.gh.prs == []


def test_hourly_limits(env):
    env.config["security"]["maxChangesPerHour"] = 1
    env.config["security"]["maxDraftsPerHour"] = 1
    env.c.sign_in()
    assert env.c.propose().status_code == 201
    assert "hourly limit" in env.c.propose().json()["error"]
    assert env.c.post("/api/repos/octo/proj/draft").status_code == 200
    assert "hourly limit" in env.c.post("/api/repos/octo/proj/draft").json()["error"]


def test_ai_draft_sends_only_repo_facts(env):
    seen = {}

    class Agent(FakeAgent):
        def draft_readme(self, facts):
            seen.update(facts)
            return "# drafted"
    c = env.client(Agent())
    c.sign_in()
    assert c.post("/api/repos/octo/proj/draft").json()["markdown"] == "# drafted"
    assert sorted(seen) == ["description", "files", "language", "name"]
    assert "ghu_" not in json.dumps(seen)


def test_spa_fallback_and_no_path_escape(env, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<title>SARVAS</title>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("nope")
    env.config["web"]["clientDist"] = str(dist)
    c = env.client()
    assert c.get("/assets/app.js").text == "console.log(1)"
    assert "SARVAS" in c.get("/repos/octo/proj").text
    assert "nope" not in c.get("/..%2Fsecret.txt").text
    assert c.get("/api/unknown").status_code == 404
