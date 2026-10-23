"""Local CLI: the same sandboxed plan -> approve -> implement -> approve pipeline
as the Slack bot, with approvals typed in the terminal.

    # a local checkout: the result is pushed as a new branch into that repo
    slack-coding-agent-cli --repo ./my-project --task "add a --version flag"

    # a GitHub repo (needs the github: section): the result is a pull request
    slack-coding-agent-cli --repo owner/name --task "fix the typo in README"

Only committed work in a local checkout is used; uncommitted edits are ignored.
"""

import argparse
import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

from . import guards
from .config import RepoConfig, load_config
from .database import Database, Task, TaskStatus
from .worker import Worker

TERMINAL = {
    TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.DECLINED,
    TaskStatus.NEEDS_INFO, TaskStatus.PLAN_REJECTED, TaskStatus.IMPL_REJECTED,
}


class ConsoleNotifier:
    """Prints worker updates to the terminal."""

    def say(self, task: Task, text: str) -> None:
        print(f"\n{text}")

    def post_plan(self, task_id: int, plan: Dict[str, Any]) -> None:
        print("\n" + "=" * 60 + "\n📋 PLAN\n" + "=" * 60)
        print(plan.get("summary", ""))
        print("\nFiles:", ", ".join(plan.get("files", [])) or "(none listed)")
        print(f"Risk: {plan.get('risk')} — {plan.get('risk_reason', '')}")
        print(f"How to verify: {plan.get('how_to_verify', '')}")

    def post_implementation(self, task_id: int, summary: str, diff_preview: str, checks_report: str = "") -> None:
        print("\n" + "=" * 60 + "\n✨ CHANGES\n" + "=" * 60)
        print(summary)
        print("\n--- diff ---\n" + diff_preview)
        if checks_report:
            print("\n--- checks ---\n" + checks_report)

    def post_pr_created(self, task_id: int, pr_url: str) -> None:
        print(f"\n🎉 {pr_url}")


class LocalRepoTarget:
    """Stands in for GitHub when working on a local checkout.

    The worker clones from the local path and "opens a PR" by pushing a new
    branch back into it. Nothing leaves the machine.
    """

    def __init__(self, path: Path):
        self.path = path.resolve()

    def get_clone_url(self, repo_owner: str, repo_name: str) -> str:
        return str(self.path)

    def git_auth_env(self, repo_owner: str, repo_name: str) -> Dict[str, str]:
        return {}

    def create_pull_request(self, repo_owner, repo_name, head_branch, title, body, base_branch="main") -> str:
        return (f"Branch '{head_branch}' was created in {self.path} "
                f"(review with: git -C {self.path} diff {base_branch}...{head_branch})")


def _ask(prompt: str) -> bool:
    try:
        return input(f"\n{prompt} [y/N]: ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _current_branch(path: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the coding agent locally (no Slack).")
    parser.add_argument("--config", default="config.yaml", help="configuration file")
    parser.add_argument("--repo", required=True, help="path to a local git checkout, or owner/repo on GitHub")
    parser.add_argument("--task", required=True, help="what you want changed")
    parser.add_argument("--plan-only", action="store_true", help="stop after showing the plan")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    local_path = Path(args.repo).expanduser()
    is_local = local_path.is_dir()
    if is_local and not (local_path / ".git").exists():
        sys.exit(f"❌ {local_path} is not a git repository")

    try:
        config = load_config(args.config, require=() if is_local else ("github",))
    except Exception as e:
        sys.exit(f"❌ Failed to load configuration: {e}")

    if is_local:
        owner, name = "local", guards.slugify(local_path.resolve().name, 60)
        full_name = f"{owner}/{name}"
        repo_cfg = config.repos.get(full_name) or RepoConfig()
        repo_cfg.default_branch = _current_branch(local_path)
        config.repos[full_name] = repo_cfg
        if config.security.allowed_repos:
            config.security.allowed_repos.append(full_name)
        target = LocalRepoTarget(local_path)
    else:
        if "/" not in args.repo:
            sys.exit("❌ --repo must be a local path or owner/repo")
        owner, name = args.repo.split("/", 1)
        full_name = args.repo
        target = None  # Worker uses the real GitHub client
        if not config.repo_allowed(full_name):
            sys.exit(f"❌ {full_name} is not in security.allowed_repos")

    with tempfile.TemporaryDirectory(prefix="slackagent-cli-") as tmp:
        config.database.path = str(Path(tmp) / "cli.db")
        database = Database(config.database.path)
        worker = Worker(config, database, ConsoleNotifier(), worker_id="cli", github=target)

        problems = worker.sandbox.preflight([config.docker.image])
        if problems:
            for problem in problems:
                print(f"❌ {problem}", file=sys.stderr)
            sys.exit(1)

        task = database.create_task(
            slack_user_id="cli", slack_channel_id="cli", slack_thread_ts="cli",
            repo_owner=owner, repo_name=name, branch_name="", task_description=args.task,
        )
        print(f"🤖 Task #{task.id} on {full_name} (AI: {config.ai.provider})")

        while True:
            worker.process_one()
            task = database.get_task(task.id)

            if task.status == TaskStatus.AWAITING_PLAN_APPROVAL:
                if args.plan_only:
                    print("\n✅ Plan only — stopping here.")
                    return
                approved = _ask("Approve this plan and let the agent make the change?")
                new = TaskStatus.PLAN_APPROVED if approved else TaskStatus.PLAN_REJECTED
                database.transition_status(task.id, TaskStatus.AWAITING_PLAN_APPROVAL, new)
            elif task.status == TaskStatus.AWAITING_IMPL_APPROVAL:
                where = "push a branch into your repo" if is_local else "push a branch and open a pull request"
                approved = _ask(f"Approve these changes and {where}?")
                new = TaskStatus.IMPL_APPROVED if approved else TaskStatus.IMPL_REJECTED
                database.transition_status(task.id, TaskStatus.AWAITING_IMPL_APPROVAL, new)
            elif task.status in TERMINAL:
                if task.status != TaskStatus.COMPLETED:
                    print(f"\nStopped: {task.status.value}")
                if args.verbose:
                    print(json.dumps(database.list_events(task.id), indent=2, default=str))
                sys.exit(0 if task.status == TaskStatus.COMPLETED else 1)


if __name__ == "__main__":
    main()
