import * as cdk from 'aws-cdk-lib';
import * as sagemaker from 'aws-cdk-lib/aws-sagemaker';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import { Construct } from 'constructs';

export interface AIStackProps extends cdk.StackProps {
  artifactsBucket: s3.Bucket;
  vpc: ec2.Vpc;
}

export class AIStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: AIStackProps) {
    super(scope, id, props);

    // ── SageMaker Execution Role ──────────────────────────────
    const sagemakerRole = new iam.Role(this, 'SageMakerRole', {
      roleName: 'slack-agent-sagemaker-role',
      assumedBy: new iam.ServicePrincipal('sagemaker.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSageMakerFullAccess'),
        iam.ManagedPolicy.fromAwsManagedPolicyName('CloudWatchFullAccess'),
      ],
    });
    props.artifactsBucket.grantReadWrite(sagemakerRole);

    // ── SageMaker Notebook: AI Monitoring ─────────────────────
    const lifecycleConfig = new sagemaker.CfnNotebookInstanceLifecycleConfig(
      this, 'MonitorLifecycle', {
        notebookInstanceLifecycleConfigName: 'slack-agent-monitor-config',
        onCreate: [{
          content: cdk.Fn.base64([
            '#!/bin/bash',
            'set -e',
            'pip install --quiet boto3 pandas matplotlib seaborn plotly',
            'pip install --quiet google-generativeai transformers',
            'echo "Monitoring environment ready" > /home/ec2-user/SageMaker/READY.txt',
          ].join('\n')),
        }],
        onStart: [{
          content: cdk.Fn.base64([
            '#!/bin/bash',
            '# Pull latest monitoring notebooks from S3',
            `aws s3 sync s3://${props.artifactsBucket.bucketName}/notebooks/ /home/ec2-user/SageMaker/ || true`,
          ].join('\n')),
        }],
      }
    );

    const notebook = new sagemaker.CfnNotebookInstance(this, 'MonitorNotebook', {
      notebookInstanceName: 'slack-agent-ai-monitor',
      instanceType: 'ml.t3.medium',
      roleArn: sagemakerRole.roleArn,
      lifecycleConfigName: lifecycleConfig.notebookInstanceLifecycleConfigName,
      volumeSizeInGb: 20,
      tags: [
        { key: 'Project', value: 'SlackCodingAgent' },
        { key: 'Purpose', value: 'AIModelMonitoring' },
      ],
    });

    // ── SageMaker Studio Domain ───────────────────────────────
    // DISABLED: Account has SageMaker Domain quota of 0 in us-east-2.
    // Request a quota increase via Service Quotas console if needed:
    // https://console.aws.amazon.com/servicequotas/home/services/sagemaker/quotas
    //
    // new sagemaker.CfnDomain(this, 'StudioDomain', {
    //   domainName: 'slack-agent-studio',
    //   authMode: 'IAM',
    //   defaultUserSettings: {
    //     executionRole: sagemakerRole.roleArn,
    //     sharingSettings: {
    //       notebookOutputOption: 'Allowed',
    //     },
    //   },
    //   vpcId: props.vpc.vpcId,
    //   subnetIds: props.vpc.isolatedSubnets.map((s) => s.subnetId),
    // });

    // ── CloudWatch AI Metrics Dashboard ──────────────────────
    const aiDashboard = new cloudwatch.Dashboard(this, 'AiDashboard', {
      dashboardName: 'SlackAgentAIMetrics',
    });

    aiDashboard.addWidgets(
      new cloudwatch.TextWidget({
        markdown: `# 🧠 Slack Coding Agent — AI Metrics
Tracks Gemini API usage, token consumption, and task quality metrics.`,
        width: 24,
        height: 2,
      })
    );

    aiDashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Gemini API — Call Volume',
        left: [
          new cloudwatch.Metric({
            namespace: 'SlackAgent/AI',
            metricName: 'GeminiApiCalls',
            statistic: 'Sum',
            label: 'API Calls',
            color: '#4285f4',
          }),
          new cloudwatch.Metric({
            namespace: 'SlackAgent/AI',
            metricName: 'GeminiApiErrors',
            statistic: 'Sum',
            label: 'API Errors',
            color: '#ea4335',
          }),
        ],
        width: 12,
        height: 6,
        period: cdk.Duration.hours(1),
      }),
      new cloudwatch.GraphWidget({
        title: 'Gemini API — Token Usage',
        left: [
          new cloudwatch.Metric({
            namespace: 'SlackAgent/AI',
            metricName: 'PlanningTokensUsed',
            statistic: 'Sum',
            label: 'Planning Tokens',
            color: '#fbbc04',
          }),
          new cloudwatch.Metric({
            namespace: 'SlackAgent/AI',
            metricName: 'ImplementationTokensUsed',
            statistic: 'Sum',
            label: 'Impl Tokens',
            color: '#34a853',
          }),
        ],
        width: 12,
        height: 6,
        period: cdk.Duration.hours(1),
      })
    );

    aiDashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Phase Duration (seconds)',
        left: [
          new cloudwatch.Metric({
            namespace: 'SlackAgent/AI',
            metricName: 'PlanningDuration',
            statistic: 'Average',
            label: 'Planning Phase (avg)',
            color: '#fbbc04',
          }),
          new cloudwatch.Metric({
            namespace: 'SlackAgent/AI',
            metricName: 'ImplementationDuration',
            statistic: 'Average',
            label: 'Implementation Phase (avg)',
            color: '#34a853',
          }),
        ],
        width: 12,
        height: 6,
        period: cdk.Duration.hours(1),
      }),
      new cloudwatch.GraphWidget({
        title: 'Task Approval Rate',
        left: [
          new cloudwatch.Metric({
            namespace: 'SlackAgent/AI',
            metricName: 'PlansApproved',
            statistic: 'Sum',
            label: 'Plans Approved',
            color: '#2ca02c',
          }),
          new cloudwatch.Metric({
            namespace: 'SlackAgent/AI',
            metricName: 'PlansRejected',
            statistic: 'Sum',
            label: 'Plans Rejected',
            color: '#d62728',
          }),
          new cloudwatch.Metric({
            namespace: 'SlackAgent/AI',
            metricName: 'ImplsApproved',
            statistic: 'Sum',
            label: 'Impls Approved',
            color: '#1f77b4',
          }),
          new cloudwatch.Metric({
            namespace: 'SlackAgent/AI',
            metricName: 'ImplsRejected',
            statistic: 'Sum',
            label: 'Impls Rejected',
            color: '#ff7f0e',
          }),
        ],
        width: 12,
        height: 6,
        period: cdk.Duration.hours(1),
      })
    );

    // ── Outputs ───────────────────────────────────────────────
    new cdk.CfnOutput(this, 'NotebookUrl', {
      value: `https://${this.region}.console.aws.amazon.com/sagemaker/home#/notebook-instances/${notebook.notebookInstanceName}`,
      description: 'SageMaker AI Monitoring Notebook',
    });

    new cdk.CfnOutput(this, 'AiDashboardUrl', {
      value: `https://console.aws.amazon.com/cloudwatch/home#dashboards:name=SlackAgentAIMetrics`,
      description: 'AI Metrics CloudWatch Dashboard',
    });
  }
}
