"""Ask, indexing, isolation, Build tasks and approval permissions."""
import json
import logging
import subprocess
from pathlib import Path

import httpx
import pytest
from test_app import PORT, Client, FakeAgent, FakeGitHub

from sarvas_web import rbac
from sarvas_web.agent_client import AgentServiceError
from sarvas_web.app import create_app
from sarvas_web.config import load_config
from sarvas_web.indexer import IndexError_, Indexes, RepoIndex
from sarvas_web.server import RedactSecrets
from sarvas_web.store import Store

SETTINGS = {"maxFileKb": 256, "maxFiles": 5000, "chunkLines": 60}
DENY = [".env*", "**/secrets/**", "**/*.pem"]


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def make_repo(path: Path, files: dict[str, str]) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    return commit(path, files)


def commit(path: Path, files: dict[str, str | None]) -> str:
    for name, text in files.items():
        f = path / name
        if text is None:
            f.unlink()
            continue
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    git(path, "add", "-A")
    git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "change")
    return git(path, "rev-parse", "HEAD")


LOGIN_PY = "\n".join(["import hashlib", "", "def check_password(user, password):",
                      "    # compare the salted hash with the stored one",
                      "    return hashlib.sha256(user.salt + password).hexdigest() == user.password_hash",
                      "", "def login_user(request):", "    user = find_user(request.form['email'])",
                      "    if not check_password(user, request.form['password']):",
                      "        raise PermissionError('wrong password')", "    return start_session(user)"]) + "\n"


# ------------------------------------------------------------------ indexer


def test_index_skips_secrets_and_updates_incrementally(tmp_path):
    origin = tmp_path / "origin"
    make_repo(origin, {"auth/login.py": LOGIN_PY, "README.md": "# Shop\nAn online shop.\n",
                       ".env": "DB_PASSWORD=hunter2hunter2\n", "config/secrets/prod.txt": "very secret\n",
                       "settings.py": 'API_KEY = "abcdefghijklmnop1234"\n',
                       "package.json": json.dumps({"dependencies": {"react": "19"}})})
    idx = RepoIndex(tmp_path / "idx", "acme", "shop", SETTINGS, DENY, remote=str(origin), allow_file_remote=True)
    assert idx.sync(None, "main")["mode"] == "full"
    st = idx.status()
    assert st["state"] == "ready" and st["files"] == 4  # .env and secrets/ never indexed
    assert st["overview"]["manifests"]["package.json"] == ["react"]
    with __import__("sqlite3").connect(idx.db_path) as db:
        bodies = " ".join(r[0] for r in db.execute("SELECT body FROM chunks"))
    assert "hunter2" not in bodies and "very secret" not in bodies and "abcdefghijklmnop1234" not in bodies

    found = idx.search("How does the password check work at login?")
    top = found["hits"][0]
    assert top["path"] == "auth/login.py" and top["startLine"] == 1 and "check_password" in top["text"]

    new = commit(origin, {"auth/login.py": LOGIN_PY.replace("wrong password", "bad credentials"),
                          "billing/invoice.py": "def total_invoice(items):\n    return sum(items)\n"})
    res = idx.sync(None, "main", head_sha=new)
    assert res == {"mode": "incremental", "changed": 2}
    assert idx.status()["commit"] == new
    assert "bad credentials" in idx.search("login password credentials")["hits"][0]["text"]
    assert idx.search("invoice total")["hits"][0]["path"] == "billing/invoice.py"
    assert idx.sync(None, "main", head_sha=new)["mode"] == "unchanged"

    commit(origin, {"billing/invoice.py": None})
    idx.sync(None, "main")
    assert all(h["path"] != "billing/invoice.py" for h in idx.search("invoice total")["hits"])


def test_search_never_returns_stale_text(tmp_path):
    origin = tmp_path / "origin"
    make_repo(origin, {"auth/login.py": LOGIN_PY})
    idx = RepoIndex(tmp_path / "idx", "acme", "shop", SETTINGS, DENY, remote=str(origin), allow_file_remote=True)
    idx.sync(None, "main")
    (idx.checkout / "auth/login.py").write_text("def login_user(): return 'current'\n")  # index is now stale
    hit = idx.search("login user")["hits"][0]
    assert "current" in hit["text"] and "check_password" not in hit["text"]


def test_repositories_are_isolated(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    make_repo(a, {"pay.py": "def refund_payment(): pass\n"})
    make_repo(b, {"ship.py": "def schedule_shipping(): pass\n"})
    ia = RepoIndex(tmp_path / "idx", "org1", "app", SETTINGS, DENY, remote=str(a), allow_file_remote=True)
    ib = RepoIndex(tmp_path / "idx", "org2", "app", SETTINGS, DENY, remote=str(b), allow_file_remote=True)
    ia.sync(None, "main")
    ib.sync(None, "main")
    assert ia.dir != ib.dir and ia.db_path != ib.db_path
    assert ia.search("refund payment")["hits"] and not ib.search("refund payment")["hits"]
    assert ib.search("shipping schedule")["hits"] and not ia.search("shipping schedule")["hits"]
    for owner, name in (("..", "x"), ("a/b", "c"), ("ok", "../../etc")):
        with pytest.raises(IndexError_):
            RepoIndex(tmp_path / "idx", owner, name, SETTINGS, DENY)
    with pytest.raises(IndexError_):
        ia.sync(None, "main;rm -rf /")


def test_symlinks_are_not_followed(tmp_path):
    origin = tmp_path / "origin"
    make_repo(origin, {"a.py": "x = 1\n"})
    (origin / "leak.txt").symlink_to("/etc/hosts")
    commit(origin, {})
    idx = RepoIndex(tmp_path / "idx", "acme", "shop", SETTINGS, DENY, remote=str(origin), allow_file_remote=True)
    idx.sync(None, "main")
    assert not idx.search("localhost broadcasthost")["hits"]


# ------------------------------------------------------------------ RBAC


def test_rbac_rules():
    config = {"security": {"roles": {"admins": ["Ada"], "engineers": ["eve"]}}}
    assert rbac.role_of(config, "ada") == "admin" and rbac.role_of(config, "EVE") == "engineer"
    assert rbac.role_of(config, "rita") == "requester"
    task = {"login": "rita"}
    push, read = {"permissions": {"push": True}}, {"permissions": {"pull": True}}
    assert rbac.can_decide_plan("requester", "rita", task, read, "low", True)[0]
    assert not rbac.can_decide_plan("requester", "rita", task, read, "medium", True)[0]
    assert rbac.can_decide_plan("requester", "rita", task, read, "medium", False)[0]  # can always stop
    assert not rbac.can_decide_plan("requester", "bob", task, read, "low", True)[0]
    assert rbac.can_decide_plan("engineer", "eve", task, push, "medium", True)[0]
    assert not rbac.can_decide_plan("engineer", "eve", task, read, "medium", True)[0]  # role alone isn't enough
    assert not rbac.can_view_task("engineer", "eve", task, read)
    assert rbac.can_decide_changes("requester", "rita", task, read)[0]
    assert not rbac.can_decide_changes("requester", "bob", task, read)[0]


# ------------------------------------------------------------------ web fakes


class WorkGitHub(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.users.update({"ghu_eve": "eve", "ghu_ada": "ada", "ghu_mallory": "mallory"})
        self.collaborators = {"alice": "read", "bob": "read", "eve": "write", "ada": "write"}
        self.emails_granted = True

    def repo(self, login="alice"):
        r = super().repo()
        level = self.collaborators.get(login, "none")
        r["permissions"] = {"pull": level != "none", "push": level == "write"}
        return r

    def handle(self, req):
        token = req.headers.get("authorization", "").replace("Bearer ", "")
        login = self.users.get(token)
        p = req.url.path
        if login and p == "/user":
            return httpx.Response(200, json={"login": login, "name": login.title(), "avatar_url": "",
                                             "html_url": f"https://github.com/{login}"})
        if login and p == "/user/emails":
            if not self.emails_granted:
                return httpx.Response(403, json={"message": "Resource not accessible by integration"})
            return httpx.Response(200, json=[{"email": f"{login}@acme.dev", "verified": True},
                                             {"email": f"{login}@old.dev", "verified": False}])
        if login and p == "/user/orgs":
            return httpx.Response(200, json=[{"login": "octo"}])
        if login and p == "/user/installations":
            return httpx.Response(200, json={"installations": [{"id": 1, "account": {"login": "octo",
                                                                                      "type": "Organization"}}]})
        if login and p == "/user/installations/1/repositories":
            return httpx.Response(200, json={"repositories": [self.repo(login)]})
        if login and p == "/repos/octo/proj":
            if self.collaborators.get(login, "none") == "none":
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=self.repo(login))
        return super().handle(req)


class WorkAgent(FakeAgent):
    """Mimics the Go agent-service's task state machine and ask endpoint."""

    def __init__(self):
        self.code_allowed, self.build_enabled = True, True
        self.asked, self.created, self.tasks = [], [], {}
        self.ask_reply = None
        self.risk = "low"

    def capabilities(self):
        remote = not self.code_allowed  # like Go: only a remote provider can lack consent
        return {"provider": "gemini" if remote else "ollama", "model": "m", "remote": remote,
                "codeAllowed": self.code_allowed,
                "build": {"enabled": self.build_enabled, "problems": [] if self.build_enabled else ["no docker"]}}

    def ask(self, body):
        self.asked.append(body)
        if self.ask_reply:
            return self.ask_reply(body)
        c = body["chunks"][0]
        return {"answerable": True, "confidence": "high", "provider": "ollama",
                "answer": f"It's in `{c['path']}`.",
                "citations": [{"path": c["path"], "startLine": c["startLine"], "endLine": c["startLine"] + 2},
                              {"path": "made/up.py", "startLine": 1, "endLine": 5}]}

    def create_task(self, body):
        tid = len(self.tasks) + 1
        self.created.append(body)
        self.tasks[tid] = {"id": tid, "status": "awaiting_plan_approval", "terminal": False,
                           "request": body["request"], "requester": body["requester"],
                           "acceptanceCriteria": body["acceptanceCriteria"],
                           "plan": {"summary": "Do it", "risk": self.risk, "acceptance_criteria": ["Works"]}}
        return self.tasks[tid]

    def get_task(self, tid):
        if tid not in self.tasks:
            raise AgentServiceError("not found", 404)
        return self.tasks[tid]

    def decide(self, tid, stage, approve, by):
        t = self.tasks[tid]
        if stage == "plan":
            t["status"] = "awaiting_impl_approval" if approve else "plan_rejected"
            t["diff"], t["diffSha256"], t["checksOk"] = "+hello\n", "abc", True
        else:
            t["status"] = "completed" if approve else "impl_rejected"
            if approve:
                t["prUrl"] = "https://github.com/octo/proj/pull/7"
        t["terminal"] = t["status"] != "awaiting_impl_approval"
        return t


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_DISABLED", raising=False)
    origin = tmp_path / "origin"
    head = make_repo(origin, {"auth/login.py": LOGIN_PY, "README.md": "# Proj\n"})
    (tmp_path / "config.json").write_text(json.dumps({
        "web": {"port": PORT, "baseUrl": f"http://localhost:{PORT}", "dataDir": str(tmp_path / "data"),
                "clientDist": ""},
        "github": {"clientId": "Iv1.test", "clientSecret": "shh"},
        "agentService": {"token": "x" * 32},
        "security": {"killSwitchFile": str(tmp_path / "KILL"),
                     "roles": {"admins": ["ada"], "engineers": ["eve"]}},
    }))
    config = load_config(tmp_path / "config.json")
    gh, agent = WorkGitHub(), WorkAgent()
    gh.branches["main"] = head
    store = Store(str(tmp_path / "data"), bytes([7]) * 32)
    indexes = Indexes(config, remote_for=lambda o, n: str(origin), allow_file_remote=True)
    app = create_app(config, store, agent, gh.http, indexes)

    class W:
        pass
    w = W()
    w.config, w.gh, w.agent, w.store, w.origin, w.tmp, w.indexes = config, gh, agent, store, origin, tmp_path, indexes
    w.client = lambda token="ghu_alice": _signed_in(Client(app, gh), token)
    return w


def _signed_in(c, token):
    c.sign_in(token)
    return c


def build_index(w, c):
    assert c.post("/api/repos/octo/proj/index").status_code == 202
    for _ in range(100):
        if c.get("/api/repos/octo/proj/index").json()["state"] == "ready":
            return
        __import__("time").sleep(0.02)
    raise AssertionError(c.get("/api/repos/octo/proj/index").json())


# ------------------------------------------------------------------ dashboard


def test_dashboard_shows_identity_role_and_disclosure(work):
    c = work.client("ghu_eve")
    me = c.get("/api/me").json()
    assert me["login"] == "eve" and me["name"] == "Eve" and me["role"] == "engineer"
    assert me["emails"] == ["eve@acme.dev"]  # verified only
    assert me["orgs"] == ["octo"] and me["installations"][0]["account"] == "octo"
    assert "stays on your infrastructure" in me["ai"]["disclosure"]
    assert me["revokeUrl"].startswith("https://github.com/settings/")
    work.gh.emails_granted = False
    assert c.get("/api/me").json()["emails"] is None
    assert c.get("/api/session").json()["role"] == "engineer"


# ------------------------------------------------------------------ ask


def test_ask_answers_with_verified_citations(work):
    c = work.client()
    first = c.post("/api/repos/octo/proj/ask", {"question": "How is the password checked at login?"})
    assert first.json()["status"] in ("indexing", "answered")
    build_index(work, c)
    res = c.post("/api/repos/octo/proj/ask", {"question": "How is the password checked at login?"}).json()
    assert res["status"] == "answered"
    assert [x["path"] for x in res["citations"]] == ["auth/login.py"]  # made-up citation dropped
    assert res["citations"][0]["url"].startswith(f"https://github.com/octo/proj/blob/{work.gh.branches['main']}/")
    sent = work.agent.asked[-1]
    assert sent["chunks"][0]["path"] == "auth/login.py" and "Repository octo/proj" in sent["overview"]
    events = c.get("/api/activity").json()["events"]
    assert events[0]["event"] == "asked" and events[0]["detail"]["status"] == "answered"


def test_out_of_context_questions_are_not_sent_to_the_model(work):
    c = work.client()
    build_index(work, c)
    calls = len(work.agent.asked)
    res = c.post("/api/repos/octo/proj/ask", {"question": "What is the capital of Mongolia?"}).json()
    assert res["status"] == "outside_context" and "won't guess" in res["answer"]
    vague = c.post("/api/repos/octo/proj/ask", {"question": "how does it work?"}).json()
    assert vague["status"] == "needs_clarification"
    assert len(work.agent.asked) == calls


def test_model_saying_no_or_ungrounded_is_reported_honestly(work):
    c = work.client()
    build_index(work, c)
    work.agent.ask_reply = lambda b: {"answerable": False, "answer": "Not in these files.", "citations": []}
    assert c.post("/api/repos/octo/proj/ask", {"question": "password login hashing"}).json()["status"] == \
        "outside_context"
    work.agent.ask_reply = lambda b: {"answerable": True, "answer": "Trust me", "confidence": "high",
                                      "citations": [{"path": "nope.py", "startLine": 1, "endLine": 2}]}
    res = c.post("/api/repos/octo/proj/ask", {"question": "password login hashing"}).json()
    assert res["status"] == "low_confidence" and "Trust me" not in res["answer"]


def test_ask_uses_fresh_code_after_a_push(work):
    c = work.client()
    build_index(work, c)
    work.gh.branches["main"] = commit(work.origin, {"auth/login.py": LOGIN_PY.replace("sha256", "argon2")})
    c.post("/api/repos/octo/proj/ask", {"question": "How is the password hashed at login?"})
    assert "argon2" in work.agent.asked[-1]["chunks"][0]["text"]


def test_ask_refused_without_code_consent_or_access(work):
    work.agent.code_allowed = False
    c = work.client()
    res = c.post("/api/repos/octo/proj/ask", {"question": "password login"})
    assert res.status_code == 400 and "NOT authorized" in res.json()["error"]
    work.agent.code_allowed = True
    m = work.client("ghu_mallory")  # signed in, but no access to octo/proj on GitHub
    assert m.post("/api/repos/octo/proj/ask", {"question": "password login"}).status_code == 400
    assert m.get("/api/repos/octo/proj/index").status_code == 400


# ------------------------------------------------------------------ build tasks + approvals


def test_low_risk_task_flow_ends_in_a_pr_awaiting_engineers(work):
    alice = work.client()
    tid = alice.post("/api/repos/octo/proj/tasks", {"request": "Add a contact page with our email",
                                                    "acceptanceCriteria": "- Page at /contact\n- Shows email"}).json()["id"]
    sent = work.agent.created[0]
    assert sent["requester"] == "alice" and sent["baseBranch"] == "main"
    assert sent["acceptanceCriteria"] == ["Page at /contact", "Shows email"]
    assert sent["reviewers"] == ["eve"]
    t = alice.get(f"/api/tasks/{tid}").json()
    assert t["status"] == "awaiting_plan_approval" and t["can"]["decidePlan"] is True
    assert alice.post(f"/api/tasks/{tid}/plan", {"approve": True}).json()["status"] == "awaiting_impl_approval"
    done = alice.post(f"/api/tasks/{tid}/changes", {"approve": True}).json()
    assert done["status"] == "completed" and done["prUrl"] == "https://github.com/octo/proj/pull/7"
    events = [e["event"] for e in alice.get("/api/activity").json()["events"]]
    assert {"task_created", "plan_approved", "changes_approved"} <= set(events)


def test_medium_risk_plan_needs_an_engineer_with_push(work):
    work.agent.risk = "medium"
    alice = work.client()
    tid = alice.post("/api/repos/octo/proj/tasks", {"request": "Change how sessions expire after login"}).json()["id"]
    view = alice.get(f"/api/tasks/{tid}").json()
    assert view["can"]["decidePlan"] is False and "engineer" in view["can"]["why"]
    res = alice.post(f"/api/tasks/{tid}/plan", {"approve": True})
    assert res.status_code == 403
    eve = work.client("ghu_eve")
    assert any(t["id"] == tid for t in eve.get("/api/tasks").json()["tasks"])
    work.gh.collaborators["eve"] = "read"  # engineer role, but no push on this repo
    assert eve.post(f"/api/tasks/{tid}/plan", {"approve": True}).status_code == 404  # can't even see it
    work.gh.collaborators["eve"] = "write"
    assert eve.post(f"/api/tasks/{tid}/plan", {"approve": True}).json()["status"] == "awaiting_impl_approval"
    refused = [e for e in work.store.events(None) if e["event"] == "decision_refused"]
    assert refused and refused[0]["login"] == "alice"


def test_tasks_are_private_to_requester_and_repo_engineers(work):
    alice = work.client()
    tid = alice.post("/api/repos/octo/proj/tasks", {"request": "Fix the typo on the pricing page"}).json()["id"]
    bob = work.client("ghu_bob")  # can read the repo, but isn't the requester or an engineer
    assert bob.get(f"/api/tasks/{tid}").status_code == 404
    assert bob.post(f"/api/tasks/{tid}/changes", {"approve": True}).status_code == 404
    assert bob.get("/api/tasks").json()["tasks"] == []
    work.gh.collaborators["alice"] = "none"  # access revoked on GitHub
    assert alice.get(f"/api/tasks/{tid}").status_code == 404


def test_build_refused_when_unavailable_paused_or_vague(work):
    alice = work.client()
    assert alice.post("/api/repos/octo/proj/tasks", {"request": "fix"}).status_code == 400
    work.agent.build_enabled = False
    res = alice.post("/api/repos/octo/proj/tasks", {"request": "Add a dark mode toggle to settings"})
    assert res.status_code == 400 and "no docker" in res.json()["error"]
    work.agent.build_enabled = True
    (work.tmp / "KILL").touch()
    assert "paused" in alice.post("/api/repos/octo/proj/tasks",
                                  {"request": "Add a dark mode toggle to settings"}).json()["error"]


def test_lost_task_after_agent_restart(work):
    alice = work.client()
    tid = alice.post("/api/repos/octo/proj/tasks", {"request": "Add a dark mode toggle to settings"}).json()["id"]
    work.agent.tasks.clear()
    assert alice.get(f"/api/tasks/{tid}").json()["status"] == "lost"


def test_clarification_starts_a_new_task(work):
    alice = work.client()
    tid = alice.post("/api/repos/octo/proj/tasks", {"request": "Make the button bigger please"}).json()["id"]
    work.agent.tasks[1].update(status="needs_info", terminal=True, plan={"questions": ["Which button?"]})
    assert alice.get(f"/api/tasks/{tid}").json()["can"]["clarify"] is True
    new = alice.post(f"/api/tasks/{tid}/clarify", {"answers": "The checkout button"}).json()["id"]
    assert new != tid and "The checkout button" in work.agent.created[-1]["request"]


# ------------------------------------------------------------------ admin & logging


def test_admin_only_audit_and_kill_switch(work):
    alice, ada = work.client(), work.client("ghu_ada")
    assert alice.get("/api/admin/audit").status_code == 403
    assert alice.post("/api/admin/kill-switch", {"on": True}).status_code == 403
    assert ada.post("/api/admin/kill-switch", {"on": True}).json()["killSwitch"] is True
    assert (work.tmp / "KILL").exists()
    logins = {e["login"] for e in ada.get("/api/admin/audit").json()["events"]}
    assert {"alice", "ada"} <= logins
    ada.post("/api/admin/kill-switch", {"on": False})
    assert not (work.tmp / "KILL").exists()


def test_logs_never_contain_tokens(caplog):
    logger = logging.getLogger("sarvas.test")
    logger.addFilter(RedactSecrets())
    with caplog.at_level(logging.INFO, logger="sarvas.test"):
        logger.info("token %s and header Bearer abc.def.ghi and ghp_%s", "ghu_" + "a" * 30, "b" * 36)
    assert "ghu_aaaa" not in caplog.text and "ghp_bbbb" not in caplog.text and "abc.def.ghi" not in caplog.text
