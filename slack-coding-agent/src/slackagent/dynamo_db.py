"""DynamoDB-backed task store mirroring the ``Database`` public interface.

The items live in the shared ``TaskState`` table (taskId HASH + timestamp SORT,
timestamp is always ``now``) so the Fargate worker, the Step Functions workflow,
the approval Lambda and the dashboard API all read the same records::

    TaskState table assumes:
        HASH  taskId     (string)
        SORT  timestamp  (string, "now")
        + GSI StatusIndex      (HASH status,  RANGE created_at)
        + GSI ClientTokenIndex (HASH client_token)

Audit events (kill switch, approvals, cancellations) are appended to a separate
``slackagent-audit-events`` table (HASH ``pk``).  Boto3 is imported lazily so
unit tests can inject a fake ``Table``.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, List, Optional

from .database import Task, TaskStatus

_TASKS_TABLE_DEFAULT = "TaskState"
_AUDIT_TABLE_DEFAULT = "slackagent-audit-events"


def _is_conditional_failure(exc: Exception) -> bool:
    """True when ``exc`` is a DynamoDB ConditionalCheckFailedException."""
    if exc.__class__.__name__ == "ConditionalCheckFailedException":
        return True
    response = getattr(exc, "response", None) or {}
    error = response.get("Error", {}) or {}
    return error.get("Code") == "ConditionalCheckFailedException"


class DynamoDBStore:
    """DynamoDB task store.

    Args:
        table_name: tasks table name (default env ``SLACKAGENT_TASKS_TABLE``)
        audit_table_name: audit-events table (default env ``SLACKAGENT_AUDIT_TABLE``)
        table: injectable boto3 resource ``Table`` (for tests)
        audit_table: injectable boto3 resource ``Table`` (for tests)
    """

    def __init__(
        self,
        table_name: Optional[str] = None,
        audit_table_name: Optional[str] = None,
        table: Any = None,
        audit_table: Any = None,
    ):
        self.table_name = table_name or os.environ.get(
            "SLACKAGENT_TASKS_TABLE", _TASKS_TABLE_DEFAULT
        )
        self.audit_table_name = audit_table_name or os.environ.get(
            "SLACKAGENT_AUDIT_TABLE", _AUDIT_TABLE_DEFAULT
        )
        self._table_obj = table
        self._audit_table_obj = audit_table

    # ------------------------------------------------------------- clients

    def _table(self) -> Any:
        if self._table_obj is None:
            import boto3

            self._table_obj = boto3.resource("dynamodb").Table(self.table_name)
        return self._table_obj

    def _audit_table(self) -> Any:
        if self._audit_table_obj is None:
            import boto3

            self._audit_table_obj = boto3.resource("dynamodb").Table(
                self.audit_table_name
            )
        return self._audit_table_obj

    # ------------------------------------------------------------- mapping

    def _item_to_task(self, item: dict) -> Task:
        return Task(
            id=int(item["taskId"]),
            slack_user_id=item["slack_user_id"],
            slack_channel_id=item["slack_channel_id"],
            slack_thread_ts=item["slack_thread_ts"],
            repo_owner=item["repo_owner"],
            repo_name=item["repo_name"],
            branch_name=item["branch_name"],
            task_description=item["task_description"],
            status=TaskStatus(item["status"]),
            plan=item.get("plan"),
            implementation_summary=item.get("implementation_summary"),
            diff=item.get("diff"),
            pr_url=item.get("pr_url"),
            error_message=item.get("error_message"),
            created_at=datetime.fromisoformat(item["created_at"]),
            updated_at=datetime.fromisoformat(item["updated_at"]),
            worker_id=item.get("worker_id"),
        )

    def _task_to_item(self, task: Task, client_token: Optional[str] = None) -> dict:
        base = {
            "timestamp": "now",
            "status": task.status.value,
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
        }
        item = {
            "taskId": str(task.id),
            **base,
            "slack_user_id": task.slack_user_id,
            "slack_channel_id": task.slack_channel_id,
            "slack_thread_ts": task.slack_thread_ts,
            "repo_owner": task.repo_owner,
            "repo_name": task.repo_name,
            "branch_name": task.branch_name,
            "task_description": task.task_description,
        }
        if task.plan is not None:
            item["plan"] = task.plan
        if task.implementation_summary is not None:
            item["implementation_summary"] = task.implementation_summary
        if task.diff is not None:
            item["diff"] = task.diff
        if task.pr_url is not None:
            item["pr_url"] = task.pr_url
        if task.error_message is not None:
            item["error_message"] = task.error_message
        if task.worker_id is not None:
            item["worker_id"] = task.worker_id
        if client_token:
            item["client_token"] = client_token
        return item

    def _now(self) -> str:
        return datetime.utcnow().isoformat()

    # ------------------------------------------------------------- tasks

    def create_task(
        self,
        slack_user_id: str,
        slack_channel_id: str,
        slack_thread_ts: str,
        repo_owner: str,
        repo_name: str,
        branch_name: str,
        task_description: str,
        client_token: Optional[str] = None,
    ) -> Task:
        """Create a new task.

        Idempotent on ``client_token``: a retry with a previously used token
        returns the existing task instead of duplicating it.
        """
        if client_token:
            existing = self._find_by_client_token(client_token)
            if existing is not None:
                return existing

        now = datetime.utcnow()
        task_id = self._next_id()
        task = Task(
            id=task_id,
            slack_user_id=slack_user_id,
            slack_channel_id=slack_channel_id,
            slack_thread_ts=slack_thread_ts,
            repo_owner=repo_owner,
            repo_name=repo_name,
            branch_name=branch_name,
            task_description=task_description,
            status=TaskStatus.PENDING,
            plan=None,
            implementation_summary=None,
            diff=None,
            pr_url=None,
            error_message=None,
            created_at=now,
            updated_at=now,
        )
        self._table().put_item(
            Item=self._task_to_item(task, client_token=client_token),
            ConditionExpression="attribute_not_exists(taskId)",
        )
        return task

    def _next_id(self) -> int:
        """Atomic monotonic id via a ``__counter__`` item."""
        response = self._table().update_item(
            Key={"taskId": "__counter__", "timestamp": "__counter__"},
            UpdateExpression="ADD n :inc",
            ExpressionAttributeValues={":inc": 1},
            ReturnValues="UPDATED_NEW",
        )
        return int(response["Attributes"]["n"])

    def _find_by_client_token(self, client_token: str) -> Optional[Task]:
        response = self._table().query(
            IndexName="ClientTokenIndex",
            KeyConditionExpression="client_token = :tok",
            ExpressionAttributeValues={":tok": client_token},
            Limit=1,
        )
        items = response.get("Items") or []
        if not items:
            return None
        first = items[0]
        key = {k: first[k] for k in ("taskId", "timestamp") if k in first}
        return self.get_task(int(key.get("taskId", first.get("taskId"))))

    def get_task(self, task_id: int) -> Optional[Task]:
        response = self._table().get_item(
            Key={"taskId": str(task_id), "timestamp": "now"}
        )
        item = response.get("Item")
        if not item:
            return None
        return self._item_to_task(item)

    def update_task_status(
        self, task_id: int, status: TaskStatus, error_message: Optional[str] = None
    ) -> None:
        self._table().update_item(
            Key={"taskId": str(task_id), "timestamp": "now"},
            UpdateExpression="SET #s = :s, #u = :u, error_message = :em",
            ExpressionAttributeNames={"#s": "status", "#u": "updated_at"},
            ExpressionAttributeValues={
                ":s": status.value,
                ":u": self._now(),
                ":em": error_message,
            },
            ConditionExpression="attribute_exists(taskId)",
        )

    def update_task_plan(self, task_id: int, plan: str) -> None:
        self._table().update_item(
            Key={"taskId": str(task_id), "timestamp": "now"},
            UpdateExpression="SET plan = :p, #u = :u",
            ExpressionAttributeNames={"#u": "updated_at"},
            ExpressionAttributeValues={":p": plan, ":u": self._now()},
            ConditionExpression="attribute_exists(taskId)",
        )

    def update_task_implementation(
        self, task_id: int, implementation_summary: str, diff: str
    ) -> None:
        self._table().update_item(
            Key={"taskId": str(task_id), "timestamp": "now"},
            UpdateExpression="SET implementation_summary = :s, diff = :d, #u = :u",
            ExpressionAttributeNames={"#u": "updated_at"},
            ExpressionAttributeValues={
                ":s": implementation_summary,
                ":d": diff,
                ":u": self._now(),
            },
            ConditionExpression="attribute_exists(taskId)",
        )

    def update_task_pr(self, task_id: int, pr_url: str) -> None:
        self._table().update_item(
            Key={"taskId": str(task_id), "timestamp": "now"},
            UpdateExpression="SET pr_url = :p, #u = :u",
            ExpressionAttributeNames={"#u": "updated_at"},
            ExpressionAttributeValues={":p": pr_url, ":u": self._now()},
            ConditionExpression="attribute_exists(taskId)",
        )

    def get_pending_tasks(self, limit: int = 10) -> List[Task]:
        """Gather tasks ready for workers (pending, plan_approved, impl_approved)."""
        results: List[Task] = []
        for status in (
            TaskStatus.PENDING,
            TaskStatus.PLAN_APPROVED,
            TaskStatus.IMPL_APPROVED,
        ):
            response = self._table().query(
                IndexName="StatusIndex",
                KeyConditionExpression="#s = :s",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": status.value},
            )
            for item in response.get("Items", []):
                results.append(self._item_to_task(item))
        results.sort(key=lambda t: t.created_at)
        return results[:limit]

    def get_task_by_thread(
        self, channel_id: str, thread_ts: str
    ) -> Optional[Task]:
        response = self._table().scan(
            FilterExpression="slack_channel_id = :c AND slack_thread_ts = :t",
            ExpressionAttributeValues={":c": channel_id, ":t": thread_ts},
            Limit=1,
            Select="ALL_ATTRIBUTES",
        )
        items = response.get("Items", [])
        if not items:
            return None
        return self._item_to_task(items[0])

    def claim_task(self, task_id: int, worker_id: str) -> bool:
        """Claim a task for a worker (fails if already claimed)."""
        try:
            self._table().update_item(
                Key={"taskId": str(task_id), "timestamp": "now"},
                UpdateExpression="SET worker_id = :w, #u = :u",
                ExpressionAttributeNames={"#u": "updated_at"},
                ExpressionAttributeValues={":w": worker_id, ":u": self._now()},
                ConditionExpression="attribute_not_exists(worker_id) AND attribute_exists(taskId)",
            )
            return True
        except Exception as exc:
            if _is_conditional_failure(exc):
                return False
            raise

    def release_task(self, task_id: int) -> None:
        self._table().update_item(
            Key={"taskId": str(task_id), "timestamp": "now"},
            UpdateExpression="REMOVE worker_id",
            ConditionExpression="attribute_exists(worker_id)",
        )

    # ------------------------------------------------------------- raw access
    # The demo runtime (Step Functions, approval Lambda, console, worker) shares
    # the TaskState table with string taskIds (e.g. "task-abc123") and string
    # statuses (READY, RUNNING, AWAITING_APPROVAL, ...). These helpers operate
    # on the raw item shape; the Task-mapped API above is kept for the local
    # (SQLite/kinD) runtime and its tests.

    def get_item_raw(self, task_id: str) -> Optional[dict]:
        response = self._table().get_item(Key={"taskId": str(task_id), "timestamp": "now"})
        return response.get("Item")

    def set_status_raw(self, task_id: str, status: str) -> None:
        self._table().update_item(
            Key={"taskId": str(task_id), "timestamp": "now"},
            UpdateExpression="SET #s = :s, updatedAt = :u",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": status, ":u": self._now()},
            ConditionExpression="attribute_exists(taskId)",
        )

    def set_branch_raw(self, task_id: str, branch: str) -> None:
        self._table().update_item(
            Key={"taskId": str(task_id), "timestamp": "now"},
            UpdateExpression="SET branch = :b, updatedAt = :u",
            ExpressionAttributeValues={":b": branch, ":u": self._now()},
            ConditionExpression="attribute_exists(taskId)",
        )

    def set_task_plan(self, task_id: str, plan: dict) -> None:
        self._table().update_item(
            Key={"taskId": str(task_id), "timestamp": "now"},
            UpdateExpression="SET plan = :p, updatedAt = :u",
            ExpressionAttributeValues={":p": json.dumps(plan, sort_keys=True), ":u": self._now()},
            ConditionExpression="attribute_exists(taskId)",
        )

    def set_task_implementation(self, task_id: str, summary: str, diff: str) -> None:
        self._table().update_item(
            Key={"taskId": str(task_id), "timestamp": "now"},
            UpdateExpression="SET implementation_summary = :s, diff = :d, updatedAt = :u",
            ExpressionAttributeValues={":s": summary, ":d": diff, ":u": self._now()},
            ConditionExpression="attribute_exists(taskId)",
        )

    def set_task_pr(self, task_id: str, pr_url: str) -> None:
        self._table().update_item(
            Key={"taskId": str(task_id), "timestamp": "now"},
            UpdateExpression="SET pr_url = :p, updatedAt = :u",
            ExpressionAttributeValues={":p": pr_url, ":u": self._now()},
            ConditionExpression="attribute_exists(taskId)",
        )

    # ------------------------------------------------------------- audit

    def record_event(
        self, task_id: int, event_type: str, detail: dict, actor: Optional[str] = None
    ) -> None:
        """Append an immutable audit event (e.g. kill-switch, approvals)."""
        import json
        import time

        event = {
            "pk": f"audit#{task_id}",
            "event_id": f"{task_id}-{time.time_ns()}",
            "event_type": event_type,
            "detail": json.dumps(detail, sort_keys=True),
            "actor": actor,
            "created_at": self._now(),
        }
        self._audit_table().put_item(Item=event)

    def list_events(self, task_id: int, limit: int = 50) -> List[dict]:
        """Return recent audit events for a task (newest first)."""
        import json

        response = self._audit_table().query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": f"audit#{task_id}"},
            ScanIndexForward=False,
            Limit=limit,
        )
        events = []
        for item in response.get("Items", []):
            item = dict(item)
            try:
                item["detail"] = json.loads(item.get("detail", "{}"))
            except ValueError:
                pass
            events.append(item)
        return events