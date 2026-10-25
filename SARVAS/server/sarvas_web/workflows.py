"""Dashboard, Ask, index and Build-task routes (registered by app.create_app).

Every repository route re-checks, with the signed-in user's own GitHub
token, that they can see the repository -- so the shared local index and
task records never reveal a repository to someone GitHub wouldn't show it to.
Role checks (rbac.py) come on top of that, never instead of it.
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import Depends, FastAPI

from . import rbac
from .actions import ActionRefused
from .agent_client import AgentServiceError
from .config import repo_allowed
from .github import GitHubError, GitHubUser
from .indexer import IndexError_, Indexes

MIN_COVERAGE = 0.5  # share of the question's terms the best excerpt must contain
MAX_CRITERIA = 10
TASK_FIELDS = ("status", "terminal", "request", "requester", "baseBranch", "acceptanceCriteria", "plan", "branch",
               "summary", "diff", "diffTruncated", "diffSha256", "checks", "checksOk", "files", "prUrl", "message",
               "created", "updated", "events")


def authorize_read(gh: GitHubUser, config: dict, owner: str, name: str) -> dict:
    full_name = f"{owner}/{name}"
    if not repo_allowed(config, full_name):
        raise ActionRefused(f"{full_name} is not in this agent's allowed repositories.")
    try:
        repo = gh.get_repo(owner, name)
    except GitHubError as e:
        if e.status in (403, 404):
            raise ActionRefused(f"You don't have access to {full_name}, or the app isn't installed there.") from None
        raise
    if not (repo.get("permissions") or {}).get("pull"):
        raise ActionRefused(f"You don't have read access to {full_name}.")
    return repo


def github_url(url) -> str | None:
    return url if isinstance(url, str) and url.startswith("https://github.com/") else None


def register(app: FastAPI, *, config: dict, store, agent, indexes: Indexes, signed_in, csrf, record, ApiError):
    sec = config["security"]
    caps_cache: dict = {"at": 0.0, "value": None}

    def capabilities() -> dict:
        if caps_cache["value"] is None or time.time() - caps_cache["at"] > 20:
            try:
                caps_cache["value"] = agent.capabilities()
            except AgentServiceError:
                caps_cache["value"] = {"provider": "unknown", "remote": True, "codeAllowed": False,
                                       "build": {"enabled": False, "problems": ["the agent service is not reachable"]}}
            caps_cache["at"] = time.time()
        return caps_cache["value"]

    def disclosure(caps: dict) -> str:
        if caps.get("provider") == "sim":
            return "Offline simulator: nothing leaves this server, and answers are placeholders."
        if not caps.get("remote"):
            return f"Self-hosted model ({caps.get('provider')}): repository code stays on your infrastructure."
        if caps.get("codeAllowed"):
            return (f"Remote model ({caps.get('provider')}): the excerpts used to answer, and Build task files, "
                    "are sent to that provider. An administrator authorized this.")
        return (f"Remote model ({caps.get('provider')}): sending repository code to it is NOT authorized, "
                "so Ask and Build are off. An administrator can set ai.sendCodeToProvider or use a self-hosted model.")

    def kill_switch_on() -> bool:
        return Path(sec["killSwitchFile"]).exists()

    def role(user) -> str:
        return rbac.role_of(config, user.login)

    def per_hour(user, event: str, limit_key: str, what: str) -> None:
        if store.events_last_hour(user.login, event) >= sec[limit_key]:
            raise ActionRefused(f"You've hit the hourly limit for {what}. Try again later.")

    # ---------------------------------------------------------------- dashboard

    @app.get("/api/me")
    def me(user=Depends(signed_in)):
        profile = user.gh.get_user()
        emails = user.gh.get_emails()
        orgs = user.gh.get_orgs()
        installs = user.gh.installations()
        caps = capabilities()
        return {
            "login": profile["login"], "name": profile.get("name") or "", "avatarUrl": profile.get("avatar_url") or "",
            "profileUrl": github_url(profile.get("html_url")),
            "emails": None if emails is None else [e["email"] for e in emails if e.get("verified")],
            "orgs": orgs if orgs is not None else sorted(
                {i["account"] for i in installs if i["type"] == "Organization"}),
            "installations": installs, "role": role(user),
            "ai": {"provider": caps.get("provider"), "model": caps.get("model"), "remote": caps.get("remote"),
                   "codeAllowed": caps.get("codeAllowed"), "disclosure": disclosure(caps)},
            "build": caps.get("build"), "killSwitch": kill_switch_on(),
            "revokeUrl": "https://github.com/settings/apps/authorizations",
        }

    # ---------------------------------------------------------------- index

    def index_view(owner: str, name: str, head: str | None) -> dict:
        idx = indexes.get(owner, name)
        st = idx.status()
        job = indexes.job(owner, name)
        if job and job["state"] == "indexing":
            st = {**st, "state": "indexing"}
        elif job and job["state"] == "error":
            st = {**st, "error": job.get("error")}
        st["stale"] = bool(st.get("commit") and head and st["commit"] != head)
        return st

    @app.get("/api/repos/{owner}/{name}/index")
    def index_status(owner: str, name: str, user=Depends(signed_in)):
        r = authorize_read(user.gh, config, owner, name)
        return index_view(owner, name, user.gh.branch_sha(owner, name, r["default_branch"]))

    @app.post("/api/repos/{owner}/{name}/index", status_code=202)
    def index_update(owner: str, name: str, user=Depends(signed_in), _body: dict = Depends(csrf)):
        r = authorize_read(user.gh, config, owner, name)
        head = user.gh.branch_sha(owner, name, r["default_branch"])
        indexes.start(owner, name, user.gh.token, r["default_branch"], head)
        record(user.login, "index_requested", {"repo": r["full_name"]})
        return index_view(owner, name, head)

    # ---------------------------------------------------------------- ask

    @app.post("/api/repos/{owner}/{name}/ask")
    def ask(owner: str, name: str, user=Depends(signed_in), body: dict = Depends(csrf)):
        question = " ".join(str(body.get("question") or "").split())[:2000]
        if not question:
            raise ActionRefused("Type a question about this repository.")
        per_hour(user, "asked", "maxQuestionsPerHour", "questions")
        r = authorize_read(user.gh, config, owner, name)
        caps = capabilities()
        if not caps.get("codeAllowed"):
            raise ActionRefused(disclosure(caps))
        branch = r["default_branch"]
        head = user.gh.branch_sha(owner, name, branch)
        idx = indexes.get(owner, name)
        st = idx.status()
        if st["state"] != "ready":
            indexes.start(owner, name, user.gh.token, branch, head)
            return {"status": "indexing", "answer": "I'm reading the repository for the first time. "
                    "Ask again in a minute.", "citations": [], "sources": []}
        if st["commit"] != head:  # never answer from a stale index
            try:
                idx.sync(user.gh.token, branch, head)
            except IndexError_ as e:
                raise ActionRefused(f"Couldn't update the index: {e}") from None
        commit = idx.status()["commit"]
        found = idx.search(question)
        hits = found["hits"]
        sources = [{k: h[k] for k in ("path", "startLine", "endLine")} for h in hits]
        for s in sources:
            s["url"] = f"https://github.com/{owner}/{name}/blob/{commit}/{s['path']}#L{s['startLine']}-L{s['endLine']}"
        short = commit[:7]

        def done(status: str, answer: str, citations=(), provider=None) -> dict:
            record(user.login, "asked", {"repo": r["full_name"], "commit": short, "status": status,
                                         "citations": len(citations), "question": question[:200]})
            return {"status": status, "answer": answer, "citations": list(citations), "sources": sources,
                    "commit": commit, "provider": provider or caps.get("provider"), "disclosure": disclosure(caps)}

        if not found["terms"]:
            return done("needs_clarification", "Your question doesn't name anything I can look up in "
                        f"{r['full_name']}. Mention a feature, page, file, function or behaviour.")
        strong = [h for h in hits if h["coverage"] >= MIN_COVERAGE or len(h["matched"]) >= 2]
        if not strong:
            return done("outside_context", f"I couldn't find anything in {r['full_name']} (commit {short}) about "
                        f"“{', '.join(found['terms'])}”. It may be outside this repository or worded differently, "
                        "so I won't guess. Try other words, or ask an engineer.")
        try:
            out = agent.ask({"repo": r["full_name"], "question": question, "overview": idx.overview_text(),
                             "chunks": [{k: h[k] for k in ("path", "startLine", "endLine", "text")} for h in strong]})
        except AgentServiceError as e:
            if e.status == 403:
                raise ActionRefused(disclosure({**caps, "codeAllowed": False})) from None
            raise
        # Keep only citations that point inside an excerpt the model was given.
        cites = []
        for c in out.get("citations") or []:
            for h in strong:
                try:
                    a, b = int(c.get("startLine", 0)), int(c.get("endLine", 0))
                except (TypeError, ValueError):
                    continue
                if c.get("path") == h["path"] and a <= h["endLine"] and b >= h["startLine"]:
                    a, b = max(a, h["startLine"]), min(max(a, b), h["endLine"])
                    cites.append({"path": h["path"], "startLine": a, "endLine": b, "url":
                                  f"https://github.com/{owner}/{name}/blob/{commit}/{h['path']}#L{a}-L{b}"})
                    break
        if not out.get("answerable"):
            return done("outside_context", out.get("answer") or "That isn't answerable from this repository's "
                        "files.", cites, out.get("provider"))
        if not cites:
            return done("low_confidence", "I found related code (listed below) but couldn't tie an answer to "
                        "specific lines, so I won't guess. Please rephrase or ask an engineer.", (),
                        out.get("provider"))
        if out.get("confidence") == "low":
            return done("low_confidence", "I'm not confident enough to answer this from the code I found. "
                        "The most relevant places are listed below; an engineer can confirm.", cites,
                        out.get("provider"))
        return done("answered", out.get("answer") or "", cites, out.get("provider"))

    # ---------------------------------------------------------------- build tasks

    def task_view(rec: dict, user, repo: dict) -> dict:
        v = rec["view"]
        r = role(user)
        risk = (v.get("plan") or {}).get("risk", "medium")
        status = v.get("status")
        plan_ok, plan_why = rbac.can_decide_plan(r, user.login, rec, repo, risk, True)
        changes_ok, changes_why = rbac.can_decide_changes(r, user.login, rec, repo)
        out = {k: v.get(k) for k in TASK_FIELDS}
        out.update(id=rec["id"], owner=rec["owner"], name=rec["name"], fullName=f"{rec['owner']}/{rec['name']}",
                   requester=rec["login"], prUrl=github_url(v.get("prUrl")), mine=rec["login"] == user.login)
        out["can"] = {
            "decidePlan": status == "awaiting_plan_approval" and plan_ok,
            "rejectPlan": status == "awaiting_plan_approval" and rbac.can_decide_plan(
                r, user.login, rec, repo, risk, False)[0],
            "decideChanges": status == "awaiting_impl_approval" and changes_ok,
            "clarify": status == "needs_info" and rec["login"] == user.login,
            "why": plan_why if status == "awaiting_plan_approval" else (
                changes_why if status == "awaiting_impl_approval" else ""),
        }
        return out

    def refresh(rec: dict) -> dict:
        if rec["view"].get("terminal") or rec["view"].get("status") == "lost":
            return rec
        try:
            view = agent.get_task(rec["agent_id"])
        except AgentServiceError as e:
            if e.status != 404:
                raise
            view = {**rec["view"], "status": "lost", "terminal": True,
                    "message": "The agent service restarted and lost this task. Nothing was changed; start it again."}
        if view.get("status") != rec["view"].get("status"):
            record(rec["login"], "task_status", {"task": rec["id"], "repo": f"{rec['owner']}/{rec['name']}",
                                                 "status": view.get("status"), "pr": github_url(view.get("prUrl"))})
        store.update_task(rec["id"], view)
        return {**rec, "view": view}

    def load_task(tid: str, user) -> tuple[dict, dict]:
        rec = store.get_task(tid)
        if not rec:
            raise ApiError(404, "Not found.")
        try:
            repo = authorize_read(user.gh, config, rec["owner"], rec["name"])
        except ActionRefused:
            raise ApiError(404, "Not found.") from None
        if not rbac.can_view_task(role(user), user.login, rec, repo):
            raise ApiError(404, "Not found.")
        return refresh(rec), repo

    def criteria_list(value) -> list[str]:
        items = value if isinstance(value, list) else str(value or "").split("\n")
        out = [" ".join(str(i).lstrip("-*• ").split())[:300] for i in items]
        return [i for i in out if i][:MAX_CRITERIA]

    def start_task(user, owner: str, name: str, request: str, criteria: list[str]) -> str:
        if kill_switch_on():
            raise ActionRefused("The agent is paused by an administrator.")
        if store.tasks_last_hour(user.login) >= sec["maxTasksPerHour"]:
            raise ActionRefused("You've hit the hourly limit for Build tasks. Try again later.")
        r = authorize_read(user.gh, config, owner, name)
        if r.get("archived"):
            raise ActionRefused(f"{r['full_name']} is archived.")
        caps = capabilities()
        build = caps.get("build") or {}
        if not build.get("enabled"):
            raise ActionRefused("Build tasks aren't available on this server: " +
                                "; ".join(build.get("problems") or ["unknown reason"]) + ".")
        reviewers = [e for e in sec["roles"]["engineers"] if e.lower() != user.login.lower()][:10]
        view = agent.create_task({"owner": owner, "name": name, "baseBranch": r["default_branch"],
                                  "request": request, "acceptanceCriteria": criteria,
                                  "requester": user.login, "reviewers": reviewers})
        tid = store.save_task(user.login, owner, name, int(view["id"]), view)
        record(user.login, "task_created", {"repo": r["full_name"], "task": tid, "request": request[:200],
                                            "criteria": len(criteria)})
        return tid

    @app.post("/api/repos/{owner}/{name}/tasks", status_code=201)
    def create_task(owner: str, name: str, user=Depends(signed_in), body: dict = Depends(csrf)):
        request = str(body.get("request") or "").strip()
        if len(request) < 10:
            raise ActionRefused("Describe the change in a sentence or two (what should happen, and where).")
        if len(request) > 2000:
            raise ActionRefused("Please keep the request under 2000 characters.")
        return {"id": start_task(user, owner, name, request, criteria_list(body.get("acceptanceCriteria")))}

    @app.get("/api/tasks")
    def list_tasks(user=Depends(signed_in)):
        repos = None
        if rbac.at_least(role(user), rbac.ENGINEER):
            repos = {r["full_name"].lower() for r in user.gh.list_repos()
                     if (r.get("permissions") or {}).get("push")}
        rows = store.list_tasks(user.login, repos)
        return {"tasks": [{"id": t["id"], "fullName": f"{t['owner']}/{t['name']}", "requester": t["login"],
                           "status": t["view"].get("status"), "request": (t["view"].get("request") or "")[:200],
                           "prUrl": github_url(t["view"].get("prUrl")), "created": t["view"].get("created")}
                          for t in rows]}

    @app.get("/api/tasks/{tid}")
    def get_task(tid: str, user=Depends(signed_in)):
        rec, repo = load_task(tid, user)
        return task_view(rec, user, repo)

    def decide(tid: str, user, body: dict, stage: str) -> dict:
        rec, repo = load_task(tid, user)
        approve = body.get("approve") is True
        if approve and kill_switch_on():
            raise ActionRefused("The agent is paused by an administrator.")
        status = rec["view"].get("status")
        if stage == "plan":
            if status != "awaiting_plan_approval":
                raise ActionRefused("This task isn't waiting for a plan decision.")
            risk = (rec["view"].get("plan") or {}).get("risk", "medium")
            ok, why = rbac.can_decide_plan(role(user), user.login, rec, repo, risk, approve)
        else:
            if status != "awaiting_impl_approval":
                raise ActionRefused("This task isn't waiting for a decision on its changes.")
            ok, why = rbac.can_decide_changes(role(user), user.login, rec, repo)
        if not ok:
            record(user.login, "decision_refused", {"task": tid, "stage": stage, "reason": why})
            raise ApiError(403, why)
        view = agent.decide(rec["agent_id"], stage, approve, user.login)
        store.update_task(tid, view)
        record(user.login, f"{stage}_{'approved' if approve else 'rejected'}",
               {"task": tid, "repo": f"{rec['owner']}/{rec['name']}", "requester": rec["login"],
                "diffSha256": view.get("diffSha256") if stage == "changes" else None})
        return task_view({**rec, "view": view}, user, repo)

    @app.post("/api/tasks/{tid}/plan")
    def decide_plan(tid: str, user=Depends(signed_in), body: dict = Depends(csrf)):
        return decide(tid, user, body, "plan")

    @app.post("/api/tasks/{tid}/changes")
    def decide_changes(tid: str, user=Depends(signed_in), body: dict = Depends(csrf)):
        return decide(tid, user, body, "changes")

    @app.post("/api/tasks/{tid}/clarify", status_code=201)
    def clarify(tid: str, user=Depends(signed_in), body: dict = Depends(csrf)):
        rec, _ = load_task(tid, user)
        if rec["login"] != user.login or rec["view"].get("status") != "needs_info":
            raise ActionRefused("Only the person who asked can answer the agent's questions.")
        answers = str(body.get("answers") or "").strip()[:1500]
        if not answers:
            raise ActionRefused("Write your answers first.")
        request = f"{rec['view'].get('request', '')}\n\nClarifications from the requester:\n{answers}"[:2000]
        new_id = start_task(user, rec["owner"], rec["name"], request, rec["view"].get("acceptanceCriteria") or [])
        record(user.login, "task_clarified", {"task": tid, "next": new_id})
        return {"id": new_id}

    # ---------------------------------------------------------------- admin

    def admin(user=Depends(signed_in)):
        if role(user) != rbac.ADMIN:
            raise ApiError(403, "Administrators only.")
        return user

    @app.get("/api/admin/audit")
    def admin_audit(user=Depends(admin)):
        return {"events": store.events(None, 500)}

    @app.post("/api/admin/kill-switch")
    def admin_kill_switch(user=Depends(admin), body: dict = Depends(csrf)):
        path = Path(sec["killSwitchFile"])
        if body.get("on") is True:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        else:
            path.unlink(missing_ok=True)
        record(user.login, "kill_switch", {"on": path.exists()})
        return {"killSwitch": path.exists()}

    @app.post("/api/admin/repos/{owner}/{name}/forget")
    def admin_forget(owner: str, name: str, user=Depends(admin), _body: dict = Depends(csrf)):
        """Delete this server's checkout and index of a repository."""
        indexes.get(owner, name).remove()
        record(user.login, "index_deleted", {"repo": f"{owner}/{name}"})
        return {"ok": True}
