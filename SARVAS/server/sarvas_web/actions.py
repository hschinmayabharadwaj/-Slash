"""The changes the page may make, and how they are checked:
  1. authorize: repo allowlisted, not archived, GitHub says the user can push
  2. propose: validate + build a preview diff (nothing changes yet)
  3. apply (after approving the exact preview): re-check the repo is unchanged,
     commit on a NEW branch, open a pull request. Never writes the default branch.
"""
from __future__ import annotations

import hashlib
import os
import secrets

from .config import repo_allowed
from .github import GitHubError, GitHubUser

README = "README.md"
ACTIONS = {
    "create_readme": "Add a README.md",
    "delete_readme": "Delete README.md",
}


class ActionRefused(Exception):
    pass


def fingerprint(p: dict) -> str:
    parts = [p["action"], p["owner"], p["name"], p["path"], p["baseBranch"], p["previousSha"], p["content"]]
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def file_diff(old_text: str, new_text: str, path: str) -> str:
    """Whole-file unified diff (create or delete)."""
    def lines(t: str) -> list[str]:
        return t.removesuffix("\n").split("\n") if t else []
    a, b = lines(old_text), lines(new_text)
    header = [f"--- {f'a/{path}' if a else '/dev/null'}", f"+++ {f'b/{path}' if b else '/dev/null'}",
              f"@@ -{1 if a else 0},{len(a)} +{1 if b else 0},{len(b)} @@"]
    return "\n".join(header + [f"-{l}" for l in a] + [f"+{l}" for l in b]) + "\n"


def authorize(gh: GitHubUser, config: dict, owner: str, name: str) -> dict:
    full_name = f"{owner}/{name}"
    if not repo_allowed(config, full_name):
        raise ActionRefused(f"{full_name} is not in this agent's allowed repositories.")
    try:
        repo = gh.get_repo(owner, name)
    except GitHubError as e:
        if e.status in (403, 404):
            raise ActionRefused(f"You don't have access to {full_name}, or the app isn't installed there.") from None
        raise
    if repo.get("archived"):
        raise ActionRefused(f"{full_name} is archived.")
    if not (repo.get("permissions") or {}).get("push"):
        raise ActionRefused(f"You don't have write access to {full_name}, so I can't change it for you.")
    return repo


def propose(gh: GitHubUser, agent, config: dict, owner: str, name: str, action: str, content: str = "") -> dict:
    if action not in ACTIONS:
        raise ActionRefused("That action isn't supported yet.")
    repo = authorize(gh, config, owner, name)
    base_branch = repo["default_branch"]
    current = gh.get_file(owner, name, README, base_branch)
    base = {"action": action, "owner": owner, "name": name, "path": README, "baseBranch": base_branch}

    if action == "create_readme":
        if current:
            raise ActionRefused(f"{owner}/{name} already has a {README}.")
        text = (content or "").replace("\r\n", "\n").strip() + "\n"
        problems = agent.check_readme(text if text.strip() else "")
        if problems:
            raise ActionRefused(f"Refused: {'; '.join(problems)}.")
        return {**base, "content": text, "previousSha": "", "diff": file_diff("", text, README)}
    if not current:
        raise ActionRefused(f"{owner}/{name} has no {README} to delete.")
    return {**base, "content": "", "previousSha": current["sha"], "diff": file_diff(current["text"], "", README)}


def apply(gh: GitHubUser, config: dict, p: dict, actor: str) -> str:
    if os.environ.get("BOT_DISABLED") == "1" or os.path.exists(config["security"]["killSwitchFile"]):
        raise ActionRefused("The agent is paused (kill switch is on).")
    authorize(gh, config, p["owner"], p["name"])
    current = gh.get_file(p["owner"], p["name"], p["path"], p["baseBranch"])
    if (current or {}).get("sha", "") != p["previousSha"]:
        raise ActionRefused(f"{p['path']} changed since the preview. Please start again.")
    verb = "add" if p["action"] == "create_readme" else "remove"
    branch = f"agent/{verb}-readme-{secrets.token_hex(3)}"
    message = f"docs: {verb} {p['path']}"
    gh.create_branch(p["owner"], p["name"], branch, gh.branch_sha(p["owner"], p["name"], p["baseBranch"]))
    try:
        if p["action"] == "create_readme":
            gh.put_file(p["owner"], p["name"], p["path"], p["content"], message, branch)
        else:
            gh.delete_file(p["owner"], p["name"], p["path"], p["previousSha"], message, branch)
        # Zero-width space after @ so the actor isn't pinged.
        body = (f"{ACTIONS[p['action']]}.\n\nRequested and approved by @​{actor} in the SARVAS page "
                "after reviewing this exact diff.\n")
        return gh.create_pull_request(p["owner"], p["name"], branch, p["baseBranch"], message, body)
    except Exception:
        try:
            gh.delete_branch(p["owner"], p["name"], branch)  # don't leave debris
        except Exception:
            pass
        raise
