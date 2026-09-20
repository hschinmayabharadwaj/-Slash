"""
Workflow State: CREATE_PR
Creates a GitHub pull request with the implementation changes (stubbed).
In production this calls the GitHub API via the GitHub App credentials.
"""
import json, logging, os
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO'))

dynamodb = boto3.resource('dynamodb')
sns = boto3.client('sns')
secretsmanager = boto3.client('secretsmanager')
TABLE = os.environ['DYNAMODB_TABLE']
TOPIC = os.environ['SNS_TOPIC_ARN']


def handler(event, context):
    task_id = event.get('taskId', '')
    channel = event.get('channel', '')
    diff_text = event.get('diffText', '')
    now = datetime.now(timezone.utc).isoformat()

    if not task_id:
        raise ValueError('taskId is required')

    # ── TODO: call GitHub API to create a real PR ─────────────────────────────
    # In production:
    #   1. Fetch GitHub App credentials from Secrets Manager
    #   2. Generate a JWT and exchange for an installation token
    #   3. Use the token to push the branch and open a PR via PyGitHub / requests
    #
    pr_url = f'https://github.com/PLACEHOLDER/slack-coding-agent/pull/1'
    pr_number = 1
    branch_name = f'agent/{task_id[:8]}'

    # Update DynamoDB
    table = dynamodb.Table(TABLE)
    table.update_item(
        Key={'taskId': task_id, 'timestamp': 'LATEST'},
        UpdateExpression='SET #s = :s, prUrl = :pr, prNumber = :pn, branchName = :bn, updatedAt = :t',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={
            ':s': 'PR_CREATED',
            ':pr': pr_url,
            ':pn': pr_number,
            ':bn': branch_name,
            ':t': now,
        },
    )

    # Notify via SNS
    sns.publish(
        TopicArn=TOPIC,
        Subject=f'PR created for task {task_id}',
        Message=json.dumps({
            'taskId': task_id,
            'prUrl': pr_url,
            'prNumber': pr_number,
            'branchName': branch_name,
            'channel': channel,
            'event': 'PrCreated',
        }),
    )

    logger.info('PR created for task %s: %s', task_id, pr_url)
    return {
        **event,
        'status': 'PR_CREATED',
        'prUrl': pr_url,
        'prNumber': pr_number,
        'branchName': branch_name,
        'prCreatedAt': now,
    }
