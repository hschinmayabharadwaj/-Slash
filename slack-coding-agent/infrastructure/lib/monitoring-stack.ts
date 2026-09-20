import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cloudwatch_actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as synthetics from 'aws-cdk-lib/aws-synthetics';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as budgets from 'aws-cdk-lib/aws-budgets';
import { Construct } from 'constructs';

export interface MonitoringStackProps extends cdk.StackProps {
  cluster: ecs.Cluster;
  taskQueue: sqs.Queue;
  taskDlq: sqs.Queue;
  alertsTopic: sns.Topic;
  api: apigateway.RestApi;
  taskWorkflow: sfn.StateMachine;
}

export class MonitoringStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: MonitoringStackProps) {
    super(scope, id, props);

    const alarmAction = new cloudwatch_actions.SnsAction(props.alertsTopic);

    // ── Log Groups ────────────────────────────────────────────
    // Note: Log groups (/slack-agent/api, /ecs/slack-agent-*, etc.) are already
    // created and managed by their respective stacks (SlackAgentApi, SlackAgentCompute).

    // ── Alarms ────────────────────────────────────────────────

    // SQS queue depth
    const queueDepthAlarm = new cloudwatch.Alarm(this, 'QueueDepthAlarm', {
      alarmName: 'slack-agent-queue-depth-high',
      alarmDescription: 'Task queue depth exceeds 100 - workers may be overwhelmed',
      metric: props.taskQueue.metricApproximateNumberOfMessagesVisible({
        period: cdk.Duration.minutes(1),
      }),
      threshold: 100,
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    queueDepthAlarm.addAlarmAction(alarmAction);

    // DLQ - any message is a critical alert
    const dlqAlarm = new cloudwatch.Alarm(this, 'DlqAlarm', {
      alarmName: 'slack-agent-dlq-messages-CRITICAL',
      alarmDescription: 'Tasks are landing in DLQ - investigate immediately',
      metric: props.taskDlq.metricApproximateNumberOfMessagesVisible({
        period: cdk.Duration.minutes(1),
      }),
      threshold: 0,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    dlqAlarm.addAlarmAction(alarmAction);

    // API 5xx errors
    const api5xxAlarm = new cloudwatch.Alarm(this, 'Api5xxAlarm', {
      alarmName: 'slack-agent-api-5xx-high',
      alarmDescription: 'API Gateway 5xx error rate exceeded 1%',
      metric: props.api.metricServerError({
        period: cdk.Duration.minutes(5),
        statistic: 'sum',
      }),
      threshold: 10,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    api5xxAlarm.addAlarmAction(alarmAction);

    // Step Functions failures
    const sfnFailureAlarm = new cloudwatch.Alarm(this, 'SfnFailureAlarm', {
      alarmName: 'slack-agent-workflow-failures',
      alarmDescription: 'Step Functions workflow executions are failing',
      metric: props.taskWorkflow.metricFailed({
        period: cdk.Duration.minutes(5),
        statistic: 'sum',
      }),
      threshold: 5,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    sfnFailureAlarm.addAlarmAction(alarmAction);

    // SQS message age (tasks stuck for > 1 hour)
    const messageAgeAlarm = new cloudwatch.Alarm(this, 'MessageAgeAlarm', {
      alarmName: 'slack-agent-message-age-high',
      alarmDescription: 'Tasks have been waiting over 1 hour - workers may be down',
      metric: props.taskQueue.metricApproximateAgeOfOldestMessage({
        period: cdk.Duration.minutes(5),
        statistic: 'maximum',
      }),
      threshold: 3600,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    messageAgeAlarm.addAlarmAction(alarmAction);

    // ── CloudWatch Dashboard ──────────────────────────────────
    const dashboard = new cloudwatch.Dashboard(this, 'OperationsDashboard', {
      dashboardName: 'SlackAgentOperations',
      periodOverride: cloudwatch.PeriodOverride.AUTO,
    });

    // Row 1: Title
    dashboard.addWidgets(
      new cloudwatch.TextWidget({
        markdown: `# 🤖 Slack Coding Agent — Operations Dashboard
**Live monitoring** of bot activity, task pipeline, and infrastructure health.
Last updated: ${new Date().toISOString()}`,
        width: 24,
        height: 2,
      })
    );

    // Row 2: Task Pipeline — key metrics
    dashboard.addWidgets(
      new cloudwatch.SingleValueWidget({
        title: 'Tasks in Queue',
        metrics: [props.taskQueue.metricApproximateNumberOfMessagesVisible()],
        width: 6,
        height: 3,
      }),
      new cloudwatch.SingleValueWidget({
        title: 'Tasks Processing',
        metrics: [props.taskQueue.metricApproximateNumberOfMessagesNotVisible()],
        width: 6,
        height: 3,
      }),
      new cloudwatch.SingleValueWidget({
        title: 'DLQ (Failed Tasks)',
        metrics: [props.taskDlq.metricApproximateNumberOfMessagesVisible()],
        width: 6,
        height: 3,
      }),
      new cloudwatch.AlarmStatusWidget({
        title: 'Alarm Status',
        alarms: [queueDepthAlarm, dlqAlarm, api5xxAlarm, sfnFailureAlarm],
        width: 6,
        height: 3,
      })
    );

    // Row 3: SQS Metrics
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'SQS — Task Queue Depth (24h)',
        left: [
          props.taskQueue.metricApproximateNumberOfMessagesVisible({
            label: 'Waiting',
            color: '#ff9900',
          }),
          props.taskQueue.metricApproximateNumberOfMessagesNotVisible({
            label: 'Processing',
            color: '#1f77b4',
          }),
          props.taskDlq.metricApproximateNumberOfMessagesVisible({
            label: 'DLQ (failed)',
            color: '#d62728',
          }),
        ],
        width: 12,
        height: 6,
        period: cdk.Duration.minutes(5),
      }),
      new cloudwatch.GraphWidget({
        title: 'SQS — Message Age (oldest task waiting)',
        left: [
          props.taskQueue.metricApproximateAgeOfOldestMessage({
            label: 'Age (seconds)',
            color: '#ff7f0e',
          }),
        ],
        width: 12,
        height: 6,
        period: cdk.Duration.minutes(5),
      })
    );

    // Row 4: API Gateway
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'API Gateway — Request Count & Errors',
        left: [
          props.api.metricCount({ label: 'Requests', color: '#2ca02c' }),
        ],
        right: [
          props.api.metricClientError({ label: '4xx Errors', color: '#ff7f0e' }),
          props.api.metricServerError({ label: '5xx Errors', color: '#d62728' }),
        ],
        width: 12,
        height: 6,
        period: cdk.Duration.minutes(5),
      }),
      new cloudwatch.GraphWidget({
        title: 'API Gateway — Latency',
        left: [
          props.api.metricLatency({ statistic: 'p50', label: 'p50', color: '#2ca02c' }),
          props.api.metricLatency({ statistic: 'p95', label: 'p95', color: '#ff7f0e' }),
          props.api.metricLatency({ statistic: 'p99', label: 'p99', color: '#d62728' }),
        ],
        width: 12,
        height: 6,
        period: cdk.Duration.minutes(5),
      })
    );

    // Row 5: Step Functions
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Step Functions — Workflow Executions',
        left: [
          props.taskWorkflow.metricStarted({ label: 'Started', color: '#1f77b4' }),
          props.taskWorkflow.metricSucceeded({ label: 'Succeeded', color: '#2ca02c' }),
          props.taskWorkflow.metricFailed({ label: 'Failed', color: '#d62728' }),
          props.taskWorkflow.metricTimedOut({ label: 'Timed Out', color: '#ff7f0e' }),
        ],
        width: 12,
        height: 6,
        period: cdk.Duration.hours(1),
      }),
      new cloudwatch.GraphWidget({
        title: 'Step Functions — Execution Duration',
        left: [
          props.taskWorkflow.metricTime({ statistic: 'p50', label: 'p50' }),
          props.taskWorkflow.metricTime({ statistic: 'p95', label: 'p95' }),
        ],
        width: 12,
        height: 6,
        period: cdk.Duration.hours(1),
      })
    );

    // Row 6: ECS
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'ECS Cluster — CPU Utilization',
        left: [
          new cloudwatch.Metric({
            namespace: 'AWS/ECS',
            metricName: 'CPUUtilization',
            dimensionsMap: { ClusterName: props.cluster.clusterName },
            statistic: 'Average',
            label: 'CPU %',
            color: '#1f77b4',
          }),
        ],
        width: 8,
        height: 6,
        period: cdk.Duration.minutes(5),
      }),
      new cloudwatch.GraphWidget({
        title: 'ECS Cluster — Memory Utilization',
        left: [
          new cloudwatch.Metric({
            namespace: 'AWS/ECS',
            metricName: 'MemoryUtilization',
            dimensionsMap: { ClusterName: props.cluster.clusterName },
            statistic: 'Average',
            label: 'Memory %',
            color: '#ff7f0e',
          }),
        ],
        width: 8,
        height: 6,
        period: cdk.Duration.minutes(5),
      }),
      new cloudwatch.GraphWidget({
        title: 'Custom Metrics — Bot Activity',
        left: [
          new cloudwatch.Metric({
            namespace: 'SlackAgent/Operations',
            metricName: 'TasksCreated',
            statistic: 'Sum',
            label: 'Tasks Created',
            color: '#2ca02c',
          }),
          new cloudwatch.Metric({
            namespace: 'SlackAgent/Operations',
            metricName: 'TasksCompleted',
            statistic: 'Sum',
            label: 'Tasks Completed',
            color: '#1f77b4',
          }),
          new cloudwatch.Metric({
            namespace: 'SlackAgent/Operations',
            metricName: 'TasksFailed',
            statistic: 'Sum',
            label: 'Tasks Failed',
            color: '#d62728',
          }),
        ],
        width: 8,
        height: 6,
        period: cdk.Duration.hours(1),
      })
    );

    // ── CloudWatch Synthetics Canary ──────────────────────────
    const canaryBucket = new s3.Bucket(this, 'CanaryBucket', {
      bucketName: `slack-agent-canary-${this.account}-${this.region}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const canaryRole = new iam.Role(this, 'CanaryRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('CloudWatchSyntheticsFullAccess'),
      ],
    });
    canaryBucket.grantReadWrite(canaryRole);

    new synthetics.Canary(this, 'DashboardCanary', {
      canaryName: 'slack-agent-dashboard-canary',
      schedule: synthetics.Schedule.rate(cdk.Duration.minutes(5)),
      test: synthetics.Test.custom({
        code: synthetics.Code.fromInline(`
const synthetics = require('Synthetics');
const log = require('SyntheticsLogger');

const flowBuilderBlueprint = async function() {
  const url = process.env.TARGET_URL || 'https://example.cloudfront.net/health';
  let page = await synthetics.getPage();
  const response = await page.goto(url, {waitUntil: 'domcontentloaded', timeout: 30000});
  if (response.status() < 200 || response.status() > 299) {
    throw new Error('Failed to load dashboard: ' + response.status());
  }
  log.info('Dashboard is accessible: ' + response.status());
};

exports.handler = async () => {
  return await flowBuilderBlueprint();
};
        `),
        handler: 'index.handler',
      }),
      runtime: synthetics.Runtime.SYNTHETICS_NODEJS_PUPPETEER_10_0,
      environmentVariables: {
        TARGET_URL: 'https://example.cloudfront.net/health',
      },
      artifactsBucketLocation: { bucket: canaryBucket },
      role: canaryRole,
      startAfterCreation: true,
      successRetentionPeriod: cdk.Duration.days(7),
      failureRetentionPeriod: cdk.Duration.days(14),
    });

    // ── Cost budget: demo run must stay cheap ─────────────────
    new budgets.CfnBudget(this, 'DemoBudget', {
      budget: {
        budgetName: 'slack-agent-demo-monthly',
        budgetType: 'COST',
        timeUnit: 'MONTHLY',
        budgetLimit: { amount: 100, unit: 'USD' },
      },
      notificationsWithSubscribers: [
        {
          notification: {
            comparisonOperator: 'GREATER_THAN',
            threshold: 80,
            notificationType: 'ACTUAL',
          },
          subscribers: [{ subscriptionType: 'SNS', address: props.alertsTopic.topicArn }],
        },
      ],
    });

    // ── Outputs ───────────────────────────────────────────────
    new cdk.CfnOutput(this, 'DashboardUrl', {
      value: `https://console.aws.amazon.com/cloudwatch/home#dashboards:name=SlackAgentOperations`,
      description: 'CloudWatch Operations Dashboard URL',
    });
  }
}
