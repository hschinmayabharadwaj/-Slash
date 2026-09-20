"""AWS worker: SQS long-poll + DynamoDB tasks + Fargate sandbox + GitHub PRs.

This worker is the runtime for the demo deployment.  It drives each task
through phases, mirroring :class:`~slackagent.worker_v2.Worker` but without any
local config file, SQLite, or Slack SDK:

    READY             --(worker picks up)-->        RUNNING (plan)
    RUNNING (plan)    --(plan posted)-->            AWAITING_APPROVAL
    AWAITING_APPROVAL --(dashboard approve)-->      PLAN_APPROVED
    PLAN_APPROVED     --(worker picks up)-->        RUNNING (implement)
    RUNNING (impl)    --(impl posted)-->            AWAITING_IMPL_APPROVAL
    AWAITING_IMPL_APPROVAL --(approve)-->           IMPL_APPROVED
    IMPL_APPROVED     --(worker picks up)-->        RUNNING (PR) -> COMPLETED

Rejections/cancellations/kill-switch are handled by the dashboard API and the
task processor Lambda; this worker only reacts to the resulting statuses plus
control messages on the queue.

Statuses are strings stored in the shared ``TaskState`` table so the Step
Functions workflow, approval Lambda and console all read the same records.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import tempfile
import time
import traceback
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from . import guards
from .agent_io import read_output, write_input
from .dynamo_db import DynamoDBStore
from .fargate_sandbox import FargateSandbox
from .github_ops import GitHubClient, GitHubConfig, GitHubOperationError
from .sandbox import SandboxResult

logger = logging.getLogger(__name__)

QUEUE_URL_ENV = "SQS_QUEUE_URL"
KILL_SWITCH_PARAM_ENV = "KILL_SWITCH_PARAM"


class TaskAbort(Exception):
    """Expected failure with a user-visible message."""

    def __init__(self, message: str, status: str = "FAILED"):
        super().__init__(message)
        self.status = status


class WorkerAWS:
    """SQS-backed worker that runs one task per Fargate sandbox phase."""

    def __init__(
        self,
        *,
        store: DynamoDBStore = None,
        sandbox: FargateSandbox = None,
        github: GitHubClient = None,
        queue_url: str = None,
        worker_id: str = None,
        sqs=None,
        ssm=None,
    ):
        self.store = store or DynamoDBStore()
        self.sandbox = sandbox or FargateSandbox()
        self.github = github or _github_from_env()
        self.queue_url = queue_url or os.environ.get(QUEUE_URL_ENV, "")
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self._sqs = sqs
        self._ssm = ssm
        self.sandbox_image = os.environ.get("SANDBOX_IMAGE", "slack-coding-agent-sandbox:latest")
        self._kill_param = os.environ.get(KILL_SWITCH_PARAM_ENV, "/slack-agent/kill-switch")
        logger.info("WorkerAWS %s initialized (queue=%s)", self.worker_id, self.queue_url)

    # ------------------------------------------------------------------ clients

    def _sqs_client(self):
        if self._sqs is None:
            import boto3
            self._sqs = boto3.client("sqs", region_name=os.environ.get("AWS_REGION"))
        return self._sqs

    def _ssm_client(self):
        if self._ssm is None:
            import boto3
            self._ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION"))
        return self._ssm

    # ------------------------------------------------------------------ loop

    def run_forever(self) -> None:
        """Long-poll the queue and process one message at a time."""
        while True:
            if self.kill_switch_on():
                logger.warning("kill switch on — worker paused")
                time.sleep(15)
                continue
            try:
                self.process_one_batch()
            except Exception:
                logger.exception("worker loop error")
                time.sleep(5)

    def kill_switch_on(self) -> bool:
        try:
            value = self._ssm_client().get_parameter(Name=self._kill_param)["Parameter"]["Value"]
            return value == "1"
        except Exception:
            return False

    def process_one_batch(self, wait_time_s: int = 20) -> int:
        """Receive and process up to 10 messages. Returns count processed."""
        if not self.queue_url:
            return 0
        resp = self._sqs_client().receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=wait_time_s,
            VisibilityTimeout=90,
        )
        messages = resp.get("Messages") or []
        for msg in messages:
            receipt = msg["ReceiptHandle"]
            try:
                self.handle_message(msg["Body"])
            except Exception:
                logger.exception("message handling failed")
                self._delete(receipt)
        return len(messages)

    def handle_message(self, body: str) -> bool:
        """Process one queue message. Returns True if real work happened."""
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            logger.warning("dropping malformed message: %r", body[:200])
            return False
        task_id = payload.get("taskId")
        if not task_id:
            return False

        task = self.store.get_item_raw(task_id)
        if not task:
            logger.warning("task %s not found — dropping message", task_id)
            return False
        if task.get("status") not in ("READY", "PLAN_APPROVED", "IMPL_APPROVED"):
            logger.info("task %s status=%s — no phase, skipping", task_id, task.get("status"))
            return False

        if not self.claim(task_id):
            logger.info("task %s already claimed — skipping", task_id)
            return False
        try:
            self.drive(task_id)
        finally:
            self.release(task_id)
        return True

    # ------------------------------------------------------------------ phases

    def drive(self, task_id: str) -> None:
        """Advance the task one phase according to its current status."""
        task = self.store.get_item_raw(task_id) or {}
        status = task.get("status")

        try:
            if status == "READY":
                self.plan_phase(task)
            elif status == "PLAN_APPROVED":
                self.implement_phase(task)
            elif status == "IMPL_APPROVED":
                self.pr_phase(task)
            else:
                logger.info("task %s has nothing to do in status %s", task_id, status)
        except TaskAbort as abort:
            self.store.set_status_raw(task_id, abort.status)
            self.store.record_event(task_id, abort.status.lower(), {"error": str(abort)})
            logger.warning("task %s aborted (%s): %s", task_id, abort.status, abort)
        except Exception:
            logger.exception("task %s failed in phase %s", task_id, status)
            self.store.set_status_raw(task_id, "FAILED")
            self.store.record_event(task_id, "failed", {"error": "internal worker error"})

    def plan_phase(self, task: dict) -> None:
        task_id = task["taskId"]
        self.set_status(task_id, "RUNNING")
        with self._workdir(task_id) as work_dir:
            repo_dir = work_dir / "repo"
            self.clone(task, repo_dir, base=task.get("baseBranch") or "main")

            io_dir = work_dir / "io"
            write_input(io_dir, {
                "request": task["request"],
                "mode": "plan",
                "model": os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
                "max_turns": int(os.environ.get("MAX_TURNS", "25")),
            })

            result = self.sandbox.run_agent(
                mode="plan",
                repo_dir=repo_dir,
                io_dir=io_dir,
                image=self.sandbox_image,
                timeout_s=int(os.environ.get("PLAN_TIMEOUT_S", "300")),
                api_key="",
            )
            output = read_output(io_dir, result)
            plan = self.normalize_plan(output.get("plan"))

            if not plan["feasible"]:
                raise TaskAbort(
                    "I can't start on this request — the plan came back infeasible. "
                    f"Details: {guards.tail(plan.get('summary', ''), 500)}",
                    "DECLINED",
                )
            protected = [f for f in plan["files"] if guards.matches_any(f, os.environ.get("PROTECTED_PATHS", "").split(","))]
            if plan["risk"] == "high" or protected:
                raise TaskAbort(
                    "I won't make this change myself (touches protected files or high risk). "
                    "Please ask an engineer.",
                    "DECLINED",
                )

            self.store.set_task_plan(task_id, plan)
            self.set_status(task_id, "AWAITING_APPROVAL")
            self.store.record_event(task_id, "plan_ready", {"summary": plan["summary"]})
            logger.info("task %s plan ready (risk=%s)", task_id, plan["risk"])

    def implement_phase(self, task: dict) -> None:
        task_id = task["taskId"]
        self.set_status(task_id, "RUNNING")
        plan = json.loads(task.get("plan") or "{}")

        with self._workdir(task_id) as work_dir:
            repo_dir = work_dir / "repo"
            self.clone(task, repo_dir, base=task.get("baseBranch") or "main")
            branch = task.get("branch") or f"agent/{task_id}-{guards.slugify(plan.get('summary', 'change'))[:30]}-{secrets.token_hex(2)}"
            if task.get("branch") is None:
                from .github_ops import GitOperations
                GitOperations.create_branch(repo_dir, branch)
                self.set_branch(task_id, branch)

            io_dir = work_dir / "io"
            write_input(io_dir, {
                "request": task["request"],
                "plan": plan,
                "mode": "implement",
                "model": os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
                "max_turns": int(os.environ.get("MAX_TURNS", "25")),
                "checks": {
                    "lint": "ruff check ." if (repo_dir / "pyproject.toml").exists() else None,
                    "test": "pytest -v" if (repo_dir / "tests").exists() else None,
                },
                "check_timeout_s": 180,
            })

            result = self.sandbox.run_agent(
                mode="implement",
                repo_dir=repo_dir,
                io_dir=io_dir,
                image=self.sandbox_image,
                timeout_s=int(os.environ.get("IMPL_TIMEOUT_S", "600")),
                api_key="",
            )
            output = read_output(io_dir, result)

            sandbox_diff = output.get("diff") or ""
            if sandbox_diff.strip():
                try:
                    _apply_patch(repo_dir, sandbox_diff)
                except Exception as exc:
                    logger.warning("could not apply sandbox diff on worker clone: %s", exc)

            report = guards.inspect_worktree(repo_dir)
            if not report.files:
                raise TaskAbort(
                    "The agent didn't change any files. Try rephrasing the request.",
                    "FAILED",
                )
            problems = guards.check_policy(
                report,
                max_files=int(os.environ.get("MAX_FILES", "25")),
                max_changed_lines=int(os.environ.get("MAX_CHANGED_LINES", "1000")),
                deny_patterns=os.environ.get("PROTECTED_PATHS", "").split(","),
            ) + guards.scan_secrets(report.added_lines)
            if problems:
                raise TaskAbort(
                    "Changes were stopped by policy:\n" + "\n".join(f"• {p}" for p in problems),
                    "DECLINED",
                )

            diff = guards.tail(_git_diff(repo_dir), 5000)
            self.store.set_task_implementation(task_id, output.get("text", "Changes made")[:2000], diff)
            self.set_status(task_id, "AWAITING_IMPL_APPROVAL")
            self.store.record_event(task_id, "impl_ready", {"summary": output.get("text", "")[:300]})
            logger.info("task %s implementation ready (%s files)", task_id, len(report.files))

    def pr_phase(self, task: dict) -> None:
        task_id = task["taskId"]
        self.set_status(task_id, "RUNNING")
        plan = json.loads(task.get("plan") or "{}")
        branch = task.get("branch") or f"agent/{task_id}-{guards.slugify(plan.get('summary', 'change'))[:30]}"

        with self._workdir(task_id) as work_dir:
            repo_dir = work_dir / "repo"
            self.clone(task, repo_dir, base=task.get("baseBranch") or "main")
            from .github_ops import GitOperations
            GitOperations.create_branch(repo_dir, branch)

            diff = task.get("diff") or ""
            if not diff.strip():
                raise TaskAbort("No stored diff to open a PR with.", "FAILED")
            _apply_patch(repo_dir, diff)

            if not GitOperations.has_changes(repo_dir):
                raise TaskAbort("No changes to commit.", "FAILED")
            GitOperations.commit_changes(
                repo_dir,
                f"{plan.get('summary', task['request'])}\n\nTask {task_id}",
                author_name="slack-coding-agent",
                author_email="bot@example.com",
            )
            GitOperations.push_branch(repo_dir, branch)

            repo_owner, repo_name = (task.get("repo") or "").split("/", 1) or ["", ""]
            pr_title = f"[bot] {plan.get('summary', task['request'])[:70]}"
            pr_body = self._build_pr_body(task, plan)
            pr_url = self.github.create_pull_request(repo_owner, repo_name, branch, pr_title, pr_body)

            self.store.set_task_pr(task_id, pr_url)
            self.set_status(task_id, "COMPLETED")
            self.store.record_event(task_id, "pr_created", {"url": pr_url})
            logger.info("task %s completed: %s", task_id, pr_url)

    def _build_pr_body(self, task: dict, plan: dict) -> str:
        lines = [
            "## Summary",
            guards.neutralize_github_mentions(plan.get("summary", "")),
            "",
            "## How to Verify",
            plan.get("how_to_verify", "See changes"),
            "",
            f"**Task:** {task['request']}",
            f"**Risk Level:** {plan.get('risk', 'medium')}",
            f"**Bot Task ID:** {task['taskId']}",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------ helpers

    def normalize_plan(self, raw: Any) -> dict:
        if not isinstance(raw, dict):
            raise TaskAbort("Couldn't produce a clear plan. Try rephrasing.", "FAILED")
        if "feasible" not in raw:
            raise TaskAbort("Plan missing required field 'feasible'.", "FAILED")
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

    def claim(self, task_id: str) -> bool:
        return self.store.claim_task(task_id, self.worker_id)

    def release(self, task_id: str) -> None:
        try:
            self.store.release_task(task_id)
        except Exception:
            logger.warning("release_task failed for %s", task_id, exc_info=True)

    def set_status(self, task_id: str, status: str) -> None:
        self.store.set_status_raw(task_id, status)

    def set_branch(self, task_id: str, branch: str) -> None:
        self.store.set_branch_raw(task_id, branch)

    def clone(self, task: dict, dest: Path, base: str = "main") -> None:
        repo_owner, repo_name = (task.get("repo") or "").split("/", 1) or ["", ""]
        if not repo_owner or not repo_name:
            raise TaskAbort("No repository (owner/name) configured on this task.", "FAILED")
        from .github_ops import GitOperations
        clone_url = self.github.get_clone_url(repo_owner, repo_name)
        GitOperations.clone_repository(clone_url, dest, branch=base)

    def _workdir(self, task_id: str):
        @contextmanager
        def _tmp():
            tmp = Path(tempfile.mkdtemp(prefix=f"work-{task_id}-"))
            try:
                yield tmp
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        return _tmp()

    def _delete(self, receipt: str) -> None:
        try:
            self._sqs_client().delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt)
        except Exception:
            logger.warning("delete_message failed", exc_info=True)


def _github_from_env() -> GitHubClient:
    private_key = os.environ.get("GITHUB_PRIVATE_KEY", "")
    key_path = None
    if private_key:
        tmp = Path(tempfile.gettempdir()) / f"github-{uuid.uuid4().hex}.pem"
        tmp.write_text(private_key)
        os.chmod(tmp, 0o600)
        key_path = str(tmp)
    config = GitHubConfig(
        app_id=os.environ.get("GITHUB_APP_ID", ""),
        private_key_path=key_path or "",
        installation_id=os.environ.get("GITHUB_INSTALLATION_ID", ""),
        default_owner=os.environ.get("GITHUB_OWNER", ""),
    )
    return GitHubClient(config)


def _git_diff(repo_dir: Path) -> str:
    from .github_ops import GitOperations
    try:
        return GitOperations.get_diff(repo_dir, cached=False)
    except GitHubOperationError:
        return ""


def _apply_patch(repo_dir: Path, diff_patch: str) -> None:
    import subprocess
    subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-"],
        cwd=repo_dir,
        input=diff_patch,
        check=True,
        capture_output=True,
        text=True,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    worker = WorkerAWS()
    logger.info("worker %s starting", worker.worker_id)
    while True:
        try:
            worker.run_forever()
        except KeyboardInterrupt:
            logger.info("shutting down")
            break
        except Exception:
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    main()