"""
Workflow State: VALIDATE
Validates the incoming task request before planning begins.
"""
import json, logging, os, re
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO'))

dynamodb = boto3.resource('dynamodb')
TABLE = os.environ['DYNAMODB_TABLE']

# Maximum task text length
MAX_TEXT_LEN = 4096
# Disallowed patterns (simple guardrail)
BLOCKED_PATTERNS = [
    re.compile(r'\bdrop\s+table\b', re.IGNORECASE),
    re.compile(r'\brm\s+-rf\b'),
    re.compile(r'\bsudo\s+rm\b'),
]


def handler(event, context):
    task_id = event.get('taskId', '')
    text = event.get('text', '')
    now = datetime.now(timezone.utc).isoformat()

    if not task_id:
        raise ValueError('taskId is required')

    if not text:
        raise ValueError('Task text is empty')

    if len(text) > MAX_TEXT_LEN:
        raise ValueError(f'Task text exceeds maximum length of {MAX_TEXT_LEN} characters')

    for pattern in BLOCKED_PATTERNS:
        if pattern.search(text):
            raise ValueError(f'Task text contains blocked content matching {pattern.pattern!r}')

    # Mark task as validated
    table = dynamodb.Table(TABLE)
    table.update_item(
        Key={'taskId': task_id, 'timestamp': 'LATEST'},
        UpdateExpression='SET #s = :s, updatedAt = :t',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={':s': 'VALIDATED', ':t': now},
    )

    logger.info('Task %s validated successfully', task_id)
    return {**event, 'status': 'VALIDATED', 'validatedAt': now}
