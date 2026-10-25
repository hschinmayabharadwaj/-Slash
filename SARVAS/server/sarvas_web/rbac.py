"""Role-based access control.

Roles come from config (security.roles, or SARVAS_ADMINS / SARVAS_ENGINEERS):
  requester  everyone allowed to sign in: Ask, submit Build tasks, approve the
             plan of a LOW-risk task they asked for, and its diff (which opens
             a pull request -- it never merges)
  engineer   also approves MEDIUM-risk plans and anyone's diffs, and sees all
             tasks on repositories they can push to
  admin      engineer powers plus the full audit log and the kill switch

Engineer powers on a repository also require GitHub push access to it, so a
role in this file never grants more than GitHub already does. Merging is
never done here: GitHub branch protection decides who can merge.
"""
from __future__ import annotations

REQUESTER, ENGINEER, ADMIN = "requester", "engineer", "admin"
RANK = {REQUESTER: 0, ENGINEER: 1, ADMIN: 2}


def role_of(config: dict, login: str) -> str:
    roles = config["security"]["roles"]
    low = login.lower()
    if any(a.lower() == low for a in roles["admins"]):
        return ADMIN
    if any(e.lower() == low for e in roles["engineers"]):
        return ENGINEER
    return REQUESTER


def at_least(role: str, wanted: str) -> bool:
    return RANK[role] >= RANK[wanted]


def engineer_on(role: str, repo: dict) -> bool:
    """Engineer powers on THIS repository: the role plus GitHub push access."""
    return at_least(role, ENGINEER) and bool((repo.get("permissions") or {}).get("push"))


def can_view_task(role: str, login: str, task: dict, repo: dict) -> bool:
    return task["login"] == login or engineer_on(role, repo)


def can_decide_plan(role: str, login: str, task: dict, repo: dict, risk: str, approve: bool) -> tuple[bool, str]:
    if engineer_on(role, repo):
        return True, ""
    if task["login"] != login:
        return False, "Only the person who asked, or an engineer on this repository, can decide."
    if not approve:
        return True, ""  # anyone may stop their own task
    if risk != "low":
        return False, f"This plan is {risk} risk, so an engineer on this repository must approve its scope."
    return True, ""


def can_decide_changes(role: str, login: str, task: dict, repo: dict) -> tuple[bool, str]:
    if task["login"] == login or engineer_on(role, repo):
        return True, ""
    return False, "Only the person who asked, or an engineer on this repository, can decide."
