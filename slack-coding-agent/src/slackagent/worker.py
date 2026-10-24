"""Task pipeline: plan -> human approval -> implement -> verify -> human approval -> PR.

    PENDING --plan--> AWAITING_PLAN_APPROVAL --approve--> PLAN_APPROVED
      --implement + verify--> AWAITING_IMPL_APPROVAL --approve--> IMPL_APPROVED
      --apply approved diff, re-verify, push, open PR--> COMPLETED

The agent only ever proposes. Everything that decides -- policy limits, secret
scanning, protected paths, trusted checks, and both human approvals -- runs
here, in plain code outside the model's control. The PR contains exactly the
diff the approver saw (checked by SHA-256); the agent is never re-run after
approval.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

from . import guards
from .agent_io import AgentError, read_output, write_input
from .config import Config
from .database import Database, Task, TaskStatus
from .github_ops import GitHubAuthError, GitHubClient, GitHubOperationError, GitOperations
from .gitutil import GitError
from .sandbox import DockerSandbox

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    """Where the worker reports progress (Slack, or the console in CLI mode)."""

    def say(self, task: Task, text: str) -> None: ...
    def post_plan(self, task_id: int, plan: Dict[str, Any]) -> None: ...
    def post_implementation(self, task_id: int, summary: str, diff_preview: str, checks_report: str = "") -> None: ...
    def post_pr_created(self, task_id: int, pr_url: str) -> None: ...


class TaskAbort(Exception):
    """Expected stop with a user-friendly message."""

    def __init__(self, message: str, status: str = "failed"):
        super().__init__(message)
        self.status = status


class TaskSuperseded(Exception):
    """The task changed state underneath us (e.g. cancelled). Stop quietly."""


def sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()


class Worker:
    """Processes tasks from the local SQLite queue."""

    def __init__(
        self,
        config: Config,
        database: Database,
        notifier: Notifier,
        worker_id: Optional[str] = None,
        github: Optional[GitHubClient] = None,
        sandbox: Optional[DockerSandbox] = None,
    ):
        self.config = config
        self.database = database
        self.notifier = notifier
        self.worker_id = worker_id or uuid.uuid4().hex[:8]
        self.github = github or GitHubClient(config.github)
        self.sandbox = sandbox or DockerSandbox(
            config.docker,
            network_mode=config.security.network_mode,
            agent_network=config.security.agent_network,
            ai_provider=config.ai.provider,
        )
        logger.info("Worker %s initialized (ai=%s)", self.worker_id, config.ai.provider)

    # ------------------------------------------------------------------ loop

    def run_forever(self, stop: threading.Event, poll_s: Optional[float] = None) -> None:
        """Process tasks until ``stop`` is set."""
        poll_s = poll_s or self.config.worker.poll_interval
        logger.info("Worker %s starting", self.worker_id)
        while not stop.is_set():
            if guards.kill_switch_on(self.config):
                logger.warning("Kill switch active, worker paused")
                stop.wait(poll_s)
                continue
            try:
                did_work = self.process_one()
            except Exception:
                logger.exception("worker loop error")
                did_work = False
            if not did_work:
                stop.wait(poll_s)

    def process_one(self) -> bool:
        """Advance one task by one phase. Returns False if there was nothing to do."""
        for candidate in self.database.get_pending_tasks(limit=5):
            if self.database.claim_task(candidate.id, self.worker_id):
                break
        else:
            return False

        task = self.database.get_task(candidate.id)
        try:
            if task.status == TaskStatus.PENDING:
                self._process_planning(task)
            elif task.status == TaskStatus.PLAN_APPROVED:
                self._process_implementation(task)
            elif task.status == TaskStatus.IMPL_APPROVED:
                self._process_pr_creation(task)
        except TaskSuperseded:
            logger.info("Task %s changed state while running; stopping", task.id)
        except TaskAbort as e:
            self._end_task(task, str(e), e.status)
        except AgentError as e:
            self._end_task(task, str(e), "failed")
        except (GitError, GitHubOperationError, GitHubAuthError) as e:
            logger.error("Git/GitHub error on task %s: %s", task.id, e)
            self._end_task(
                task,
                "I hit a problem talking to git/GitHub. Nothing was merged. Please try again.",
                "failed",
            )
        except Exception:
            logger.exception("Task %s crashed", task.id)
            self._end_task(
                task,
                f"Something went wrong on my side (task #{task.id}). Nothing was merged.",
                "failed",
            )
        finally:
            self.database.release_task(task.id)
        return True

    # ------------------------------------------------------------------ PLANNING

    def _process_planning(self, task: Task) -> None:
        tid = task.id
        self._advance(task, TaskStatus.PENDING, TaskStatus.PLANNING)
        self._check_repo_allowed(task)
        self.notifier.say(task, ":mag: Reading the code and drafting a plan...")

        work_dir = self._fresh_work_dir(tid)
        try:
            repo_dir = work_dir / "repo"
            self._clone(task, repo_dir)

            io_dir = work_dir / "io"
            write_input(io_dir, self._agent_input(task, "plan"))
            result = self.sandbox.run_agent(
                mode="plan",
                repo_dir=repo_dir,
                io_dir=io_dir,
                image=self.config.docker.image,
                timeout_s=self.config.agent.plan_timeout_s,
                api_key=self._api_key(),
            )
            output = read_output(io_dir, result)
            plan = self._normalize_plan(output.get("plan"))
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        self.database.record_event(tid, "plan_generated", "agent", {
            "risk": plan["risk"], "files": plan["files"],
            "tokens": output.get("tokens_used"), "provider": output.get("provider"),
        })

        if not plan["feasible"] or plan["questions"]:
            lines = [":thinking_face: I can't start on this yet.", guards.escape_slack(plan["summary"])]
            lines += [f"• {guards.escape_slack(q)}" for q in plan["questions"]]
            lines.append("Reply with more detail and tag me again.")
            raise TaskAbort("\n".join(lines), "needs_info")

        protected = [f for f in plan["files"] if guards.matches_any(f, self._deny(task))]
        if plan["risk"] == "high" or protected:
            why = "it touches protected files" if protected else guards.escape_slack(plan["risk_reason"])
            raise TaskAbort(f":no_entry: I won't make this change myself ({why}). Please ask an engineer.", "declined")

        self.database.update_task_plan(tid, json.dumps(plan))
        self._advance(task, TaskStatus.PLANNING, TaskStatus.AWAITING_PLAN_APPROVAL)
        if not self.config.security.require_approval:
            self._auto_approve(task, TaskStatus.AWAITING_PLAN_APPROVAL, TaskStatus.PLAN_APPROVED)
            return
        self.notifier.post_plan(tid, plan)

    def _normalize_plan(self, raw: Any) -> Dict[str, Any]:
        """Validate the agent's plan JSON and clamp every field."""
        if not isinstance(raw, dict):
            raise TaskAbort("Couldn't produce a clear plan. Try rephrasing with more detail.")
        if "feasible" not in raw:
            raise TaskAbort("Plan missing required field 'feasible'. Try rephrasing with more detail.")
        risk = raw.get("risk") if raw.get("risk") in ("low", "medium", "high") else "medium"
        files = raw.get("files") if isinstance(raw.get("files"), list) else []
        questions = raw.get("questions") if isinstance(raw.get("questions"), list) else []
        return {
            "feasible": raw.get("feasible") is True,
            "summary": str(raw.get("summary") or "").strip()[:1500],
            "files": [str(f) for f in files][:20],
            "risk": risk,
            "risk_reason": str(raw.get("risk_reason") or "").strip()[:300],
            "questions": [str(q).strip() for q in questions if str(q).strip()][:5],
            "how_to_verify": str(raw.get("how_to_verify") or "").strip()[:600],
        }

    # ------------------------------------------------------------------ IMPLEMENTATION

    def _process_implementation(self, task: Task) -> None:
        tid = task.id
        self._advance(task, TaskStatus.PLAN_APPROVED, TaskStatus.IMPLEMENTING)
        self._check_repo_allowed(task)
        plan = json.loads(task.plan or "{}")
        self.notifier.say(task, ":hammer_and_wrench: Approved. Working on the change now...")

        branch = f"agent/{tid}-{guards.slugify(plan.get('summary', 'change'), 30)}-{secrets.token_hex(2)}"
        self.database.update_task_branch(tid, branch)

        work_dir = self._fresh_work_dir(tid)
        try:
            repo_dir = work_dir / "repo"
            self._clone(task, repo_dir)

            io_dir = work_dir / "io"
            write_input(io_dir, self._agent_input(task, "implement", plan))
            result = self.sandbox.run_agent(
                mode="implement",
                repo_dir=repo_dir,
                io_dir=io_dir,
                image=self.config.docker.image,
                timeout_s=self.config.agent.implement_timeout_s,
                api_key=self._api_key(),
            )
            output = read_output(io_dir, result)

            # Verify on the host -- never trust the sandbox's own account.
            self._advance(task, TaskStatus.IMPLEMENTING, TaskStatus.VERIFYING)
            report = guards.inspect_worktree(repo_dir)
            if not report.files:
                note = guards.escape_slack(guards.tail(output.get("text", ""), 600))
                raise TaskAbort(f"I didn't end up changing any files.\n{note}".strip())
            self._enforce_policy(task, repo_dir, report)

            diff = GitOperations.get_diff(repo_dir, cached=True)
            checks_ok, checks_report = self._run_checks(task, repo_dir)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        self.database.record_event(tid, "implementation_verified", "worker", {
            "files": [f.path for f in report.files],
            "lines": report.total_changed,
            "checks_ok": checks_ok,
            "diff_sha256": sha256(diff),
        })
        if not checks_ok and self.config.security.block_on_failed_checks:
            raise TaskAbort(
                ":x: The change broke the repository's checks, so I stopped before opening a PR.\n"
                f"```{guards.tail(checks_report, 1500)}```"
            )

        summary = guards.tail(output.get("text", "Changes made."), 2000)
        self.database.update_task_implementation(tid, summary, diff, sha256(diff), checks_report)
        self._advance(task, TaskStatus.VERIFYING, TaskStatus.AWAITING_IMPL_APPROVAL)
        if not self.config.security.require_approval:
            self._auto_approve(task, TaskStatus.AWAITING_IMPL_APPROVAL, TaskStatus.IMPL_APPROVED)
            return
        preview = diff if len(diff) <= 2500 else diff[:2500] + "\n... (truncated)"
        self.notifier.post_implementation(tid, summary, preview, checks_report)

    def _enforce_policy(self, task: Task, repo_dir: Path, report: guards.DiffReport) -> None:
        """Deterministic limits on what the change may contain."""
        sec = self.config.security
        repo_cfg = self.config.repo(self._full_name(task))
        problems: List[str] = guards.check_policy(
            report,
            max_files=repo_cfg.max_files_override or sec.max_files,
            max_changed_lines=repo_cfg.max_lines_override or sec.max_changed_lines,
            deny_patterns=self._deny(task),
        )
        limit = sec.max_file_size_mb * 1024 * 1024
        for change in report.files:
            path = repo_dir / change.path
            if path.is_file() and path.stat().st_size > limit:
                problems.append(f"`{change.path}` is larger than {sec.max_file_size_mb} MB")
        if sec.scan_secrets:
            problems += guards.scan_secrets(report.added_lines)
        if problems:
            self.database.record_event(task.id, "policy_blocked", "worker", {"problems": problems})
            raise TaskAbort(
                ":no_entry: I stopped without opening a PR because:\n"
                + "\n".join(f"• {p}" for p in problems)
                + "\nTry a smaller, more specific request.",
                "declined",
            )

    def _run_checks(self, task: Task, repo_dir: Path) -> "tuple[bool, str]":
        """Run the repo's configured lint/test commands (network off)."""
        repo_cfg = self.config.repo(self._full_name(task))
        commands = [c for c in (repo_cfg.lint_command, repo_cfg.test_command) if c]
        if not commands:
            return True, "No checks configured for this repository."
        ok, lines = True, []
        for command in commands:
            result = self.sandbox.run_checks(
                repo_dir=repo_dir,
                image=self.config.docker.image,
                command=command,
                timeout_s=self.config.agent.check_timeout_s,
            )
            if result.timed_out:
                status = f"timed out after {self.config.agent.check_timeout_s}s"
            else:
                status = "passed" if result.returncode == 0 else f"failed (exit {result.returncode})"
            ok = ok and result.returncode == 0 and not result.timed_out
            output = guards.tail(f"{result.stdout}\n{result.stderr}", 1200)
            lines.append(f"$ {command}\n{status}\n{output}")
        return ok, "\n\n".join(lines)

    # ------------------------------------------------------------------ PR CREATION

    def _process_pr_creation(self, task: Task) -> None:
        tid = task.id
        self._advance(task, TaskStatus.IMPL_APPROVED, TaskStatus.CREATING_PR)
        self._check_repo_allowed(task)
        plan = json.loads(task.plan or "{}")

        diff = task.diff or ""
        if not diff.strip():
            raise TaskAbort("There is no approved change to open a PR with.")
        if not task.diff_sha256 or sha256(diff) != task.diff_sha256:
            raise TaskAbort(":no_entry: The stored change doesn't match what was approved. Stopping.", "declined")

        work_dir = self._fresh_work_dir(tid)
        try:
            repo_dir = work_dir / "repo"
            self._clone(task, repo_dir)
            GitOperations.create_branch(repo_dir, task.branch_name)
            GitOperations.apply_patch(repo_dir, diff)

            # Defense in depth: re-check exactly what is about to be committed.
            report = guards.inspect_worktree(repo_dir)
            if not report.files:
                raise TaskAbort("No changes to commit.")
            self._enforce_policy(task, repo_dir, report)

            GitOperations.commit_changes(
                repo_dir,
                f"{plan.get('summary', task.task_description)[:72]}\n\nBot task #{tid}",
                author_name=self.config.github.commit_author_name,
                author_email=self.config.github.commit_author_email,
            )
            GitOperations.push_branch(repo_dir, task.branch_name, env=self._git_env(task))
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        title = guards.neutralize_github_mentions(
            f"[bot] {plan.get('summary', task.task_description)}"[:90]
        )
        pr_url = self.github.create_pull_request(
            task.repo_owner,
            task.repo_name,
            task.branch_name,
            title,
            self._build_pr_body(task, plan),
            base_branch=self._base_branch(task),
        )
        self.database.update_task_pr(tid, pr_url)
        self._advance(task, TaskStatus.CREATING_PR, TaskStatus.COMPLETED)
        self.database.record_event(tid, "pr_created", "worker", {"url": pr_url, "branch": task.branch_name})
        self.notifier.post_pr_created(tid, pr_url)

    def _build_pr_body(self, task: Task, plan: Dict[str, Any]) -> str:
        """PR description; all model/user text is neutralized for @-mentions."""
        safe = guards.neutralize_github_mentions
        lines = [
            "## Summary",
            safe(plan.get("summary", "")),
            "",
            "## How to verify",
            safe(plan.get("how_to_verify") or "See the changes."),
            "",
            "## Checks",
            "```",
            guards.tail(task.checks_report or "Not run.", 3000),
            "```",
            "",
            f"**Request:** {safe(task.task_description)}",
            f"**Risk:** {plan.get('risk', 'medium')}",
            f"**Bot task:** #{task.id} (plan and diff approved by a human in Slack)",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------ helpers

    def _agent_input(self, task: Task, mode: str, plan: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        agent = self.config.agent
        payload: Dict[str, Any] = {
            "request": guards.sanitize_untrusted(
                task.task_description, self.config.security.max_request_chars
            ),
            "mode": mode,
            "provider": self.config.ai.provider,
            "model": self.config.ai.model,
            "max_turns": agent.max_turns,
            "token_budget": agent.planning_budget if mode == "plan" else agent.implementation_budget,
            "deny_patterns": self._deny(task),
            "max_file_bytes": self.config.security.max_file_size_mb * 1024 * 1024,
        }
        if plan is not None:
            payload["plan"] = plan
        return payload

    def _api_key(self) -> str:
        return self.config.ai.api_key if self.config.ai.provider == "gemini" else ""

    def _clone(self, task: Task, dest: Path) -> None:
        url = self.github.get_clone_url(task.repo_owner, task.repo_name)
        GitOperations.clone_repository(url, dest, branch=self._base_branch(task), env=self._git_env(task))

    def _git_env(self, task: Task) -> Dict[str, str]:
        auth = getattr(self.github, "git_auth_env", None)
        return auth(task.repo_owner, task.repo_name) if auth else {}

    def _full_name(self, task: Task) -> str:
        return f"{task.repo_owner}/{task.repo_name}"

    def _deny(self, task: Task) -> List[str]:
        return self.config.deny_for(self._full_name(task))

    def _base_branch(self, task: Task) -> str:
        return self.config.repo(self._full_name(task)).default_branch

    def _check_repo_allowed(self, task: Task) -> None:
        if not self.config.repo_allowed(self._full_name(task)):
            raise TaskAbort(f"I'm not allowed to work on `{self._full_name(task)}`.", "declined")

    def _advance(self, task: Task, expected: TaskStatus, new: TaskStatus) -> None:
        if not self.database.transition_status(task.id, expected, new):
            raise TaskSuperseded()
        task.status = new

    def _auto_approve(self, task: Task, expected: TaskStatus, new: TaskStatus) -> None:
        """Only when security.require_approval is false (not recommended)."""
        self._advance(task, expected, new)
        self.database.record_event(task.id, f"auto_{new.value}", "config", {})

    def _work_dir(self, task_id: int) -> Path:
        return Path(self.config.database.path).resolve().parent / "work" / f"task-{task_id}"

    def _fresh_work_dir(self, task_id: int) -> Path:
        work_dir = self._work_dir(task_id)
        shutil.rmtree(work_dir, ignore_errors=True)
        work_dir.mkdir(parents=True, mode=0o700)
        return work_dir

    def _end_task(self, task: Task, message: str, status: str) -> None:
        """Mark the task finished (failed/declined/needs_info) and tell the user."""
        new = TaskStatus[status.upper()] if status.upper() in TaskStatus.__members__ else TaskStatus.FAILED
        current = self.database.get_task(task.id)
        if current and current.status in (TaskStatus.CANCELLED, TaskStatus.COMPLETED):
            return
        self.database.update_task_status(task.id, new, error_message=message)
        self.database.record_event(task.id, new.value, "worker", {"message": message[:500]})
        self.notifier.say(task, message)
