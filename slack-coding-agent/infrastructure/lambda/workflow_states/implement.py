"""
Workflow State: IMPLEMENT
Executes the approved plan (stubbed for initial deploy).
In production this triggers the sandboxed Docker ECS task / CLI runner.
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
    plan_text = event.get('planText', '')
    channel = event.get('channel', '')
    now = datetime.now(timezone.utc).isoformat()

    if not task_id:
        raise ValueError('taskId is required')

    # ── TODO: trigger the sandboxed ECS / Docker task here ───────────────────
    # In production, use ECS RunTask or SQS to dispatch to the worker:
    #
    #   ecs = boto3.client('ecs')
    #   ecs.run_task(
    #       cluster='SlackAgentCluster',
    #       taskDefinition='WorkerTask',
    #       overrides={'containerOverrides': [{'name': 'worker', 'environment': [...]}]},
    #       launchType='FARGATE', networkConfiguration={...}
    #   )
    #
    diff_text = (
        '--- a/example.py\n'
        '+++ b/example.py\n'
        '@@ -1,3 +1,5 @@\n'
        '+# Changes applied by Slack Coding Agent\n'
        ' def main():\n'
        '-    pass\n'
        '+    print("Hello from the agent!")\n'
        '+    return 0\n'
    )
    impl_id = f'{task_id}-impl'

    # Update DynamoDB
    table = dynamodb.Table(TABLE)
    table.update_item(
        Key={'taskId': task_id, 'timestamp': 'LATEST'},
        UpdateExpression='SET #s = :s, diffText = :d, implId = :iid, updatedAt = :t',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={
            ':s': 'IMPL_READY',
            ':d': diff_text,
            ':iid': impl_id,
            ':t': now,
        },
    )

    # Notify via SNS
    sns.publish(
        TopicArn=TOPIC,
        Subject=f'Implementation ready for task {task_id}',
        Message=json.dumps({
            'taskId': task_id,
            'implId': impl_id,
            'channel': channel,
            'diffText': diff_text,
            'event': 'ImplReady',
        }),
    )

    logger.info('Implementation complete for task %s', task_id)
    return {
        **event,
        'status': 'IMPL_READY',
        'implId': impl_id,
        'diffText': diff_text,
        'implementedAt': now,
    }
