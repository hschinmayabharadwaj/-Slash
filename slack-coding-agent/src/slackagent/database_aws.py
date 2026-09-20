"""AWS-extended database that mirrors writes to DynamoDB/SQS while keeping SQLite as the source of truth.

This module provides :class:`AWSDatabase`, a drop-in replacement for
:class:`~slackagent.database.Database` that transparently syncs every write
operation to the AWS services provisioned by the CDK stacks:

* **DynamoDB** – task state and history records via ``save_task_state``
* **SQS** – task action messages enqueued via ``enqueue_task``
* **EventBridge** – lifecycle events (``TaskCreated``, ``TaskStatusChanged``,
  ``TaskCompleted``) via ``put_event``
* **CloudWatch** – per-transition metrics via ``put_metric``
* **S3** – diff artefacts uploaded and stored as pre-signed URLs via
  ``upload_diff``
* **SNS** – completion / failure notifications via ``notify``

All AWS calls are best-effort: if they fail the underlying SQLite operation is
**not** rolled back and execution continues normally, so the service degrades
gracefully when AWS credentials are unavailable or services are unreachable.
"""

import logging
from datetime import datetime
from typing import List, Optional

from .aws_client import AWSClient
from .database import Database, Task, TaskStatus

logger = logging.getLogger(__name__)


class AWSDatabase(Database):
    """Database subclass that mirrors every write to AWS services.

    Parameters
    ----------
    db_path:
        Path passed through to the parent :class:`Database`.
    use_aws:
        When *True* (default) an :class:`AWSClient` is instantiated and all
        write methods are mirrored to AWS.  Set to *False* to behave identically
        to the plain :class:`Database` (useful in local / test environments where
        AWS credentials are not available).
    aws_client:
        An existing :class:`AWSClient` instance to use.  If *None* and
        ``use_aws`` is *True* a new instance is created automatically.
    """

    def __init__(
        self,
        db_path: str,
        use_aws: bool = True,
        aws_client: Optional[AWSClient] = None,
    ):
        super().__init__(db_path)
        self._use_aws = use_aws
        if use_aws:
            self._aws: Optional[AWSClient] = aws_client or AWSClient()
            logger.info('AWSDatabase: AWS sync enabled')
        else:
            self._aws = None
            logger.info('AWSDatabase: AWS sync disabled, using SQLite only')

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _task_to_dynamo_state(self, task: Task) -> dict:
        """Convert a :class:`Task` dataclass to a flat dict suitable for DynamoDB."""
        return {
            'taskId': str(task.id),
            'status': task.status.value,
            'slackUserId': task.slack_user_id,
            'slackChannelId': task.slack_channel_id,
            'slackThreadTs': task.slack_thread_ts,
            'repoOwner': task.repo_owner,
            'repoName': task.repo_name,
            'branchName': task.branch_name,
            'taskDescription': task.task_description,
            'plan': task.plan,
            'implementationSummary': task.implementation_summary,
            'prUrl': task.pr_url,
            'errorMessage': task.error_message,
            'createdAt': task.created_at.isoformat() if task.created_at else None,
            'updatedAt': task.updated_at.isoformat() if task.updated_at else None,
        }

    def _sync_task(self, task_id: int) -> Optional[Task]:
        """Fetch the latest task from SQLite and push it to DynamoDB."""
        if not self._use_aws or self._aws is None:
            return None
        task = self.get_task(task_id)
        if task:
            try:
                self._aws.save_task_state(str(task_id), self._task_to_dynamo_state(task))
            except Exception as e:
                logger.warning(f'AWS sync for task {task_id} failed (non-fatal): {e}')
        return task

    # ------------------------------------------------------------------
    # Overrides
    # ------------------------------------------------------------------

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
        """Create a task in SQLite then mirror to AWS.

        In addition to persisting in SQLite, this method:

        1. Saves the initial state to DynamoDB.
        2. Puts a ``TaskCreated`` event on EventBridge.
        3. Enqueues a ``plan`` action on SQS so the worker picks it up.
        4. Optionally starts a Step Functions workflow if configured.

        Args:
            slack_user_id: Slack user who triggered the request.
            slack_channel_id: Slack channel ID.
            slack_thread_ts: Slack thread timestamp.
            repo_owner: GitHub organisation / user owning the repository.
            repo_name: GitHub repository name.
            branch_name: Feature branch name to use.
            task_description: Free-text description of the task.

        Returns:
            The newly created :class:`Task`.
        """
        task = super().create_task(
            slack_user_id,
            slack_channel_id,
            slack_thread_ts,
            repo_owner,
            repo_name,
            branch_name,
            task_description,
        )

        if self._use_aws and self._aws is not None:
            try:
                state = self._task_to_dynamo_state(task)
                # 1. DynamoDB
                self._aws.save_task_state(str(task.id), state)
                # 2. EventBridge
                self._aws.put_event('TaskCreated', {
                    'taskId': str(task.id),
                    'repo': f'{repo_owner}/{repo_name}',
                    'branch': branch_name,
                    'requestedBy': slack_user_id,
                    'description': task_description,
                })
                # 3. SQS – worker picks this up and starts planning
                self._aws.enqueue_task(str(task.id), 'plan', {
                    'slackChannelId': slack_channel_id,
                    'slackThreadTs': slack_thread_ts,
                })
                # 4. CloudWatch – task created counter
                self._aws.put_metric('TasksCreated', 1, dimensions={'Repo': f'{repo_owner}/{repo_name}'})
                # 5. Step Functions (if configured)
                execution_arn = self._aws.start_workflow(str(task.id), state)
                if execution_arn:
                    logger.info(f'Started SFN workflow for task {task.id}: {execution_arn}')
            except Exception as e:
                logger.warning(f'AWS create_task sync failed for task {task.id} (non-fatal): {e}')

        return task

    def update_task_status(
        self,
        task_id: int,
        status: TaskStatus,
        error_message: Optional[str] = None,
    ) -> None:
        """Update task status in SQLite then mirror to AWS.

        Additionally:

        * Syncs the updated record to DynamoDB.
        * Puts a ``TaskStatusChanged`` event on EventBridge.
        * Publishes a ``TaskFailed`` / ``TaskCompleted`` notification to SNS
          when the terminal states are reached.
        * Records a ``TaskStatusTransition`` metric on CloudWatch.

        Args:
            task_id: Task identifier.
            status: New :class:`TaskStatus`.
            error_message: Optional error details (only stored on failure).
        """
        super().update_task_status(task_id, status, error_message)

        if self._use_aws and self._aws is not None:
            try:
                task = self._sync_task(task_id)
                if task is None:
                    return

                # EventBridge
                self._aws.put_event('TaskStatusChanged', {
                    'taskId': str(task_id),
                    'status': status.value,
                    'errorMessage': error_message,
                })

                # CloudWatch
                self._aws.put_metric(
                    'TaskStatusTransition',
                    1,
                    dimensions={'Status': status.value},
                )

                # SNS for terminal states
                if status == TaskStatus.FAILED:
                    self._aws.notify(
                        subject=f'Task {task_id} Failed',
                        message=(
                            f'Task {task_id} ({task.task_description[:100]}) failed.\n'
                            f'Error: {error_message or "unknown"}'
                        ),
                        task_id=str(task_id),
                    )
                    self._aws.put_metric('TasksFailed', 1)
                elif status == TaskStatus.COMPLETED:
                    self._aws.put_metric('TasksCompleted', 1)
            except Exception as e:
                logger.warning(f'AWS update_task_status sync failed for task {task_id} (non-fatal): {e}')

    def update_task_plan(self, task_id: int, plan: str) -> None:
        """Persist plan to SQLite and mirror to DynamoDB.

        Args:
            task_id: Task identifier.
            plan: Generated plan text.
        """
        super().update_task_plan(task_id, plan)

        if self._use_aws and self._aws is not None:
            try:
                self._sync_task(task_id)
            except Exception as e:
                logger.warning(f'AWS update_task_plan sync failed for task {task_id} (non-fatal): {e}')

    def update_task_implementation(
        self,
        task_id: int,
        implementation_summary: str,
        diff: str,
    ) -> None:
        """Persist implementation results to SQLite, upload diff to S3, mirror to DynamoDB.

        The diff is uploaded to S3 (AES-256 encrypted) and a pre-signed URL is
        stored alongside the implementation summary in DynamoDB so reviewers can
        download the full diff without hitting the DynamoDB item size limit.

        Args:
            task_id: Task identifier.
            implementation_summary: Human-readable summary of changes.
            diff: Raw ``git diff`` output.
        """
        super().update_task_implementation(task_id, implementation_summary, diff)

        if self._use_aws and self._aws is not None:
            try:
                # Upload diff to S3 first so we can include the URL in DynamoDB
                diff_url: Optional[str] = None
                if diff:
                    diff_url = self._aws.upload_diff(str(task_id), diff)
                    if diff_url:
                        logger.info(f'Diff for task {task_id} uploaded to S3')

                task = self.get_task(task_id)
                if task:
                    state = self._task_to_dynamo_state(task)
                    if diff_url:
                        state['diffUrl'] = diff_url
                    self._aws.save_task_state(str(task_id), state)

                self._aws.put_metric('TasksImplemented', 1)
            except Exception as e:
                logger.warning(f'AWS update_task_implementation sync failed for task {task_id} (non-fatal): {e}')

    def update_task_pr(self, task_id: int, pr_url: str) -> None:
        """Persist PR URL to SQLite, mirror to DynamoDB, fire TaskCompleted event, notify SNS.

        Args:
            task_id: Task identifier.
            pr_url: URL of the created GitHub pull request.
        """
        super().update_task_pr(task_id, pr_url)

        if self._use_aws and self._aws is not None:
            try:
                task = self._sync_task(task_id)

                # EventBridge – TaskCompleted
                self._aws.put_event('TaskCompleted', {
                    'taskId': str(task_id),
                    'prUrl': pr_url,
                    'repo': f'{task.repo_owner}/{task.repo_name}' if task else 'unknown',
                })

                # SNS notification
                description = task.task_description[:100] if task else ''
                self._aws.notify(
                    subject=f'Task {task_id} Completed – PR Ready',
                    message=(
                        f'Task {task_id} completed successfully.\n'
                        f'Description: {description}\n'
                        f'Pull request: {pr_url}'
                    ),
                    task_id=str(task_id),
                )

                self._aws.put_metric('PRsCreated', 1)
            except Exception as e:
                logger.warning(f'AWS update_task_pr sync failed for task {task_id} (non-fatal): {e}')
