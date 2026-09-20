"""
Workflow State: PLAN
Generates an implementation plan using Gemini AI (stubbed for initial deploy).
In production this would call the Gemini API and post the plan to Slack.
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
    text = event.get('text', '')
    channel = event.get('channel', '')
    now = datetime.now(timezone.utc).isoformat()

    if not task_id:
        raise ValueError('taskId is required')

    # ── TODO: call Gemini API here ────────────────────────────────────────────
    # In production, replace the stub plan with a real Gemini call:
    #
    #   import google.generativeai as genai
    #   genai.configure(api_key=get_gemini_key())
    #   model = genai.GenerativeModel('gemini-1.5-pro')
    #   response = model.generate_content(f"Create a detailed implementation plan for: {text}")
    #   plan_text = response.text
    #
    plan_text = (
        f'[STUB PLAN] Implementation plan for: {text}\n\n'
        '1. Analyse the existing codebase\n'
        '2. Identify the relevant files to modify\n'
        '3. Implement the requested changes\n'
        '4. Add or update tests\n'
        '5. Create a pull request\n'
    )

    plan_id = f'{task_id}-plan'

    # Update DynamoDB
    table = dynamodb.Table(TABLE)
    table.update_item(
        Key={'taskId': task_id, 'timestamp': 'LATEST'},
        UpdateExpression='SET #s = :s, planText = :p, planId = :pid, updatedAt = :t',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={
            ':s': 'PLAN_READY',
            ':p': plan_text,
            ':pid': plan_id,
            ':t': now,
        },
    )

    # Notify via SNS (triggers Slack message in real implementation)
    sns.publish(
        TopicArn=TOPIC,
        Subject=f'Plan ready for task {task_id}',
        Message=json.dumps({
            'taskId': task_id,
            'planId': plan_id,
            'channel': channel,
            'planText': plan_text,
            'event': 'PlanReady',
        }),
    )

    logger.info('Plan generated for task %s', task_id)
    return {
        **event,
        'status': 'PLAN_READY',
        'planId': plan_id,
        'planText': plan_text,
        'plannedAt': now,
    }
