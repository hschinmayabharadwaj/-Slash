"""Worker process for executing tasks."""

import logging
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from .agent_runner import AgentRunner, format_check_results
from .config import Config
from .database import Database, Task, TaskStatus
from .github_ops import GitHubClient, GitOperations, GitHubOperationError
from .security import SecurityManager

logger = logging.getLogger(__name__)


class Worker:
    """Background worker for processing tasks."""

    def __init__(
        self,
        config: Config,
        database: Database,
        slack_handler: "SlackHandler",
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
        self.security_manager = SecurityManager(config.security)
        self.agent_runner = AgentRunner(
            config.agent,
            config.docker,
            self.security_manager,
            config.gemini.api_key,
        )

        logger.info(f"Worker {self.worker_id} initialized")

    def run(self) -> None:
        """Run worker loop."""
        logger.info(f"Worker {self.worker_id} starting")

        while True:
            try:
                # Get pending tasks
                tasks = self.database.get_pending_tasks(limit=1)

                if not tasks:
                    # No tasks, sleep and retry
                    time.sleep(self.config.worker.poll_interval)
                    continue

                task = tasks[0]

                # Try to claim the task
                if not self.database.claim_task(task.id, self.worker_id):
                    # Task already claimed by another worker
                    continue

                logger.info(f"Worker {self.worker_id} processing task {task.id}")

                # Process the task based on current status
                try:
                    if task.status == TaskStatus.PENDING:
                        self._process_planning(task)
                    elif task.status == TaskStatus.PLAN_APPROVED:
                        self._process_implementation(task)
                    elif task.status == TaskStatus.IMPL_APPROVED:
                        self._process_pr_creation(task)
                except Exception as e:
                    logger.error(
                        f"Worker {self.worker_id} failed to process task {task.id}: {e}",
                        exc_info=True,
                    )
                    self._handle_task_failure(task, str(e))
                finally:
                    # Release the task
                    self.database.release_task(task.id)

            except Exception as e:
                logger.error(f"Worker {self.worker_id} error: {e}", exc_info=True)
                time.sleep(self.config.worker.poll_interval)

    def _process_planning(self, task: Task) -> None:
        """Process planning phase.

        Args:
            task: Task to process
        """
        logger.info(f"Planning phase for task {task.id}")
        self.database.update_task_status(task.id, TaskStatus.PLANNING)

        # Create temporary working directory
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "repo"

            try:
                # Clone repository
                clone_url = self.github_client.get_clone_url(
                    task.repo_owner, task.repo_name
                )
                GitOperations.clone_repository(clone_url, workdir)

                # Run planning phase
                plan = self.agent_runner.run_planning_phase(
                    workdir, task.task_description
                )

                # Save plan
                self.database.update_task_plan(task.id, plan)
                self.database.update_task_status(
                    task.id, TaskStatus.AWAITING_PLAN_APPROVAL
                )

                # Post plan to Slack
                self.slack_handler.post_plan(task.id, plan)

                logger.info(f"Planning phase complete for task {task.id}")

            except Exception as e:
                raise Exception(f"Planning failed: {e}")

    def _process_implementation(self, task: Task) -> None:
        """Process implementation phase.

        Args:
            task: Task to process
        """
        logger.info(f"Implementation phase for task {task.id}")
        self.database.update_task_status(task.id, TaskStatus.IMPLEMENTING)

        # Create temporary working directory
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "repo"

            try:
                # Clone repository
                clone_url = self.github_client.get_clone_url(
                    task.repo_owner, task.repo_name
                )
                GitOperations.clone_repository(clone_url, workdir)

                # Create new branch
                GitOperations.create_branch(workdir, task.branch_name)

                # Run implementation phase
                summary = self.agent_runner.run_implementation_phase(
                    workdir, task.task_description, task.plan or ""
                )

                # Check if there are changes
                if not GitOperations.has_changes(workdir):
                    raise Exception("No changes were made by the agent")

                # Stage changes and get diff
                GitOperations.commit_changes(
                    workdir,
                    f"Implement: {task.task_description[:50]}",
                )
                diff = GitOperations.get_diff(workdir, cached=True)

                # Security: Scan diff for secrets
                secret_warning = self.security_manager.scan_diff_for_secrets(diff)
                if secret_warning:
                    raise Exception(f"Security check failed:\n{secret_warning}")

                # Run checks
                check_results = self.agent_runner.run_checks(workdir)
                check_summary = format_check_results(check_results)

                # Save implementation results
                full_summary = f"{summary}\n\n**Checks:**\n{check_summary}"
                self.database.update_task_implementation(
                    task.id, full_summary, diff
                )
                self.database.update_task_status(
                    task.id, TaskStatus.AWAITING_IMPL_APPROVAL
                )

                # Truncate diff for Slack display
                diff_preview = diff[:1000] + "\n... (truncated)" if len(diff) > 1000 else diff

                # Post implementation to Slack
                self.slack_handler.post_implementation(
                    task.id, full_summary, diff_preview
                )

                logger.info(f"Implementation phase complete for task {task.id}")

            except Exception as e:
                raise Exception(f"Implementation failed: {e}")

    def _process_pr_creation(self, task: Task) -> None:
        """Process PR creation.

        Args:
            task: Task to process
        """
        logger.info(f"Creating PR for task {task.id}")
        self.database.update_task_status(task.id, TaskStatus.CREATING_PR)

        # Create temporary working directory
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "repo"

            try:
                # Clone repository
                clone_url = self.github_client.get_clone_url(
                    task.repo_owner, task.repo_name
                )
                GitOperations.clone_repository(clone_url, workdir)

                # Create and checkout branch
                GitOperations.create_branch(workdir, task.branch_name)

                # Re-apply changes (we need to recreate them)
                # In a production system, you'd save the changes and reapply them
                # For now, we'll run implementation again
                summary = self.agent_runner.run_implementation_phase(
                    workdir, task.task_description, task.plan or ""
                )

                if not GitOperations.has_changes(workdir):
                    raise Exception("No changes to commit")

                # Commit changes
                commit_message = f"{task.task_description}\n\nTask ID: {task.id}"
                GitOperations.commit_changes(workdir, commit_message)

                # Push branch
                GitOperations.push_branch(workdir, task.branch_name)

                # Create PR
                pr_title = task.task_description[:70]
                pr_body = f"""## Task Description

{task.task_description}

## Implementation Plan

{task.plan or 'N/A'}

## Summary

{task.implementation_summary or 'N/A'}

---
_This PR was created automatically by Slack Coding Agent (Task #{task.id})_
"""

                pr_url = self.github_client.create_pull_request(
                    task.repo_owner,
                    task.repo_name,
                    task.branch_name,
                    pr_title,
                    pr_body,
                )

                # Save PR URL
                self.database.update_task_pr(task.id, pr_url)
                self.database.update_task_status(task.id, TaskStatus.COMPLETED)

                # Post PR to Slack
                self.slack_handler.post_pr_created(task.id, pr_url)

                logger.info(f"PR created for task {task.id}: {pr_url}")

            except Exception as e:
                raise Exception(f"PR creation failed: {e}")

    def _handle_task_failure(self, task: Task, error_message: str) -> None:
        """Handle task failure.

        Args:
            task: Failed task
            error_message: Error message
        """
        logger.error(f"Task {task.id} failed: {error_message}")

        # Update task status
        self.database.update_task_status(
            task.id, TaskStatus.FAILED, error_message=error_message
        )

        # Post error to Slack
        self.slack_handler.post_error(task.id, error_message)


def main() -> None:
    """Main entry point for worker process."""
    import sys

    from .config import load_config

    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Load configuration
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    try:
        config = load_config(config_path)
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        sys.exit(1)

    # Initialize database
    database = Database(config.database.path)

    # Initialize Slack handler (for posting updates)
    from .slack_handler import SlackHandler

    slack_handler = SlackHandler(config, database)

    # Create and run worker
    worker = Worker(config, database, slack_handler)
    worker.run()


if __name__ == "__main__":
    main()
