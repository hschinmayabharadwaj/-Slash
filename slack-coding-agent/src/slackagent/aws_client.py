"""AWS service integrations for the Slack Coding Agent."""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3

logger = logging.getLogger(__name__)


class AWSClient:
    """Unified AWS client for all service integrations."""

    def __init__(self, region: str = None):
        self.region = region or os.environ.get('AWS_REGION', 'us-east-1')
        self.dynamodb = boto3.resource('dynamodb', region_name=self.region)
        self.sqs = boto3.client('sqs', region_name=self.region)
        self.sns = boto3.client('sns', region_name=self.region)
        self.events = boto3.client('events', region_name=self.region)
        self.s3 = boto3.client('s3', region_name=self.region)
        self.stepfunctions = boto3.client('stepfunctions', region_name=self.region)
        self.secretsmanager = boto3.client('secretsmanager', region_name=self.region)
        self.cloudwatch = boto3.client('cloudwatch', region_name=self.region)

        # Config from env
        self.table_name = os.environ.get('DYNAMODB_TABLE', 'TaskState')
        self.queue_url = os.environ.get('SQS_QUEUE_URL', '')
        self.sns_topic_arn = os.environ.get('SNS_TOPIC_ARN', '')
        self.event_bus_name = os.environ.get('EVENTBRIDGE_BUS', 'SlackAgentBus')
        self.s3_bucket = os.environ.get('S3_BUCKET', '')
        self.state_machine_arn = os.environ.get('STEP_FUNCTION_ARN', '')

    # --- DynamoDB Task State ---

    def save_task_state(self, task_id: str, state: Dict[str, Any]) -> None:
        """Save task state to DynamoDB.

        Writes both a LATEST record (used for point lookups) and a timestamped
        history record so callers can reconstruct state transitions.

        Args:
            task_id: Task identifier.
            state: Arbitrary key/value state to persist alongside the task.
        """
        if not self.table_name:
            return
        try:
            table = self.dynamodb.Table(self.table_name)
            now_iso = datetime.now(timezone.utc).isoformat()
            ttl = int(datetime.now(timezone.utc).timestamp()) + 30 * 86400  # 30 days
            base_item = {
                'taskId': str(task_id),
                'updatedAt': now_iso,
                'ttl': ttl,
                **{k: v for k, v in state.items() if v is not None},
            }
            # Current / latest record
            table.put_item(Item={**base_item, 'timestamp': 'LATEST'})
            # History record keyed by actual timestamp
            table.put_item(Item={**base_item, 'timestamp': now_iso})
        except Exception as e:
            logger.error(f'DynamoDB save_task_state error: {e}')

    def get_task_state(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get the latest task state from DynamoDB.

        Args:
            task_id: Task identifier.

        Returns:
            Item dict if found, otherwise None.
        """
        if not self.table_name:
            return None
        try:
            table = self.dynamodb.Table(self.table_name)
            response = table.get_item(Key={'taskId': str(task_id), 'timestamp': 'LATEST'})
            return response.get('Item')
        except Exception as e:
            logger.error(f'DynamoDB get_task_state error: {e}')
            return None

    def list_tasks_by_status(self, status: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Query tasks by status using the StatusIndex GSI.

        Args:
            status: Task status string to filter on.
            limit: Maximum number of items to return.

        Returns:
            List of matching DynamoDB items (may be empty on error).
        """
        if not self.table_name:
            return []
        try:
            from boto3.dynamodb.conditions import Key as DynamoKey
            table = self.dynamodb.Table(self.table_name)
            response = table.query(
                IndexName='StatusIndex',
                KeyConditionExpression=DynamoKey('status').eq(status),
                Limit=limit,
                ScanIndexForward=False,
            )
            return response.get('Items', [])
        except Exception as e:
            logger.error(f'DynamoDB list_tasks_by_status error: {e}')
            return []

    # --- SQS ---

    def enqueue_task(self, task_id: str, action: str, payload: Dict[str, Any] = None) -> bool:
        """Send a task action message to the SQS queue.

        Args:
            task_id: Task identifier.
            action: Action name (e.g. 'plan', 'implement', 'create_pr').
            payload: Optional additional data to include in the message body.

        Returns:
            True on success, False otherwise.
        """
        if not self.queue_url:
            return False
        try:
            message = json.dumps({
                'taskId': task_id,
                'action': action,
                'timestamp': datetime.now(timezone.utc).isoformat(),
                **(payload or {}),
            })
            self.sqs.send_message(
                QueueUrl=self.queue_url,
                MessageBody=message,
            )
            return True
        except Exception as e:
            logger.error(f'SQS enqueue_task error: {e}')
            return False

    # --- SNS ---

    def notify(self, subject: str, message: str, task_id: str = None) -> bool:
        """Publish a notification to the SNS topic.

        Args:
            subject: Notification subject (truncated to 100 chars by SNS rules).
            message: Notification body.
            task_id: Optional task identifier added as a message attribute.

        Returns:
            True on success, False otherwise.
        """
        if not self.sns_topic_arn:
            return False
        try:
            attrs: Dict[str, Any] = {}
            if task_id:
                attrs['taskId'] = {'DataType': 'String', 'StringValue': str(task_id)}
            self.sns.publish(
                TopicArn=self.sns_topic_arn,
                Subject=subject[:100],
                Message=message,
                MessageAttributes=attrs,
            )
            return True
        except Exception as e:
            logger.error(f'SNS notify error: {e}')
            return False

    # --- EventBridge ---

    def put_event(self, detail_type: str, detail: Dict[str, Any]) -> bool:
        """Put a custom event onto the EventBridge bus.

        Args:
            detail_type: Human-readable event type string.
            detail: Event detail payload (will be JSON-serialised).

        Returns:
            True on success, False otherwise.
        """
        if not self.event_bus_name:
            return False
        try:
            self.events.put_events(Entries=[{
                'Source': 'slack-agent',
                'DetailType': detail_type,
                'Detail': json.dumps(detail),
                'EventBusName': self.event_bus_name,
            }])
            return True
        except Exception as e:
            logger.error(f'EventBridge put_event error: {e}')
            return False

    # --- S3 ---

    def upload_diff(self, task_id: str, diff: str) -> Optional[str]:
        """Upload a git diff to S3 and return a pre-signed download URL.

        Objects are stored under ``diffs/<task_id>/<timestamp>.diff`` and
        encrypted at rest with AES-256.  The pre-signed URL is valid for 24 h.

        Args:
            task_id: Task identifier (used to namespace the S3 key).
            diff: Raw diff text to upload.

        Returns:
            Pre-signed URL string, or None on failure.
        """
        if not self.s3_bucket:
            return None
        try:
            key = f'diffs/{task_id}/{datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")}.diff'
            self.s3.put_object(
                Bucket=self.s3_bucket,
                Key=key,
                Body=diff.encode('utf-8'),
                ContentType='text/plain',
                ServerSideEncryption='AES256',
            )
            url = self.s3.generate_presigned_url(
                'get_object',
                Params={'Bucket': self.s3_bucket, 'Key': key},
                ExpiresIn=86400,
            )
            return url
        except Exception as e:
            logger.error(f'S3 upload_diff error: {e}')
            return None

    # --- CloudWatch Metrics ---

    def put_metric(
        self,
        metric_name: str,
        value: float,
        unit: str = 'Count',
        dimensions: Dict[str, str] = None,
    ) -> None:
        """Publish a custom metric to CloudWatch under the ``SlackAgent/Operations`` namespace.

        Args:
            metric_name: CloudWatch metric name.
            value: Numeric value to record.
            unit: CloudWatch unit string (default ``'Count'``).
            dimensions: Optional dict of dimension name → value pairs.
        """
        try:
            metric_data = {
                'MetricName': metric_name,
                'Value': value,
                'Unit': unit,
                'Dimensions': [
                    {'Name': k, 'Value': v} for k, v in (dimensions or {}).items()
                ],
            }
            self.cloudwatch.put_metric_data(
                Namespace='SlackAgent/Operations',
                MetricData=[metric_data],
            )
        except Exception as e:
            logger.error(f'CloudWatch put_metric error: {e}')

    # --- Secrets Manager ---

    def get_secret(self, secret_name: str) -> Optional[str]:
        """Retrieve a secret string from AWS Secrets Manager.

        If the stored secret is a JSON object the method returns the value of
        the ``password`` key (if present), otherwise the raw string.

        Args:
            secret_name: Secrets Manager secret name or ARN.

        Returns:
            Secret value string, or None on failure.
        """
        try:
            response = self.secretsmanager.get_secret_value(SecretId=secret_name)
            secret = response.get('SecretString', '')
            try:
                data = json.loads(secret)
                return data.get('password') or secret
            except json.JSONDecodeError:
                return secret
        except Exception as e:
            logger.error(f'Secrets Manager error for {secret_name}: {e}')
            return None

    # --- Step Functions ---

    def start_workflow(self, task_id: str, input_data: Dict[str, Any]) -> Optional[str]:
        """Start a Step Functions state machine execution for a task.

        The execution name is ``task-<task_id>-<unix_timestamp>`` which ensures
        uniqueness while remaining human-readable in the console.

        Args:
            task_id: Task identifier.
            input_data: Additional data to merge into the execution input JSON.

        Returns:
            Execution ARN string on success, or None on failure / not configured.
        """
        if not self.state_machine_arn:
            return None
        try:
            response = self.stepfunctions.start_execution(
                stateMachineArn=self.state_machine_arn,
                name=f'task-{task_id}-{int(datetime.now(timezone.utc).timestamp())}',
                input=json.dumps({'taskId': task_id, **input_data}),
            )
            return response['executionArn']
        except Exception as e:
            logger.error(f'Step Functions start_workflow error: {e}')
            return None

    def send_task_success(self, task_token: str, output: Dict[str, Any]) -> bool:
        """Send a task success heartbeat to Step Functions (for wait-for-callback patterns).

        Args:
            task_token: The task token provided in the SFN input.
            output: Output payload to return to the state machine.

        Returns:
            True on success, False otherwise.
        """
        try:
            self.stepfunctions.send_task_success(
                taskToken=task_token,
                output=json.dumps(output),
            )
            return True
        except Exception as e:
            logger.error(f'Step Functions send_task_success error: {e}')
            return False

    def send_task_failure(self, task_token: str, error: str, cause: str = '') -> bool:
        """Send a task failure heartbeat to Step Functions (for wait-for-callback patterns).

        Args:
            task_token: The task token provided in the SFN input.
            error: Short error code / name.
            cause: Longer human-readable cause string.

        Returns:
            True on success, False otherwise.
        """
        try:
            self.stepfunctions.send_task_failure(
                taskToken=task_token,
                error=error,
                cause=cause,
            )
            return True
        except Exception as e:
            logger.error(f'Step Functions send_task_failure error: {e}')
            return False
