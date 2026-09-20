import * as cdk from 'aws-cdk-lib';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sns_subs from 'aws-cdk-lib/aws-sns-subscriptions';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import { Construct } from 'constructs';

export class MessagingStack extends cdk.Stack {
  public readonly taskQueue: sqs.Queue;
  public readonly taskDlq: sqs.Queue;
  public readonly notificationsTopic: sns.Topic;
  public readonly alertsTopic: sns.Topic;
  public readonly eventBus: events.EventBus;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ── Dead Letter Queue ─────────────────────────────────────
    this.taskDlq = new sqs.Queue(this, 'TaskDLQ', {
      queueName: 'slack-agent-task-dlq',
      retentionPeriod: cdk.Duration.days(14),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
    });

    // ── Main Task Queue ───────────────────────────────────────
    this.taskQueue = new sqs.Queue(this, 'TaskQueue', {
      queueName: 'slack-agent-task-queue',
      visibilityTimeout: cdk.Duration.seconds(900), // 15 min — matches worker timeout
      retentionPeriod: cdk.Duration.days(4),
      receiveMessageWaitTime: cdk.Duration.seconds(20), // Long polling
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      deadLetterQueue: {
        queue: this.taskDlq,
        maxReceiveCount: 3,
      },
    });

    // ── SNS Topics ────────────────────────────────────────────
    this.notificationsTopic = new sns.Topic(this, 'AgentNotifications', {
      topicName: 'slack-agent-notifications',
      displayName: 'Slack Agent Notifications',
    });

    this.alertsTopic = new sns.Topic(this, 'TaskAlerts', {
      topicName: 'slack-agent-alerts',
      displayName: 'Slack Agent Critical Alerts',
    });

    // Optional email alert subscription — update email before deploying
    const alertEmail = this.node.tryGetContext('alertEmail') as string;
    if (alertEmail) {
      this.alertsTopic.addSubscription(
        new sns_subs.EmailSubscription(alertEmail)
      );
    }

    // ── EventBridge Custom Bus ────────────────────────────────
    this.eventBus = new events.EventBus(this, 'SlackAgentBus', {
      eventBusName: 'SlackAgentBus',
    });

    // Enable archive for replay
    this.eventBus.archive('SlackAgentArchive', {
      archiveName: 'slack-agent-event-archive',
      description: 'Archive of all Slack Agent events',
      retention: cdk.Duration.days(30),
      eventPattern: { source: ['slack-agent'] },
    });

    // ── EventBridge Rules ─────────────────────────────────────
    // TaskCreated → Notifications topic
    new events.Rule(this, 'TaskCreatedRule', {
      ruleName: 'slack-agent-task-created',
      eventBus: this.eventBus,
      eventPattern: {
        source: ['slack-agent'],
        detailType: ['TaskCreated'],
      },
      targets: [new targets.SnsTopic(this.notificationsTopic)],
    });

    // TaskCompleted → Notifications topic
    new events.Rule(this, 'TaskCompletedRule', {
      ruleName: 'slack-agent-task-completed',
      eventBus: this.eventBus,
      eventPattern: {
        source: ['slack-agent'],
        detailType: ['TaskCompleted'],
      },
      targets: [new targets.SnsTopic(this.notificationsTopic)],
    });

    // TaskFailed → Alerts topic (critical)
    new events.Rule(this, 'TaskFailedRule', {
      ruleName: 'slack-agent-task-failed',
      eventBus: this.eventBus,
      eventPattern: {
        source: ['slack-agent'],
        detailType: ['TaskFailed'],
      },
      targets: [new targets.SnsTopic(this.alertsTopic)],
    });

    // Approved → re-enqueue in SQS for worker pickup
    new events.Rule(this, 'TaskApprovedRule', {
      ruleName: 'slack-agent-task-approved',
      eventBus: this.eventBus,
      eventPattern: {
        source: ['slack-agent'],
        detailType: ['TaskApproved'],
      },
      targets: [new targets.SqsQueue(this.taskQueue)],
    });

    // ── Outputs ───────────────────────────────────────────────
    new cdk.CfnOutput(this, 'TaskQueueUrl', {
      value: this.taskQueue.queueUrl,
      exportName: 'SlackAgentTaskQueueUrl',
    });

    new cdk.CfnOutput(this, 'TaskDlqUrl', {
      value: this.taskDlq.queueUrl,
      exportName: 'SlackAgentTaskDlqUrl',
    });

    new cdk.CfnOutput(this, 'NotificationsTopicArn', {
      value: this.notificationsTopic.topicArn,
      exportName: 'SlackAgentNotificationsArn',
    });

    new cdk.CfnOutput(this, 'AlertsTopicArn', {
      value: this.alertsTopic.topicArn,
      exportName: 'SlackAgentAlertsArn',
    });

    new cdk.CfnOutput(this, 'EventBusArn', {
      value: this.eventBus.eventBusArn,
      exportName: 'SlackAgentEventBusArn',
    });

    new cdk.CfnOutput(this, 'EventBusName', {
      value: this.eventBus.eventBusName,
      exportName: 'SlackAgentEventBusName',
    });
  }
}
