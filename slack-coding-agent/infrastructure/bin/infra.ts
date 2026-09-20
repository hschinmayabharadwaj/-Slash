#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { NetworkStack } from '../lib/network-stack';
import { DatabaseStack } from '../lib/database-stack';
import { MessagingStack } from '../lib/messaging-stack';
import { ComputeStack } from '../lib/compute-stack';
import { ApiStack } from '../lib/api-stack';
import { MonitoringStack } from '../lib/monitoring-stack';
import { AIStack } from '../lib/ai-stack';

const app = new cdk.App();

const env: cdk.Environment = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION || 'us-east-2',
};

// ── Layer 1: Network ─────────────────────────────────────────
const networkStack = new NetworkStack(app, 'SlackAgentNetwork', {
  env,
  description: 'VPC and networking for Slack Coding Agent',
  tags: { Project: 'slack-coding-agent', Layer: 'Network' },
});

// ── Layer 2: Storage & Data ──────────────────────────────────
const databaseStack = new DatabaseStack(app, 'SlackAgentDatabase', {
  env,
  vpc: networkStack.vpc,
  dbSecurityGroup: networkStack.dbSecurityGroup,
  description: 'Aurora PostgreSQL, DynamoDB, S3 for Slack Coding Agent',
  tags: { Project: 'slack-coding-agent', Layer: 'Database' },
});
databaseStack.addDependency(networkStack);

// ── Layer 2: Messaging ───────────────────────────────────────
const messagingStack = new MessagingStack(app, 'SlackAgentMessaging', {
  env,
  description: 'SQS, SNS, EventBridge for Slack Coding Agent',
  tags: { Project: 'slack-coding-agent', Layer: 'Messaging' },
});

// ── Layer 3: Compute ─────────────────────────────────────────
const computeStack = new ComputeStack(app, 'SlackAgentCompute', {
  env,
  vpc: networkStack.vpc,
  botSecurityGroup: networkStack.botSecurityGroup,
  dbSecret: databaseStack.dbSecret,
  taskTable: databaseStack.taskTable,
  auditTable: databaseStack.auditTable,
  artifactsBucket: databaseStack.artifactsBucket,
  taskQueue: messagingStack.taskQueue,
  eventBus: messagingStack.eventBus,
  description: 'ECS Fargate, EC2, App Runner, Lightsail for Slack Coding Agent',
  tags: { Project: 'slack-coding-agent', Layer: 'Compute' },
});
computeStack.addDependency(databaseStack);
computeStack.addDependency(messagingStack);

// ── Layer 4: API ─────────────────────────────────────────────
const apiStack = new ApiStack(app, 'SlackAgentApi', {
  env,
  taskTable: databaseStack.taskTable,
  auditTable: databaseStack.auditTable,
  artifactsBucket: databaseStack.artifactsBucket,
  taskQueue: messagingStack.taskQueue,
  notificationsTopic: messagingStack.notificationsTopic,
  alertsTopic: messagingStack.alertsTopic,
  eventBus: messagingStack.eventBus,
  description: 'API Gateway, Lambda, Step Functions for Slack Coding Agent',
  tags: { Project: 'slack-coding-agent', Layer: 'API' },
});
apiStack.addDependency(databaseStack);
apiStack.addDependency(messagingStack);

// ── Layer 5 (removed): no us-east-1 resources. The demo dashboard is served
// from the App Runner service in SlackAgentCompute (us-east-2).

// ── Layer 6: Monitoring ──────────────────────────────────────
const monitoringStack = new MonitoringStack(app, 'SlackAgentMonitoring', {
  env,
  cluster: computeStack.cluster,
  taskQueue: messagingStack.taskQueue,
  taskDlq: messagingStack.taskDlq,
  alertsTopic: messagingStack.alertsTopic,
  api: apiStack.api,
  taskWorkflow: apiStack.taskWorkflow,
  description: 'CloudWatch dashboards, alarms, X-Ray for Slack Coding Agent',
  tags: { Project: 'slack-coding-agent', Layer: 'Monitoring' },
});
monitoringStack.addDependency(computeStack);
monitoringStack.addDependency(apiStack);

// ── Layer 7: AI / SageMaker ──────────────────────────────────
const aiStack = new AIStack(app, 'SlackAgentAI', {
  env,
  vpc: networkStack.vpc,
  artifactsBucket: databaseStack.artifactsBucket,
  description: 'SageMaker for AI model monitoring for Slack Coding Agent',
  tags: { Project: 'slack-coding-agent', Layer: 'AI' },
});
aiStack.addDependency(networkStack);
aiStack.addDependency(databaseStack);

// ── Dashboards ────────────────────────────────────────────────
// The dashboard SPA + API is served in-process by the site Lambda behind the
// HTTP API in SlackAgentApi (https). No CloudFront / S3 website / App Runner /
// dedicated Fargate service. See `ApiStack.DashboardUrl` output.

app.synth();
