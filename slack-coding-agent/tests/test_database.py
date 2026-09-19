"""Tests for database operations."""

import tempfile
from pathlib import Path

import pytest

from slackagent.database import Database, TaskStatus


@pytest.fixture
def database() -> Database:
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    db = Database(db_path)
    yield db

    # Clean up
    Path(db_path).unlink(missing_ok=True)


def test_create_task(database: Database) -> None:
    """Test creating a task."""
    task = database.create_task(
        slack_user_id="U12345",
        slack_channel_id="C12345",
        slack_thread_ts="1234567890.123456",
        repo_owner="testowner",
        repo_name="testrepo",
        branch_name="test-branch",
        task_description="Test task",
    )

    assert task.id is not None
    assert task.slack_user_id == "U12345"
    assert task.repo_owner == "testowner"
    assert task.repo_name == "testrepo"
    assert task.status == TaskStatus.PENDING


def test_get_task(database: Database) -> None:
    """Test retrieving a task by ID."""
    task = database.create_task(
        slack_user_id="U12345",
        slack_channel_id="C12345",
        slack_thread_ts="1234567890.123456",
        repo_owner="testowner",
        repo_name="testrepo",
        branch_name="test-branch",
        task_description="Test task",
    )

    retrieved = database.get_task(task.id)

    assert retrieved is not None
    assert retrieved.id == task.id
    assert retrieved.task_description == "Test task"


def test_get_nonexistent_task(database: Database) -> None:
    """Test retrieving a non-existent task."""
    task = database.get_task(99999)
    assert task is None


def test_update_task_status(database: Database) -> None:
    """Test updating task status."""
    task = database.create_task(
        slack_user_id="U12345",
        slack_channel_id="C12345",
        slack_thread_ts="1234567890.123456",
        repo_owner="testowner",
        repo_name="testrepo",
        branch_name="test-branch",
        task_description="Test task",
    )

    database.update_task_status(task.id, TaskStatus.PLANNING)

    updated = database.get_task(task.id)
    assert updated.status == TaskStatus.PLANNING


def test_update_task_plan(database: Database) -> None:
    """Test updating task plan."""
    task = database.create_task(
        slack_user_id="U12345",
        slack_channel_id="C12345",
        slack_thread_ts="1234567890.123456",
        repo_owner="testowner",
        repo_name="testrepo",
        branch_name="test-branch",
        task_description="Test task",
    )

    plan = "1. Do this\n2. Do that\n3. Test"
    database.update_task_plan(task.id, plan)

    updated = database.get_task(task.id)
    assert updated.plan == plan


def test_update_task_implementation(database: Database) -> None:
    """Test updating task implementation."""
    task = database.create_task(
        slack_user_id="U12345",
        slack_channel_id="C12345",
        slack_thread_ts="1234567890.123456",
        repo_owner="testowner",
        repo_name="testrepo",
        branch_name="test-branch",
        task_description="Test task",
    )

    summary = "Made changes to files"
    diff = "diff --git a/file.py..."
    database.update_task_implementation(task.id, summary, diff)

    updated = database.get_task(task.id)
    assert updated.implementation_summary == summary
    assert updated.diff == diff


def test_get_pending_tasks(database: Database) -> None:
    """Test retrieving pending tasks."""
    # Create tasks with different statuses
    task1 = database.create_task(
        slack_user_id="U12345",
        slack_channel_id="C12345",
        slack_thread_ts="1234567890.123456",
        repo_owner="testowner",
        repo_name="testrepo",
        branch_name="test-branch-1",
        task_description="Task 1",
    )

    task2 = database.create_task(
        slack_user_id="U12345",
        slack_channel_id="C12345",
        slack_thread_ts="1234567890.123457",
        repo_owner="testowner",
        repo_name="testrepo",
        branch_name="test-branch-2",
        task_description="Task 2",
    )

    database.update_task_status(task2.id, TaskStatus.COMPLETED)

    # Get pending tasks
    pending = database.get_pending_tasks()

    assert len(pending) == 1
    assert pending[0].id == task1.id


def test_claim_and_release_task(database: Database) -> None:
    """Test claiming and releasing tasks."""
    task = database.create_task(
        slack_user_id="U12345",
        slack_channel_id="C12345",
        slack_thread_ts="1234567890.123456",
        repo_owner="testowner",
        repo_name="testrepo",
        branch_name="test-branch",
        task_description="Test task",
    )

    # Claim task
    claimed = database.claim_task(task.id, "worker-1")
    assert claimed is True

    updated = database.get_task(task.id)
    assert updated.worker_id == "worker-1"

    # Try to claim again (should fail)
    claimed_again = database.claim_task(task.id, "worker-2")
    assert claimed_again is False

    # Release task
    database.release_task(task.id)

    released = database.get_task(task.id)
    assert released.worker_id is None

    # Now worker-2 can claim
    claimed_by_2 = database.claim_task(task.id, "worker-2")
    assert claimed_by_2 is True


def test_get_task_by_thread(database: Database) -> None:
    """Test retrieving task by Slack thread."""
    task = database.create_task(
        slack_user_id="U12345",
        slack_channel_id="C12345",
        slack_thread_ts="1234567890.123456",
        repo_owner="testowner",
        repo_name="testrepo",
        branch_name="test-branch",
        task_description="Test task",
    )

    retrieved = database.get_task_by_thread("C12345", "1234567890.123456")

    assert retrieved is not None
    assert retrieved.id == task.id
