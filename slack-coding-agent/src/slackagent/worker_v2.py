"""Enhanced worker with robust error handling, plan validation, and policy verification.

The worker drives each task through stages:
    PENDING → [plan] → AWAITING_PLAN_APPROVAL → (human approves) → PLAN_APPROVED
           → [implement] → verify (policy + secrets + checks) → push → PR → COMPLETED
"""
from __future__ import annotations

import json
import logging
import secrets
import shutil
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from . import guards
from .agent_io import AgentError, read_output, write_input
from .config import Config
from .database import Database, Task, TaskStatus
from .github_ops import GitHubClient, GitOperations, GitHubOperationError
from .gitutil import GitError
from .sandbox import DockerSandbox
from .slack_handler import SlackHandler

logger = logging.getLogger(__name__)


class TaskAbort(Exception):
    """Expected failure with user-friendly message."""
    
    def __init__(self, message: str, status: str = "failed"):
        super().__init__(message)
        self.status = status


class Worker:
    """Enhanced background worker with policy enforcement."""
    
    def __init__(
        self,
        config: Config,
        database: Database,
        slack_handler: SlackHandler,
        worker_id: Optional[str] = None,
    ):
        """Initialize worker.
        
        Args:
            config: Application configuration
            database: Database instance
            slack_handler: Slack handler for notifications
            worker_id: Optional worker identifier
        """
        self.config = config
        self.database = database
        self.slack_handler = slack_handler
        self.worker_id = worker_id or str(uuid.uuid4())[:8]
        
        # Initialize clients
        self.github_client = GitHubClient(config.github)
        self.sandbox = DockerSandbox(config.docker, config.security.network_mode)
        
        logger.info(f"Worker {self.worker_id} initialized")
    
    def run_forever(self, stop: threading.Event, poll_s: float = None) -> None:
        """Run worker loop until stopped.
        
        Args:
            stop: Event to signal shutdown
            poll_s: Poll interval in seconds
        """
        poll_s = poll_s or self.config.worker.poll_interval
        logger.info(f"Worker {self.worker_id} starting")
        
        while not stop.is_set():
            # Check kill switch
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
        """Process one task. Returns False if nothing to do."""
        tasks = self.database.get_pending_tasks(limit=1)
        
        if not tasks:
            return False
        
        task = tasks[0]
        
        # Try to claim
        if not self.database.claim_task(task.id, self.worker_id):
            return False
        
        logger.info(f"Worker {self.worker_id} processing task {task.id}")
        
        try:
            if task.status == TaskStatus.PENDING:
                self._process_planning(task)
            elif task.status == TaskStatus.PLAN_APPROVED:
                self._process_implementation(task)
            elif task.status == TaskStatus.IMPL_APPROVED:
                self._process_pr_creation(task)
        except TaskAbort as e:
            self._end_task(task, str(e), e.status)
        except AgentError as e:
            self._end_task(task, str(e), "failed")
        except GitError as e:
            logger.error(f"Git error on task {task.id}: {e}")
            self._end_task(
                task,
                "I hit a problem with git/GitHub. Nothing was merged. Please try again.",
                "failed"
            )
        except Exception:
            logger.exception(f"Task {task.id} crashed")
            self._end_task(
                task,
                f"Something went wrong on my side (task #{task.id}). Nothing was changed. "
                "Please try again or check logs.",
                "failed"
            )
        finally:
            self.database.release_task(task.id)
        
        return True
    
    # ------------------------------------------------------------------ PLANNING
    
    def _process_planning(self, task: Task) -> None:
        """Run planning phase and post plan for approval."""
        tid = task.id
        self.database.update_task_status(tid, TaskStatus.PLANNING)
        
        self._say(task, ":mag: Reading the code and drafting a plan...")
        
        # Create working directory
        work_dir = self._work_dir(tid)
        work_dir.mkdir(parents=True, exist_ok=True)
        
        try:
            # Clone repository
            clone_url = self.github_client.get_clone_url(task.repo_owner, task.repo_name)
            repo_dir = work_dir / "repo"
            GitOperations.clone_repository(clone_url, repo_dir)
            
            # Prepare agent input
            io_dir = work_dir / "io"
            agent_input = {
                "request": task.task_description,
                "mode": "plan",
                "model": self.config.agent.model,
                "planning_budget": self.config.agent.planning_budget,
                "max_turns": self.config.agent.max_turns,
            }
            
            write_input(io_dir, agent_input)
            
            # Run planning in sandbox
            result = self.sandbox.run_agent(
                mode="plan",
                repo_dir=repo_dir,
                io_dir=io_dir,
                image=self.config.docker.image,
                timeout_s=300,
                api_key=self.config.gemini.api_key,
            )
            
            # Read output
            output = read_output(io_dir, result)
            plan = self._normalize_plan(output.get("plan"))
            
            # Check if feasible
            if not plan["feasible"] or plan["questions"]:
                lines = [":thinking_face: I can't start on this yet."]
                lines.append(guards.escape_slack(plan["summary"]))
                lines += [f"• {guards.escape_slack(q)}" for q in plan["questions"]]
                lines.append("Reply with more detail and tag me again.")
                self._say(task, "\n".join(lines))
                self._end_task(task, "needs_info", "needs_info")
                return
            
            # Check risk level
            deny_patterns = self.config.security.protected_paths
            protected = [f for f in plan["files"] if guards.matches_any(f, deny_patterns)]
            
            if plan["risk"] == "high" or protected:
                why = (
                    "touches protected files" if protected
                    else guards.escape_slack(plan["risk_reason"])
                )
                self._say(
                    task,
                    f":no_entry: I won't make this change myself ({why}). "
                    "Please ask an engineer."
                )
                self._end_task(task, "declined", "declined")
                return
            
            # Save plan and transition to approval
            self.database.update_task_plan(tid, json.dumps(plan))
            self.database.update_task_status(tid, TaskStatus.AWAITING_PLAN_APPROVAL)
            
            # Post plan to Slack
            self.slack_handler.post_plan(tid, plan["summary"])
            
        finally:
            # Cleanup
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)
    
    def _normalize_plan(self, raw: Any) -> Dict[str, Any]:
        """Validate and normalize plan JSON."""
        if not isinstance(raw, dict):
            raise TaskAbort("Couldn't produce a clear plan. Try rephrasing with more detail.")
        if "feasible" not in raw:
            raise TaskAbort("Plan missing required field 'feasible'. Try rephrasing with more detail.")
        
        risk = raw.get("risk") if raw.get("risk") in ("low", "medium", "high") else "medium"
        
        return {
            "feasible": bool(raw.get("feasible", False)),
            "summary": str(raw.get("summary") or "").strip()[:1500],
            "files": [str(f) for f in (raw.get("files") or [])][:20],
            "risk": risk,
            "risk_reason": str(raw.get("risk_reason") or "").strip()[:300],
            "questions": [str(q).strip() for q in (raw.get("questions") or []) if str(q).strip()][:5],
            "how_to_verify": str(raw.get("how_to_verify") or "").strip()[:600],
        }
    
    # ------------------------------------------------------------------ IMPLEMENTATION
    
    def _process_implementation(self, task: Task) -> None:
        """Run implementation phase and verify changes."""
        tid = task.id
        self.database.update_task_status(tid, TaskStatus.IMPLEMENTING)
        
        plan = json.loads(task.plan or "{}")
        self._say(task, ":hammer_and_wrench: Approved. Working on the change now...")
        
        work_dir = self._work_dir(tid)
        work_dir.mkdir(parents=True, exist_ok=True)
        
        try:
            # Clone and create branch
            clone_url = self.github_client.get_clone_url(task.repo_owner, task.repo_name)
            repo_dir = work_dir / "repo"
            GitOperations.clone_repository(clone_url, repo_dir)
            
            branch = f"agent/{tid}-{guards.slugify(plan.get('summary', 'change'))}-{secrets.token_hex(2)}"
            GitOperations.create_branch(repo_dir, branch)
            self.database.update(tid, branch=branch)
            
            # Prepare agent input
            io_dir = work_dir / "io"
            agent_input = {
                "request": task.task_description,
                "plan": plan,
                "mode": "implement",
                "model": self.config.agent.model,
                "implementation_budget": self.config.agent.implementation_budget,
                "max_turns": self.config.agent.max_turns,
                "checks": {
                    "lint": "ruff check ." if (repo_dir / "pyproject.toml").exists() else None,
                    "test": "pytest -v" if (repo_dir / "tests").exists() else None,
                },
                "check_timeout_s": self.config.agent.check_timeout_s,
            }
            
            write_input(io_dir, agent_input)
            
            # Run implementation
            result = self.sandbox.run_agent(
                mode="implement",
                repo_dir=repo_dir,
                io_dir=io_dir,
                image=self.config.docker.image,
                timeout_s=600,
                api_key=self.config.gemini.api_key,
            )
            
            output = read_output(io_dir, result)
            
            # Verify changes on host (never trust sandbox)
            self.database.update_task_status(tid, TaskStatus.VERIFYING)
            report = guards.inspect_worktree(repo_dir)
            
            if not report.files:
                note = guards.escape_slack(guards.tail(output.get("text", ""), 600))
                raise TaskAbort(f"Didn't change any files.\n{note}" if note else "Didn't change any files.")
            
            # Policy check
            problems = guards.check_policy(
                report,
                max_files=self.config.security.max_files,
                max_changed_lines=self.config.security.max_changed_lines,
                deny_patterns=self.config.security.protected_paths,
            ) + guards.scan_secrets(report.added_lines)
            
            if problems:
                raise TaskAbort(
                    ":no_entry: Stopped without opening PR because:\n"
                    + "\n".join(f"• {p}" for p in problems)
                    + "\nTry a smaller, more specific request."
                )
            
            # Save changes
            summary = output.get("text", "Changes made")[:2000]
            diff = guards.tail(GitOperations.get_diff(repo_dir, cached=False), 5000)
            
            self.database.update_task_implementation(tid, summary, diff)
            self.database.update_task_status(tid, TaskStatus.AWAITING_IMPL_APPROVAL)
            
            # Post to Slack
            diff_preview = diff[:1000] + "\n... (truncated)" if len(diff) > 1000 else diff
            self.slack_handler.post_implementation(tid, summary, diff_preview)
            
        finally:
            # Cleanup
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)
    
    # ------------------------------------------------------------------ PR CREATION
    
    def _process_pr_creation(self, task: Task) -> None:
        """Create pull request with changes."""
        tid = task.id
        self.database.update_task_status(tid, TaskStatus.CREATING_PR)
        
        plan = json.loads(task.plan or "{}")
        
        work_dir = self._work_dir(tid)
        work_dir.mkdir(parents=True, exist_ok=True)
        
        try:
            # Clone and recreate changes
            clone_url = self.github_client.get_clone_url(task.repo_owner, task.repo_name)
            repo_dir = work_dir / "repo"
            GitOperations.clone_repository(clone_url, repo_dir)
            
            branch = task.branch_name
            GitOperations.create_branch(repo_dir, branch)
            
            # Re-apply changes (re-run implementation)
            # In production, you'd cache the changes; for now, re-run
            io_dir = work_dir / "io"
            agent_input = {
                "request": task.task_description,
                "plan": plan,
                "mode": "implement",
                "model": self.config.agent.model,
                "implementation_budget": self.config.agent.implementation_budget,
                "max_turns": self.config.agent.max_turns,
            }
            
            write_input(io_dir, agent_input)
            
            result = self.sandbox.run_agent(
                mode="implement",
                repo_dir=repo_dir,
                io_dir=io_dir,
                image=self.config.docker.image,
                timeout_s=600,
                api_key=self.config.gemini.api_key,
            )
            
            read_output(io_dir, result)  # Validate
            
            # Commit and push
            if not GitOperations.has_changes(repo_dir):
                raise TaskAbort("No changes to commit")
            
            commit_msg = f"{plan.get('summary', task.task_description)}\n\nTask #{tid}"
            GitOperations.commit_changes(
                repo_dir,
                commit_msg,
                author_name=self.config.github.default_owner or "Bot",
                author_email="bot@example.com"
            )
            
            GitOperations.push_branch(repo_dir, branch)
            
            # Create PR
            pr_title = f"[bot] {plan.get('summary', task.task_description)[:70]}"
            pr_body = self._build_pr_body(task, plan)
            
            pr_url = self.github_client.create_pull_request(
                task.repo_owner,
                task.repo_name,
                branch,
                pr_title,
                pr_body,
            )
            
            self.database.update_task_pr(tid, pr_url)
            self.database.update_task_status(tid, TaskStatus.COMPLETED)
            
            self.slack_handler.post_pr_created(tid, pr_url)
            
        finally:
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)
    
    def _build_pr_body(self, task: Task, plan: Dict) -> str:
        """Build PR description."""
        lines = [
            "## Summary",
            guards.neutralize_github_mentions(plan.get("summary", "")),
            "",
            "## How to Verify",
            plan.get("how_to_verify", "See changes"),
            "",
            f"**Task:** {task.task_description}",
            f"**Risk Level:** {plan.get('risk', 'medium')}",
            f"**Bot Task ID:** #{task.id}",
        ]
        return "\n".join(lines)
    
    # ------------------------------------------------------------------ HELPERS
    
    def _work_dir(self, task_id: int) -> Path:
        """Get working directory for a task."""
        base = Path(self.config.database.path).parent / "work"
        return base / f"task-{task_id}"
    
    def _say(self, task: Task, text: str) -> None:
        """Post message to Slack thread."""
        try:
            self.slack_handler.app.client.chat_postMessage(
                channel=task.slack_channel_id,
                thread_ts=task.slack_thread_ts,
                text=text,
            )
        except Exception as e:
            logger.error(f"Failed to post to Slack: {e}")
    
    def _end_task(self, task: Task, message: str, status: str) -> None:
        """End task with message."""
        self.database.update_task_status(
            task.id,
            TaskStatus[status.upper()] if hasattr(TaskStatus, status.upper()) else TaskStatus.FAILED,
            error_message=message if status == "failed" else None
        )
        self._say(task, message)


def main() -> None:
    """Main entry point for worker process."""
    import sys
    from .config import load_config
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    
    try:
        config = load_config(config_path)
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        sys.exit(1)
    
    database = Database(config.database.path)
    
    from .slack_handler import SlackHandler
    slack_handler = SlackHandler(config, database)
    
    worker = Worker(config, database, slack_handler)
    stop = threading.Event()
    
    try:
        worker.run_forever(stop)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        stop.set()


if __name__ == "__main__":
    main()
