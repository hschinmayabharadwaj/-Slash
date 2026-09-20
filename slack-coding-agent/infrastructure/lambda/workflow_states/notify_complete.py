"""
Workflow State: NOTIFY_COMPLETE
Sends a success notification to Slack and SNS.
"""
import json, logging, os
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO'))

dynamodb = boto3.resource('dynamodb')
sns = boto3.client('sns')
TABLE = os.environ['DYNAMODB_TABLE']
TOPIC = os.environ['SNS_TOPIC_ARN']


def handler(event, context):
    task_id = event.get('taskId', '')
    channel = event.get('channel', '')
    pr_url = event.get('prUrl', '')
    now = datetime.now(timezone.utc).isoformat()

    if not task_id:
        raise ValueError('taskId is required')

    # Update DynamoDB
    table = dynamodb.Table(TABLE)
    table.update_item(
        Key={'taskId': task_id, 'timestamp': 'LATEST'},
        UpdateExpression='SET #s = :s, completedAt = :t, updatedAt = :t',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={':s': 'COMPLETED', ':t': now},
    )

    # Send completion notification
    message = {
        'taskId': task_id,
        'status': 'COMPLETED',
        'channel': channel,
        'prUrl': pr_url,
        'completedAt': now,
        'event': 'TaskCompleted',
    }
    sns.publish(
        TopicArn=TOPIC,
        Subject=f'Task {task_id} completed successfully',
        Message=json.dumps(message),
    )

    # ── TODO: post to Slack via bot token ────────────────────────────────────
    # In production, call the Slack Web API:
    #   slack_client.chat_postMessage(
    #       channel=channel,
    #       text=f':white_check_mark: Task complete! PR: {pr_url}',
    #       thread_ts=event.get('threadTs'),
    #   )

    logger.info('Task %s completed. PR: %s', task_id, pr_url)
    return {**event, 'status': 'COMPLETED', 'completedAt': now}
