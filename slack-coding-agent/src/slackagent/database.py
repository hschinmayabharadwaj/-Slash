"""Database schema and task management."""

import enum
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional


class TaskStatus(enum.Enum):
    """Task lifecycle states."""

    PENDING = "pending"
    PLANNING = "planning"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    PLAN_APPROVED = "plan_approved"
    PLAN_REJECTED = "plan_rejected"
    IMPLEMENTING = "implementing"
    AWAITING_IMPL_APPROVAL = "awaiting_impl_approval"
    IMPL_APPROVED = "impl_approved"
    IMPL_REJECTED = "impl_rejected"
    CREATING_PR = "creating_pr"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    """Task record."""

    id: Optional[int]
    slack_user_id: str
    slack_channel_id: str
    slack_thread_ts: str
    repo_owner: str
    repo_name: str
    branch_name: str
    task_description: str
    status: TaskStatus
    plan: Optional[str]
    implementation_summary: Optional[str]
    diff: Optional[str]
    pr_url: Optional[str]
    error_message: Optional[str]
    created_at: datetime
    updated_at: datetime
    worker_id: Optional[str] = None


class Database:
    """SQLite database for task management."""

    def __init__(self, db_path: str):
        """Initialize database connection.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection."""
        conn = sqlite3.Connection(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        """Initialize database schema."""
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slack_user_id TEXT NOT NULL,
                    slack_channel_id TEXT NOT NULL,
                    slack_thread_ts TEXT NOT NULL,
                    repo_owner TEXT NOT NULL,
                    repo_name TEXT NOT NULL,
                    branch_name TEXT NOT NULL,
                    task_description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    plan TEXT,
                    implementation_summary TEXT,
                    diff TEXT,
                    pr_url TEXT,
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    worker_id TEXT
                )
                """
            )

            # Create indexes
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tasks_status 
                ON tasks(status)
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tasks_slack_thread 
                ON tasks(slack_channel_id, slack_thread_ts)
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tasks_worker 
                ON tasks(worker_id, status)
                """
            )

            conn.commit()

    def create_task(
        self,
        slack_user_id: str,
        slack_channel_id: str,
        slack_thread_ts: str,
        repo_owner: str,
        repo_name: str,
        branch_name: str,
        task_description: str,
    ) -> Task:
        """Create a new task.

        Args:
            slack_user_id: User who created the task
            slack_channel_id: Slack channel ID
            slack_thread_ts: Slack thread timestamp
            repo_owner: GitHub repository owner
            repo_name: GitHub repository name
            branch_name: Git branch name
            task_description: Task description

        Returns:
            Created task
        """
        now = datetime.utcnow()
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO tasks (
                    slack_user_id, slack_channel_id, slack_thread_ts,
                    repo_owner, repo_name, branch_name, task_description,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    slack_user_id,
                    slack_channel_id,
                    slack_thread_ts,
                    repo_owner,
                    repo_name,
                    branch_name,
                    task_description,
                    TaskStatus.PENDING.value,
                    now,
                    now,
                ),
            )
            conn.commit()
            task_id = cursor.lastrowid

        return Task(
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

    def get_task(self, task_id: int) -> Optional[Task]:
        """Get a task by ID.

        Args:
            task_id: Task ID

        Returns:
            Task or None if not found
        """
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

        if not row:
            return None

        return self._row_to_task(row)

    def update_task_status(
        self,
        task_id: int,
        status: TaskStatus,
        error_message: Optional[str] = None,
    ) -> None:
        """Update task status.

        Args:
            task_id: Task ID
            status: New status
            error_message: Optional error message
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE tasks 
                SET status = ?, error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (status.value, error_message, datetime.utcnow(), task_id),
            )
            conn.commit()

    def update_task_plan(self, task_id: int, plan: str) -> None:
        """Update task plan.

        Args:
            task_id: Task ID
            plan: Plan text
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE tasks 
                SET plan = ?, updated_at = ?
                WHERE id = ?
                """,
                (plan, datetime.utcnow(), task_id),
            )
            conn.commit()

    def update_task_implementation(
        self, task_id: int, implementation_summary: str, diff: str
    ) -> None:
        """Update task implementation results.

        Args:
            task_id: Task ID
            implementation_summary: Summary of changes
            diff: Git diff output
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE tasks 
                SET implementation_summary = ?, diff = ?, updated_at = ?
                WHERE id = ?
                """,
                (implementation_summary, diff, datetime.utcnow(), task_id),
            )
            conn.commit()

    def update_task_pr(self, task_id: int, pr_url: str) -> None:
        """Update task with PR URL.

        Args:
            task_id: Task ID
            pr_url: Pull request URL
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE tasks 
                SET pr_url = ?, updated_at = ?
                WHERE id = ?
                """,
                (pr_url, datetime.utcnow(), task_id),
            )
            conn.commit()

    def get_pending_tasks(self, limit: int = 10) -> List[Task]:
        """Get pending tasks for workers to process.

        Args:
            limit: Maximum number of tasks to return

        Returns:
            List of pending tasks
        """
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks 
                WHERE status IN (?, ?, ?)
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (
                    TaskStatus.PENDING.value,
                    TaskStatus.PLAN_APPROVED.value,
                    TaskStatus.IMPL_APPROVED.value,
                    limit,
                ),
            ).fetchall()

        return [self._row_to_task(row) for row in rows]

    def get_task_by_thread(
        self, channel_id: str, thread_ts: str
    ) -> Optional[Task]:
        """Get task by Slack thread.

        Args:
            channel_id: Slack channel ID
            thread_ts: Slack thread timestamp

        Returns:
            Task or None if not found
        """
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM tasks 
                WHERE slack_channel_id = ? AND slack_thread_ts = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (channel_id, thread_ts),
            ).fetchone()

        if not row:
            return None

        return self._row_to_task(row)

    def claim_task(self, task_id: int, worker_id: str) -> bool:
        """Claim a task for processing by a worker.

        Args:
            task_id: Task ID
            worker_id: Worker identifier

        Returns:
            True if task was claimed, False if already claimed
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks 
                SET worker_id = ?, updated_at = ?
                WHERE id = ? AND worker_id IS NULL
                """,
                (worker_id, datetime.utcnow(), task_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def release_task(self, task_id: int) -> None:
        """Release a task from a worker.

        Args:
            task_id: Task ID
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE tasks 
                SET worker_id = NULL, updated_at = ?
                WHERE id = ?
                """,
                (datetime.utcnow(), task_id),
            )
            conn.commit()

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        """Convert database row to Task object.

        Args:
            row: Database row

        Returns:
            Task object
        """
        return Task(
            id=row["id"],
            slack_user_id=row["slack_user_id"],
            slack_channel_id=row["slack_channel_id"],
            slack_thread_ts=row["slack_thread_ts"],
            repo_owner=row["repo_owner"],
            repo_name=row["repo_name"],
            branch_name=row["branch_name"],
            task_description=row["task_description"],
            status=TaskStatus(row["status"]),
            plan=row["plan"],
            implementation_summary=row["implementation_summary"],
            diff=row["diff"],
            pr_url=row["pr_url"],
            error_message=row["error_message"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            worker_id=row["worker_id"],
        )
