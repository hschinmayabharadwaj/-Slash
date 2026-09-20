"""AWS-native worker that consumes tasks from SQS instead of polling SQLite.

This module provides :class:`SQSWorker`, an ECS/Lambda-compatible replacement
for the in-process :class:`~slackagent.worker.Worker`.  The key differences are:

* **SQS long-polling** replaces the SQLite polling loop, which means the worker
  process can scale horizontally without coordination.
* **Dead-letter queue** (DLQ) handling is delegated to SQS itself: if a message
  is received more than ``maxReceiveCount`` times (configured on the queue) it is
  automatically moved to the DLQ without any application-level bookkeeping.
* **CloudWatch metrics** are emitted for task duration, per-phase duration, and
  success/failure counts so that the alarms in the CDK monitoring stack fire
  correctly.
* **Step Functions callback tokens** – if the SQS message contains a
  ``taskToken`` field the worker sends ``SendTaskSuccess`` / ``SendTaskFailure``
  after processing, enabling Step Functions wait-for-callback patterns.
* **Graceful shutdown** – a ``SIGTERM`` handler (sent by ECS before task
  termination) sets a flag so the worker finishes its current message and then
  exits cleanly.

The constructor also accepts a ``use_sqs=False`` flag that falls back to the
original SQLite polling behaviour (delegated to :class:`Worker`) so that existing
local / CI deployments continue to work unchanged.
"""

import json
import logging
import os
import signal
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .aws_client import AWSClient
from .config import Config
from .database import Database, Task, TaskStatus
from .worker import Worker

logger = logging.getLogger(__name__)

# How long to wait for messages in a single long-poll call (max = 20 s per AWS docs)
_SQS_WAIT_SECONDS = 20
# Visibility timeout extension when processing a task (must be >= task timeout)
_VISIBILITY_EXTENSION_SECONDS = 900
# How many seconds between heartbeat visibility extensions
_HEARTBEAT_INTERVAL = 60


class SQSWorker:
    """Worker that drives task execution from an SQS queue.

    In SQS mode (``use_sqs=True``, the default) the worker long-polls the
    ``SQS_QUEUE_URL`` queue.  Each message must be a JSON object with at least::

        {
            "taskId": "<int or str>",
            "action": "plan" | "implement" | "create_pr",
            // optional:
            "taskToken": "<step-functions-callback-token>"
        }

    In legacy mode (``use_sqs=False``) the worker delegates to
    :class:`~slackagent.worker.Worker` and behaves identically to the original
    SQLite-polling implementation.

    Parameters
    ----------
    config:
        Application configuration.
    database:
        Database instance (may be an :class:`~slackagent.database_aws.AWSDatabase`).
    slack_handler:
        Slack handler used to post updates to threads.
    aws_client:
        Optional pre-constructed :class:`AWSClient`.  If *None* a new instance
        is created automatically when ``use_sqs=True``.
    use_sqs:
        When *True* (default) the SQS long-poll loop is used.  When *False* the
        constructor creates an inner :class:`Worker` and delegates ``run()`` to
        it, ignoring all SQS / AWS arguments.
    worker_id:
        Optional human-readable identifier used in log messages.
    """

    def __init__(
        self,
        config: Config,
        database: Database,
        slack_handler: "SlackHandler",  # noqa: F821 – forward ref avoids circular import
        aws_client: Optional[AWSClient] = None,
        use_sqs: bool = True,
        worker_id: Optional[str] = None,
    ):
        self.config = config
        self.database = database
        self.slack_handler = slack_handler
        self.worker_id = worker_id or str(uuid.uuid4())[:8]
        self._use_sqs = use_sqs
        self._shutdown = False

        if use_sqs:
            self._aws = aws_client or AWSClient()
            queue_url = self._aws.queue_url or os.environ.get('SQS_QUEUE_URL', '')
            if not queue_url:
                raise ValueError(
                    'SQS_QUEUE_URL environment variable must be set when use_sqs=True'
                )
            self._queue_url = queue_url
            logger.info(f'SQSWorker {self.worker_id} initialised (SQS mode, queue={queue_url})')
        else:
            # Delegate to the original SQLite worker
            self._legacy_worker = Worker(
                config=config,
                database=database,
                slack_handler=slack_handler,
                worker_id=self.worker_id,
            )
            self._aws = aws_client or AWSClient()
            logger.info(f'SQSWorker {self.worker_id} initialised (legacy SQLite polling mode)')

        # Register SIGTERM handler for graceful ECS shutdown
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        signal.signal(signal.SIGINT, self._handle_sigterm)

        # Import here to avoid circular imports at module load time
        from .agent_runner import AgentRunner, format_check_results
        from .github_ops import GitHubClient, GitOperations
        from .security import SecurityManager

        self._format_check_results = format_check_results
        self.github_client = GitHubClient(config.github)
        self.security_manager = SecurityManager(config.security)
        self.agent_runner = AgentRunner(
            config.agent,
            config.docker,
            SecurityManager(config.security),
            config.gemini.api_key,
        )
        self._GitOperations = GitOperations

    # ------------------------------------------------------------------
    # Signal handling
    # ------------------------------------------------------------------

    def _handle_sigterm(self, signum: int, frame: Any) -> None:
        """Mark shutdown so the main loop exits after the current message."""
        logger.info(f'SQSWorker {self.worker_id} received signal {signum}, shutting down gracefully…')
        self._shutdown = True

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the worker loop.

        In SQS mode this long-polls the queue until ``SIGTERM`` is received.
        In legacy mode this delegates to the original :class:`Worker` run loop.
        """
        if not self._use_sqs:
            logger.info(f'SQSWorker {self.worker_id} running in legacy mode')
            self._legacy_worker.run()
            return

        logger.info(f'SQSWorker {self.worker_id} starting SQS loop')
        while not self._shutdown:
            try:
                self._poll_once()
            except Exception as e:
                logger.error(f'SQSWorker {self.worker_id} unexpected error in poll loop: {e}', exc_info=True)
                if not self._shutdown:
                    time.sleep(5)

        logger.info(f'SQSWorker {self.worker_id} stopped')

    def _poll_once(self) -> None:
        """Perform a single SQS long-poll and process any received message."""
        response = self._aws.sqs.receive_message(
            QueueUrl=self._queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=_SQS_WAIT_SECONDS,
            AttributeNames=['ApproximateReceiveCount'],
            MessageAttributeNames=['All'],
        )

        messages = response.get('Messages', [])
        if not messages:
            return  # No messages; loop again

        message = messages[0]
        receipt_handle = message['ReceiptHandle']
        receive_count = int(message.get('Attributes', {}).get('ApproximateReceiveCount', '1'))

        try:
            body = json.loads(message['Body'])
        except json.JSONDecodeError as e:
            logger.error(f'Failed to parse SQS message body: {e} – deleting poisoned message')
            self._delete_message(receipt_handle)
            return

        task_id_raw = body.get('taskId')
        action = body.get('action', 'plan')
        task_token: Optional[str] = body.get('taskToken')  # Step Functions callback token

        if task_id_raw is None:
            logger.error(f'SQS message missing taskId, deleting: {body}')
            self._delete_message(receipt_handle)
            return

        task_id = int(task_id_raw)
        logger.info(
            f'SQSWorker {self.worker_id} received message for task {task_id} '
            f'action={action} receive_count={receive_count}'
        )

        start_ts = time.monotonic()
        success = False
        error_msg = ''

        try:
            # Extend visibility so we have time to process
            self._aws.sqs.change_message_visibility(
                QueueUrl=self._queue_url,
                ReceiptHandle=receipt_handle,
                VisibilityTimeout=_VISIBILITY_EXTENSION_SECONDS,
            )

            task = self.database.get_task(task_id)
            if task is None:
                # Try DynamoDB if not in SQLite
                dynamo_state = self._aws.get_task_state(str(task_id))
                if dynamo_state is None:
                    raise ValueError(f'Task {task_id} not found in SQLite or DynamoDB')
                logger.warning(f'Task {task_id} not in SQLite, found in DynamoDB only – skipping')
                self._delete_message(receipt_handle)
                return

            # Dispatch to the appropriate processing phase
            phase_start = time.monotonic()
            if action == 'plan' or task.status == TaskStatus.PENDING:
                self._process_planning(task, receipt_handle)
            elif action == 'implement' or task.status == TaskStatus.PLAN_APPROVED:
                self._process_implementation(task, receipt_handle)
            elif action == 'create_pr' or task.status == TaskStatus.IMPL_APPROVED:
                self._process_pr_creation(task, receipt_handle)
            else:
                logger.warning(
                    f'Task {task_id} is in status {task.status.value} with action={action}; '
                    f'no handler – deleting message'
                )
                self._delete_message(receipt_handle)
                return

            phase_duration = time.monotonic() - phase_start
            self._aws.put_metric('PhaseDuration', phase_duration, unit='Seconds',
                                 dimensions={'Action': action})
            success = True

        except Exception as e:
            error_msg = str(e)
            logger.error(
                f'SQSWorker {self.worker_id} failed to process task {task_id}: {e}',
                exc_info=True,
            )
            self._handle_task_failure(task_id, error_msg)
            # Let the message go back to the queue (do NOT delete it).
            # After maxReceiveCount attempts SQS will move it to the DLQ.

        finally:
            total_duration = time.monotonic() - start_ts
            self._aws.put_metric(
                'TaskDuration', total_duration, unit='Seconds',
                dimensions={'Action': action},
            )
            if success:
                self._aws.put_metric('TasksSucceeded', 1, dimensions={'Action': action})
                # Send Step Functions success token if present
                if task_token:
                    self._aws.send_task_success(task_token, {
                        'taskId': str(task_id),
                        'action': action,
                        'durationSeconds': total_duration,
                    })
            else:
                self._aws.put_metric('TasksFailed', 1, dimensions={'Action': action})
                # Send Step Functions failure token if present
                if task_token:
                    self._aws.send_task_failure(
                        task_token,
                        error='TaskProcessingFailed',
                        cause=error_msg[:256],
                    )

    # ------------------------------------------------------------------
    # Processing phases (mirror of Worker._process_* but called from SQS)
    # ------------------------------------------------------------------

    def _process_planning(self, task: Task, receipt_handle: str) -> None:
        """Run the planning phase for *task*.

        On success the SQLite task status is set to
        ``AWAITING_PLAN_APPROVAL`` and the SQS message is deleted.

        Args:
            task: Task to plan.
            receipt_handle: SQS receipt handle for the originating message.
        """
        import tempfile
        from pathlib import Path

        logger.info(f'Planning phase for task {task.id}')
        self.database.update_task_status(task.id, TaskStatus.PLANNING)

        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / 'repo'
            try:
                clone_url = self.github_client.get_clone_url(task.repo_owner, task.repo_name)
                self._GitOperations.clone_repository(clone_url, workdir)

                plan = self.agent_runner.run_planning_phase(workdir, task.task_description)

                self.database.update_task_plan(task.id, plan)
                self.database.update_task_status(task.id, TaskStatus.AWAITING_PLAN_APPROVAL)

                self.slack_handler.post_plan(task.id, plan)
                logger.info(f'Planning phase complete for task {task.id}')

                # Only delete the message on success
                self._delete_message(receipt_handle)

            except Exception as e:
                raise Exception(f'Planning failed: {e}') from e

    def _process_implementation(self, task: Task, receipt_handle: str) -> None:
        """Run the implementation phase for *task*.

        On success the SQLite task status is set to
        ``AWAITING_IMPL_APPROVAL`` and the SQS message is deleted.

        Args:
            task: Task to implement.
            receipt_handle: SQS receipt handle for the originating message.
        """
        import tempfile
        from pathlib import Path

        logger.info(f'Implementation phase for task {task.id}')
        self.database.update_task_status(task.id, TaskStatus.IMPLEMENTING)

        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / 'repo'
            try:
                clone_url = self.github_client.get_clone_url(task.repo_owner, task.repo_name)
                self._GitOperations.clone_repository(clone_url, workdir)
                self._GitOperations.create_branch(workdir, task.branch_name)

                summary = self.agent_runner.run_implementation_phase(
                    workdir, task.task_description, task.plan or ''
                )

                if not self._GitOperations.has_changes(workdir):
                    raise Exception('No changes were made by the agent')

                self._GitOperations.commit_changes(
                    workdir, f'Implement: {task.task_description[:50]}'
                )
                diff = self._GitOperations.get_diff(workdir, cached=True)

                secret_warning = self.security_manager.scan_diff_for_secrets(diff)
                if secret_warning:
                    raise Exception(f'Security check failed:\n{secret_warning}')

                check_results = self.agent_runner.run_checks(workdir)
                check_summary = self._format_check_results(check_results)
                full_summary = f'{summary}\n\n**Checks:**\n{check_summary}'

                self.database.update_task_implementation(task.id, full_summary, diff)
                self.database.update_task_status(task.id, TaskStatus.AWAITING_IMPL_APPROVAL)

                diff_preview = diff[:1000] + '\n… (truncated)' if len(diff) > 1000 else diff
                self.slack_handler.post_implementation(task.id, full_summary, diff_preview)

                logger.info(f'Implementation phase complete for task {task.id}')
                self._delete_message(receipt_handle)

            except Exception as e:
                raise Exception(f'Implementation failed: {e}') from e

    def _process_pr_creation(self, task: Task, receipt_handle: str) -> None:
        """Run the PR creation phase for *task*.

        On success the SQLite task status is set to ``COMPLETED`` and the SQS
        message is deleted.

        Args:
            task: Task for which to create a PR.
            receipt_handle: SQS receipt handle for the originating message.
        """
        import tempfile
        from pathlib import Path

        logger.info(f'Creating PR for task {task.id}')
        self.database.update_task_status(task.id, TaskStatus.CREATING_PR)

        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / 'repo'
            try:
                clone_url = self.github_client.get_clone_url(task.repo_owner, task.repo_name)
                self._GitOperations.clone_repository(clone_url, workdir)
                self._GitOperations.create_branch(workdir, task.branch_name)

                self.agent_runner.run_implementation_phase(
                    workdir, task.task_description, task.plan or ''
                )

                if not self._GitOperations.has_changes(workdir):
                    raise Exception('No changes to commit')

                commit_message = f'{task.task_description}\n\nTask ID: {task.id}'
                self._GitOperations.commit_changes(workdir, commit_message)
                self._GitOperations.push_branch(workdir, task.branch_name)

                pr_title = task.task_description[:70]
                pr_body = (
                    f'## Task Description\n\n{task.task_description}\n\n'
                    f'## Implementation Plan\n\n{task.plan or "N/A"}\n\n'
                    f'## Summary\n\n{task.implementation_summary or "N/A"}\n\n'
                    f'---\n_This PR was created automatically by Slack Coding Agent'
                    f' (Task #{task.id})_\n'
                )

                pr_url = self.github_client.create_pull_request(
                    task.repo_owner, task.repo_name, task.branch_name, pr_title, pr_body
                )

                self.database.update_task_pr(task.id, pr_url)
                self.database.update_task_status(task.id, TaskStatus.COMPLETED)

                self.slack_handler.post_pr_created(task.id, pr_url)

                logger.info(f'PR created for task {task.id}: {pr_url}')
                self._delete_message(receipt_handle)

            except Exception as e:
                raise Exception(f'PR creation failed: {e}') from e

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _delete_message(self, receipt_handle: str) -> None:
        """Delete a processed message from the SQS queue.

        Args:
            receipt_handle: The receipt handle returned when the message was received.
        """
        try:
            self._aws.sqs.delete_message(
                QueueUrl=self._queue_url,
                ReceiptHandle=receipt_handle,
            )
        except Exception as e:
            logger.warning(f'Failed to delete SQS message (non-fatal): {e}')

    def _handle_task_failure(self, task_id: int, error_message: str) -> None:
        """Mark a task as failed in the database and notify via Slack.

        Args:
            task_id: Task identifier.
            error_message: Human-readable error description.
        """
        logger.error(f'Task {task_id} failed: {error_message}')
        try:
            self.database.update_task_status(task_id, TaskStatus.FAILED, error_message=error_message)
            self.slack_handler.post_error(task_id, error_message)
        except Exception as e:
            logger.error(f'Failed to record task failure for {task_id}: {e}')


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point for the SQS worker process.

    Reads configuration from the path given as the first positional argument
    (defaults to ``config.yaml``), then starts the :class:`SQSWorker`.

    Pass ``--legacy`` as a second argument to use the SQLite polling mode.
    """
    import sys

    from .config import load_config
    from .database_aws import AWSDatabase
    from .slack_handler import SlackHandler

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )

    config_path = sys.argv[1] if len(sys.argv) > 1 else 'config.yaml'
    use_sqs = '--legacy' not in sys.argv

    try:
        config = load_config(config_path)
    except Exception as e:
        logger.error(f'Failed to load configuration: {e}')
        sys.exit(1)

    database = AWSDatabase(config.database.path, use_aws=use_sqs)
    slack_handler = SlackHandler(config, database)

    worker = SQSWorker(
        config=config,
        database=database,
        slack_handler=slack_handler,
        use_sqs=use_sqs,
    )
    worker.run()


if __name__ == '__main__':
    main()
