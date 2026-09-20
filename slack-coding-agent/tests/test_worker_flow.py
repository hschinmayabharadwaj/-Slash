"""Tests for worker flow and integration."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from slackagent.config import Config
from slackagent.database import Database, TaskStatus
from slackagent.worker_v2 import Worker, TaskAbort


class TestWorkerFlow:
    """Tests for worker task processing flow."""
    
    def test_worker_initialization(self, test_config, fake_slack, fake_github, fake_sandbox):
        """Test worker initialization."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/test.db"
            database = Database(db_path)
            
            # Create worker with fakes
            worker = Worker(
                test_config,
                database,
                fake_slack,
                worker_id="test-worker"
            )
            
            assert worker.worker_id == "test-worker"
            assert worker.config == test_config
    
    def test_plan_normalization(self, test_config, fake_slack, fake_github, fake_sandbox):
        """Test plan normalization."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/test.db"
            database = Database(db_path)
            
            worker = Worker(test_config, database, fake_slack)
            
            # Test valid plan
            raw_plan = {
                "feasible": True,
                "summary": "Add feature X",
                "files": ["main.py"],
                "risk": "low",
                "risk_reason": "simple change",
                "questions": [],
                "how_to_verify": "Run tests"
            }
            
            normalized = worker._normalize_plan(raw_plan)
            
            assert normalized["feasible"] is True
            assert normalized["summary"] == "Add feature X"
            assert normalized["risk"] == "low"
            assert len(normalized["files"]) == 1
    
    def test_plan_invalid_risk(self, test_config, fake_slack, fake_github, fake_sandbox):
        """Test plan normalization with invalid risk."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/test.db"
            database = Database(db_path)
            
            worker = Worker(test_config, database, fake_slack)
            
            raw_plan = {
                "feasible": True,
                "summary": "Test",
                "files": [],
                "risk": "very-high",  # Invalid
                "risk_reason": "something",
                "questions": [],
                "how_to_verify": ""
            }
            
            normalized = worker._normalize_plan(raw_plan)
            
            # Should default to "medium" for invalid risk
            assert normalized["risk"] == "medium"
    
    def test_plan_missing_required_fields(self, test_config, fake_slack, fake_github, fake_sandbox):
        """Test plan with missing required fields."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/test.db"
            database = Database(db_path)
            
            worker = Worker(test_config, database, fake_slack)
            
            # Should raise for invalid plan
            with pytest.raises(TaskAbort):
                worker._normalize_plan("not a dict")
            
            with pytest.raises(TaskAbort):
                worker._normalize_plan({"summary": "only summary"})  # Missing feasible
    
    def test_work_dir_creation(self, test_config, fake_slack, fake_github, fake_sandbox):
        """Test work directory creation."""
        with tempfile.TemporaryDirectory() as tmp:
            test_config.database.path = f"{tmp}/test.db"
            database = Database(f"{tmp}/test.db")
            
            worker = Worker(test_config, database, fake_slack)
            
            work_dir = worker._work_dir(123)
            
            # Should create parent directories
            work_dir.mkdir(parents=True, exist_ok=True)
            assert work_dir.exists()
            assert "task-123" in str(work_dir)


class TestPerRepoConfig:
    """Tests for per-repository configuration."""
    
    def test_default_repo_config(self, test_config):
        """Test default repo config when not specified."""
        repo_cfg = test_config.repo("unknown/repo")
        
        assert repo_cfg.lint_command is None
        assert repo_cfg.test_command is None
        assert repo_cfg.default_branch == "main"
        assert repo_cfg.enabled is True
    
    def test_specific_repo_config(self, test_config):
        """Test config for specific repo."""
        repo_cfg = test_config.repo("testorg/testrepo")
        
        assert repo_cfg.lint_command == "ruff check ."
        assert repo_cfg.test_command == "pytest -v"
        assert "src/admin/**" in repo_cfg.extra_deny_paths
    
    def test_deny_patterns(self, test_config):
        """Test combined deny patterns."""
        patterns = test_config.deny_for("testorg/testrepo")
        
        # Should include global + repo-specific
        assert ".git/**" in patterns  # Global
        assert ".env*" in patterns  # Global
        assert "src/admin/**" in patterns  # Repo-specific
    
    def test_repo_enabled(self, test_config):
        """Test repo enabled/disabled."""
        assert test_config.repo("testorg/testrepo").enabled is True
        
        # Add a disabled repo
        test_config.repos["disabled/repo"] = type('obj', (object,), {
            'lint_command': None,
            'test_command': None,
            'extra_deny_paths': [],
            'default_branch': 'main',
            'enabled': False,
            'max_files_override': None,
            'max_lines_override': None,
        })()
        
        assert not test_config.repo("disabled/repo").enabled


class TestErrorHandling:
    """Tests for error handling in worker."""
    
    def test_task_abort(self):
        """Test TaskAbort exception."""
        err = TaskAbort("Something went wrong", status="needs_info")
        
        assert str(err) == "Something went wrong"
        assert err.status == "needs_info"
        
        err2 = TaskAbort("Failed")
        assert err2.status == "failed"
    
    def test_end_task_with_message(self, test_config, fake_slack):
        """Test ending a task with a message."""
        with tempfile.TemporaryDirectory() as tmp:
            test_config.database.path = f"{tmp}/test.db"
            database = Database(f"{tmp}/test.db")
            
            # Create a task
            task = database.create_task(
                slack_user_id="U123",
                slack_channel_id="C123",
                slack_thread_ts="123.456",
                repo_owner="testorg",
                repo_name="testrepo",
                branch_name="main",
                task_description="Test task"
            )
            
            worker = Worker(test_config, database, fake_slack)
            
            # End task with message
            worker._end_task(task, "Test message", "failed")
            
            # Check task was updated
            updated = database.get_task(task.id)
            assert updated.status == TaskStatus.FAILED
            assert "Test message" in updated.error_message


class TestDatabaseIntegration:
    """Tests for database and worker integration."""
    
    def test_task_lifecycle(self, test_config, fake_slack, fake_github, fake_sandbox):
        """Test task through various lifecycle stages."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/test.db"
            database = Database(db_path)
            
            # Create task
            task = database.create_task(
                slack_user_id="U123",
                slack_channel_id="C123",
                slack_thread_ts="123.456",
                repo_owner="testorg",
                repo_name="testrepo",
                branch_name="main",
                task_description="Add a test feature"
            )
            
            assert task.status == TaskStatus.PENDING
            
            # Verify database methods work
            tasks = database.get_pending_tasks()
            assert len(tasks) == 1
            assert tasks[0].id == task.id
            
            # Test claiming
            claimed = database.claim_task(task.id, "worker-1")
            assert claimed is True
            
            # Test releasing
            database.release_task(task.id)
            released = database.get_task(task.id)
            assert released.worker_id is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])