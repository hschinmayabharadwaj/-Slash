"""Web page tests against an in-memory fake of the GitHub API."""

import base64
import re
import sqlite3
from urllib.parse import parse_qs, urlparse

import pytest

from slackagent.config import load_config
from slackagent.webapp import create_app

BASE = "http://localhost:8765"


class FakeResponse:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data
        self.content = b"" if data is None else b"x"

    def json(self):
        return self._data


class FakeGitHub:
    """Just enough of github.com + api.github.com for the web flow."""

    def __init__(self):
        self.push = True
        self.files = {}  # (branch, path) -> text
        self.branches = {"main": "sha-main"}
        self.prs, self.revoked, self.calls = [], [], []
        self.users = {"ghu_alice": "alice", "ghu_bob": "bob"}
        self.next_token = "ghu_alice"

    def repo(self):
        return {"name": "proj", "full_name": "octo/proj", "owner": {"login": "octo"},
                "default_branch": "main", "private": False, "archived": False,
                "description": "A demo", "language": "Python", "permissions": {"push": self.push}}

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        u = urlparse(url)
        path = u.path
        if u.netloc == "github.com" and path == "/login/oauth/access_token":
            return FakeResponse(200, {"access_token": self.next_token, "refresh_token": "ghr_x", "expires_in": 28800})
        if path.startswith("/applications/"):
            self.revoked.append(kw["json"]["access_token"])
            return FakeResponse(204)
        token = (kw.get("headers") or {}).get("Authorization", "").removeprefix("Bearer ")
        if token not in self.users:
            return FakeResponse(401, {"message": "Bad credentials"})
        if path == "/user":
            return FakeResponse(200, {"login": self.users[token]})
        if path == "/user/installations":
            return FakeResponse(200, {"installations": [{"id": 1}]})
        if path == "/user/installations/1/repositories":
            return FakeResponse(200, {"repositories": [self.repo()]})
        if path == "/repos/octo/proj":
            return FakeResponse(200, self.repo())
        if path == "/repos/octo/proj/contents/":
            return FakeResponse(200, [{"name": "app.py", "type": "file"}, {"name": "src", "type": "dir"}])
        if path == "/repos/octo/proj/contents/README.md":
            branch = (kw.get("params") or {}).get("ref") or (kw.get("json") or {}).get("branch")
            key = (branch, "README.md")
            if method == "GET":
                if key not in self.files:
                    return FakeResponse(404, {"message": "Not Found"})
                text = self.files[key]
                return FakeResponse(200, {"type": "file", "sha": f"blob-{hash(text)}", "size": len(text),
                                          "encoding": "base64", "content": base64.b64encode(text.encode()).decode()})
            if method == "PUT":
                self.files[key] = base64.b64decode(kw["json"]["content"]).decode()
                return FakeResponse(201, {})
            if method == "DELETE":
                self.files.pop(key, None)
                return FakeResponse(200, {})
        if path == "/repos/octo/proj/git/ref/heads/main":
            return FakeResponse(200, {"object": {"sha": self.branches["main"]}})
        if path == "/repos/octo/proj/git/refs" and method == "POST":
            branch = kw["json"]["ref"].removeprefix("refs/heads/")
            self.branches[branch] = kw["json"]["sha"]
            # a new branch starts with main's files
            for (b, p), text in list(self.files.items()):
                if b == "main":
                    self.files[(branch, p)] = text
            return FakeResponse(201, {})
        if path.startswith("/repos/octo/proj/git/refs/heads/") and method == "DELETE":
            self.branches.pop(path.rsplit("/", 1)[-1], None)
            return FakeResponse(204)
        if path == "/repos/octo/proj/pulls":
            self.prs.append(kw["json"])
            return FakeResponse(201, {"html_url": f"https://github.com/octo/proj/pull/{len(self.prs)}"})
        return FakeResponse(404, {"message": f"unhandled {method} {path}"})


@pytest.fixture
def gh():
    return FakeGitHub()


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.delenv("WEB_ENCRYPTION_KEY", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(f"""
github:
  client_id: Iv1.test
  client_secret: shh
ai:
  provider: sim
database:
  path: {tmp_path}/t.db
web:
  encryption_key_file: {tmp_path}/key
security:
  kill_switch_file: {tmp_path}/KILL
""")
    return load_config(str(path), require=("web",))


@pytest.fixture
def client(config, gh):
    return create_app(config, http=gh).test_client()


def sign_in(client, user="ghu_alice", gh=None):
    if gh:
        gh.next_token = user
    location = client.get("/auth/login", base_url=BASE).headers["Location"]
    state = parse_qs(urlparse(location).query)["state"][0]
    response = client.get(f"/auth/callback?code=abc&state={state}", base_url=BASE)
    assert response.status_code == 302
    return response


def csrf(client, url):
    html = client.get(url, base_url=BASE).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def propose(client, action="create_readme", content="# proj\nHello\n"):
    token = csrf(client, "/repos/octo/proj")
    return client.post("/repos/octo/proj/propose", base_url=BASE,
                       data={"csrf": token, "action": action, "content": content})


# --------------------------------------------------------------------------- auth & plumbing

def test_landing_page_and_headers(client):
    response = client.get("/", base_url=BASE)
    assert b"Connect GitHub" in response.data
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Frame-Options"] == "DENY"


def test_unexpected_host_rejected(client):
    assert client.get("/", base_url="http://evil.example:8765").status_code == 400


def test_login_redirects_to_github_with_state(client):
    location = client.get("/auth/login", base_url=BASE).headers["Location"]
    query = parse_qs(urlparse(location).query)
    assert location.startswith("https://github.com/login/oauth/authorize")
    assert query["client_id"] == ["Iv1.test"] and len(query["state"][0]) > 30
    assert query["redirect_uri"] == [f"{BASE}/auth/callback"]


def test_callback_rejects_wrong_state(client, gh):
    client.get("/auth/login", base_url=BASE)
    response = client.get("/auth/callback?code=abc&state=forged", base_url=BASE)
    assert response.status_code == 400
    assert not any("access_token" in url for _, url in gh.calls)


def test_sign_in_rotates_session_and_encrypts_token(client, config):
    first = client.get("/auth/login", base_url=BASE).headers["Set-Cookie"]
    second = sign_in(client).headers["Set-Cookie"]
    assert first.split(";")[0] != second.split(";")[0]
    assert "HttpOnly" in second and "SameSite=Lax" in second
    assert b"@alice" in client.get("/", base_url=BASE).data
    raw = sqlite3.connect(config.database.path).execute("SELECT tokens FROM web_sessions").fetchall()
    assert raw and all("ghu_alice" not in (row[0] or "") for row in raw)


def test_logout_revokes_token(client, gh):
    sign_in(client)
    token = csrf(client, "/")
    client.post("/auth/logout", base_url=BASE, data={"csrf": token})
    assert gh.revoked == ["ghu_alice"]
    assert b"Connect GitHub" in client.get("/", base_url=BASE).data


def test_post_without_csrf_is_forbidden(client):
    sign_in(client)
    response = client.post("/repos/octo/proj/propose", base_url=BASE,
                           data={"action": "create_readme", "content": "x"})
    assert response.status_code == 403


def test_cross_origin_post_is_forbidden(client):
    sign_in(client)
    token = csrf(client, "/repos/octo/proj")
    response = client.post("/repos/octo/proj/propose", base_url=BASE,
                           headers={"Origin": "https://evil.example"},
                           data={"csrf": token, "action": "create_readme", "content": "x"})
    assert response.status_code == 403


# --------------------------------------------------------------------------- changes

def test_create_readme_flow_opens_pr_and_never_touches_main(client, gh):
    sign_in(client)
    assert b"Add a README.md" in client.get("/repos/octo/proj", base_url=BASE).data

    response = propose(client)
    assert response.status_code == 303
    preview_url = response.headers["Location"]
    preview = client.get(preview_url, base_url=BASE).get_data(as_text=True)
    assert "+# proj" in preview and "Approve" in preview
    assert not gh.prs  # nothing happens before approval

    token = csrf(client, preview_url)
    client.post(preview_url + "/approve", base_url=BASE, data={"csrf": token})
    assert len(gh.prs) == 1 and gh.prs[0]["base"] == "main"
    branch = gh.prs[0]["head"]
    assert gh.files[(branch, "README.md")] == "# proj\nHello\n"
    assert ("main", "README.md") not in gh.files
    assert "pull/1" in client.get(preview_url, base_url=BASE).get_data(as_text=True)

    # Approving twice does nothing.
    client.post(preview_url + "/approve", base_url=BASE, data={"csrf": token})
    assert len(gh.prs) == 1

    activity = client.get("/activity", base_url=BASE).get_data(as_text=True)
    assert "proposed" in activity and "approved" in activity


def test_delete_readme_flow(client, gh):
    gh.files[("main", "README.md")] = "# Old\n"
    sign_in(client)
    page = client.get("/repos/octo/proj", base_url=BASE).get_data(as_text=True)
    assert "Preview deleting README.md" in page
    preview_url = propose(client, action="delete_readme").headers["Location"]
    assert "-# Old" in client.get(preview_url, base_url=BASE).get_data(as_text=True)
    client.post(preview_url + "/approve", base_url=BASE, data={"csrf": csrf(client, preview_url)})
    branch = gh.prs[0]["head"]
    assert (branch, "README.md") not in gh.files and gh.files[("main", "README.md")] == "# Old\n"


def test_no_write_permission_is_refused(client, gh):
    gh.push = False
    sign_in(client)
    response = client.get("/repos/octo/proj", base_url=BASE)
    assert response.status_code == 400 and b"write access" in response.data


def test_repo_not_in_allowlist(client, config):
    config.security.allowed_repos = ["octo/other"]
    sign_in(client)
    assert client.get("/repos/octo/proj", base_url=BASE).status_code == 400


@pytest.mark.parametrize("content,needle", [
    ("token = 'ghp_abcdefghijklmnopqrstuvwxyz0123456789'", b"GitHub token"),
    ("   ", b"empty"),
])
def test_bad_readme_content_refused(client, gh, content, needle):
    sign_in(client)
    response = propose(client, content=content)
    assert response.status_code == 400 and needle in response.data


def test_unknown_action_refused(client):
    sign_in(client)
    assert propose(client, action="delete_repo").status_code == 400


def test_create_when_readme_exists_refused(client, gh):
    gh.files[("main", "README.md")] = "# Existing\n"
    sign_in(client)
    assert b"already has" in propose(client).data


def test_stale_preview_refused(client, gh):
    sign_in(client)
    preview_url = propose(client).headers["Location"]
    gh.files[("main", "README.md")] = "# Someone else added one\n"
    response = client.post(preview_url + "/approve", base_url=BASE, data={"csrf": csrf(client, preview_url)})
    assert response.status_code == 400 and b"changed since the preview" in response.data
    assert not gh.prs and set(gh.branches) == {"main"}


def test_other_user_cannot_see_or_approve(client, config, gh):
    sign_in(client)
    preview_url = propose(client).headers["Location"]

    other = create_app(config, http=gh).test_client()
    sign_in(other, "ghu_bob", gh)
    assert other.get(preview_url, base_url=BASE).status_code == 404
    token = csrf(other, "/")
    assert other.post(preview_url + "/approve", base_url=BASE, data={"csrf": token}).status_code == 404
    assert not gh.prs


def test_kill_switch_blocks_apply(client, config, gh, tmp_path):
    sign_in(client)
    preview_url = propose(client).headers["Location"]
    (tmp_path / "KILL").touch()
    response = client.post(preview_url + "/approve", base_url=BASE, data={"csrf": csrf(client, preview_url)})
    assert b"paused" in response.data and not gh.prs


def test_hourly_limit(client, config):
    config.security.max_changes_per_hour = 1
    sign_in(client)
    assert propose(client).status_code == 303
    assert b"hourly limit" in propose(client).data


def test_ai_draft_with_sim(client):
    sign_in(client)
    token = csrf(client, "/repos/octo/proj")
    html = client.post("/repos/octo/proj/draft", base_url=BASE, data={"csrf": token}).get_data(as_text=True)
    assert "# proj" in html and "app.py" in html


def test_draft_output_is_escaped(client, gh):
    gh_repo = gh.repo
    gh.repo = lambda: {**gh_repo(), "description": "<script>alert(1)</script>"}
    sign_in(client)
    token = csrf(client, "/repos/octo/proj")
    html = client.post("/repos/octo/proj/draft", base_url=BASE, data={"csrf": token}).get_data(as_text=True)
    assert "<script>alert(1)" not in html
