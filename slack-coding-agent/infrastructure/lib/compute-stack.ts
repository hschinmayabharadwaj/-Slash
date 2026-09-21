import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as autoscaling from 'aws-cdk-lib/aws-applicationautoscaling';
import * as events from 'aws-cdk-lib/aws-events';
import { Construct } from 'constructs';

export interface ComputeStackProps extends cdk.StackProps {
  vpc: ec2.Vpc;
  botSecurityGroup: ec2.SecurityGroup;
  dbSecret: secretsmanager.ISecret;
  taskTable: dynamodb.Table;
  auditTable: dynamodb.Table;
  artifactsBucket: s3.Bucket;
  taskQueue: sqs.Queue;
  eventBus: events.EventBus;
}

export class ComputeStack extends cdk.Stack {
  public readonly cluster: ecs.Cluster;
  public readonly botRepository: ecr.Repository;
  public readonly sandboxRepository: ecr.Repository;

  constructor(scope: Construct, id: string, props: ComputeStackProps) {
    super(scope, id, props);

    // ── ECR Repositories ──────────────────────────────────────
    this.botRepository = new ecr.Repository(this, 'BotRepo', {
      repositoryName: 'slack-coding-agent',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteImages: true,
      lifecycleRules: [{ maxImageCount: 10 }],
      imageScanOnPush: true,
    });

    this.sandboxRepository = new ecr.Repository(this, 'SandboxRepo', {
      repositoryName: 'slack-agent-sandbox',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteImages: true,
      lifecycleRules: [{ maxImageCount: 5 }],
    });

    // ── ECS Cluster ───────────────────────────────────────────
    this.cluster = new ecs.Cluster(this, 'SlackAgentCluster', {
      clusterName: 'SlackAgentCluster',
      vpc: props.vpc,
      containerInsights: true,
      enableFargateCapacityProviders: true,
    });

    // ── IAM Task Role ─────────────────────────────────────────
    const taskRole = new iam.Role(this, 'EcsTaskRole', {
      roleName: 'slack-agent-ecs-task-role',
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore'),
      ],
    });

    // Grant access to AWS services
    props.taskTable.grantReadWriteData(taskRole);
    props.auditTable.grantReadWriteData(taskRole);
    props.artifactsBucket.grantReadWrite(taskRole);
    props.taskQueue.grantConsumeMessages(taskRole);
    props.taskQueue.grantSendMessages(taskRole);
    props.dbSecret.grantRead(taskRole);
    props.eventBus.grantPutEventsTo(taskRole);

    taskRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'cloudwatch:PutMetricData',
        'xray:PutTraceSegments',
        'xray:PutTelemetryRecords',
        'secretsmanager:GetSecretValue',
        'sns:Publish',
        'states:StartExecution',
        'states:SendTaskSuccess',
        'states:SendTaskFailure',
      ],
      resources: ['*'],
    }));

    // Worker needs to launch/stop one-shot Fargate sandbox tasks
    taskRole.addToPolicy(new iam.PolicyStatement({
      actions: ['ecs:RunTask', 'ecs:StopTask', 'ecs:DescribeTasks', 'iam:PassRole'],
      resources: [
        '*', // PassRole resource ARNs (sandbox roles) are account-scoped
      ],
    }));

    // Kill-switch + model-backend read (SSM parameters checked by the worker)
    taskRole.addToPolicy(new iam.PolicyStatement({
      actions: ['ssm:GetParameter'],
      resources: [
        `arn:aws:ssm:${this.region}:${this.account}:parameter/slack-agent/kill-switch`,
        `arn:aws:ssm:${this.region}:${this.account}:parameter/slack-agent/model-backend`,
      ],
    }));

    // ── Shared Secrets ────────────────────────────────────────
    // These secrets must be pre-created in Secrets Manager before deploying
    const slackBotToken = secretsmanager.Secret.fromSecretNameV2(
      this, 'SlackBotToken', 'slack-agent/SLACK_BOT_TOKEN'
    );
    const slackAppToken = secretsmanager.Secret.fromSecretNameV2(
      this, 'SlackAppToken', 'slack-agent/SLACK_APP_TOKEN'
    );
    const slackSigningSecret = secretsmanager.Secret.fromSecretNameV2(
      this, 'SlackSigningSecret', 'slack-agent/SLACK_SIGNING_SECRET'
    );
    const githubAppId = secretsmanager.Secret.fromSecretNameV2(
      this, 'GithubAppId', 'slack-agent/GITHUB_APP_ID'
    );
    const githubPrivateKey = secretsmanager.Secret.fromSecretNameV2(
      this, 'GithubPrivateKey', 'slack-agent/GITHUB_PRIVATE_KEY'
    );
    const githubInstallationId = secretsmanager.Secret.fromSecretNameV2(
      this, 'GithubInstallationId', 'slack-agent/GITHUB_INSTALLATION_ID'
    );
    const geminiApiKey = secretsmanager.Secret.fromSecretNameV2(
      this, 'GeminiApiKey', 'slack-agent/GEMINI_API_KEY'
    );
    // Optional — only used when MODEL_BACKEND=anthropic. Create it with:
    //   aws secretsmanager create-secret --name slack-agent/ANTHROPIC_API_KEY \
    //     --secret-string 'sk-ant-...' --region <region>
    // Then deploy with INCLUDE_ANTHROPIC_API_KEY=true so the container can
    // reference it. Kept OFF by default so bedrock/sim deploys need no key.
    let anthropicSecretEnv: Record<string, ecs.Secret> = {};
    if (process.env.INCLUDE_ANTHROPIC_API_KEY === 'true') {
      const anthropicApiKey = secretsmanager.Secret.fromSecretNameV2(
        this, 'AnthropicApiKey', 'slack-agent/ANTHROPIC_API_KEY'
      );
      anthropicSecretEnv = { ANTHROPIC_API_KEY: ecs.Secret.fromSecretsManager(anthropicApiKey) };
    }

    // ── Common Environment Variables ──────────────────────────
    const commonEnvironment: Record<string, string> = {
      AWS_REGION: this.region,
      DYNAMODB_TABLE: props.taskTable.tableName,
      SQS_QUEUE_URL: props.taskQueue.queueUrl,
      S3_BUCKET: props.artifactsBucket.bucketName,
      EVENTBRIDGE_BUS: props.eventBus.eventBusName,
      DB_NAME: 'slackagent',
      DB_PORT: '5432',
    };

    const commonSecrets: Record<string, ecs.Secret> = {
      SLACK_BOT_TOKEN: ecs.Secret.fromSecretsManager(slackBotToken),
      SLACK_APP_TOKEN: ecs.Secret.fromSecretsManager(slackAppToken),
      SLACK_SIGNING_SECRET: ecs.Secret.fromSecretsManager(slackSigningSecret),
      GITHUB_APP_ID: ecs.Secret.fromSecretsManager(githubAppId),
      GITHUB_PRIVATE_KEY: ecs.Secret.fromSecretsManager(githubPrivateKey),
      GITHUB_INSTALLATION_ID: ecs.Secret.fromSecretsManager(githubInstallationId),
      GEMINI_API_KEY: ecs.Secret.fromSecretsManager(geminiApiKey),
      ...anthropicSecretEnv,
      DB_PASSWORD: ecs.Secret.fromSecretsManager(props.dbSecret, 'password'),
      DB_HOST: ecs.Secret.fromSecretsManager(props.dbSecret, 'host'),
    };

    // ── Bot Task Definition (Slack Socket Mode listener) ──────
    const botLogGroup = new logs.LogGroup(this, 'BotLogGroup', {
      logGroupName: '/ecs/slack-agent-bot',
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const botTaskDef = new ecs.FargateTaskDefinition(this, 'BotTaskDef', {
      family: 'slack-agent-bot',
      cpu: 512,   // 0.5 vCPU — hard budget: bot + 1 worker + 2 sandboxes ≤ 3.0 vCPU
      memoryLimitMiB: 1024,
      taskRole,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    botTaskDef.addContainer('bot', {
      containerName: 'bot',
      image: ecs.ContainerImage.fromEcrRepository(this.botRepository, 'latest'),
      command: ['python', '-m', 'slackagent.main'],
      environment: commonEnvironment,
      secrets: commonSecrets,
      logging: ecs.LogDrivers.awsLogs({
        logGroup: botLogGroup,
        streamPrefix: 'bot',
      }),
      healthCheck: {
        command: ['CMD-SHELL', 'python -c "import slackagent" || exit 1'],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(60),
      },
    });

    // ── Worker Task Definition (processes tasks from SQS) ─────
    const workerLogGroup = new logs.LogGroup(this, 'WorkerLogGroup', {
      logGroupName: '/ecs/slack-agent-worker',
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ── One-shot Sandbox: IAM roles + task definition ────────
    // The sandbox task role is deliberately minimal: it can read/write its own
    // S3 prefix and invoke Bedrock (Claude Code auths via the task role with
    // CLAUDE_CODE_USE_BEDROCK=1). No GitHub token / provider key is provisioned.
    const sandboxTaskRole = new iam.Role(this, 'SandboxTaskRole', {
      roleName: 'slack-agent-sandbox-task-role',
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
    });
    sandboxTaskRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject', 's3:PutObject', 's3:ListBucket'],
      resources: [
        props.artifactsBucket.bucketArn,
        `${props.artifactsBucket.bucketArn}/sandbox*`,
      ],
    }));
    sandboxTaskRole.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel'],
      resources: [
        `arn:aws:bedrock:${this.region}:${this.account}:foundation-model/*`,
        `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/*`,
      ],
    }));

    const sandboxExecutionRole = new iam.Role(this, 'SandboxExecutionRole', {
      roleName: 'slack-agent-sandbox-execution-role',
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          'service-role/AmazonECSTaskExecutionRolePolicy'
        ),
      ],
    });

    const sandboxLogGroup = new logs.LogGroup(this, 'SandboxLogGroup', {
      logGroupName: '/ecs/slack-agent-sandbox',
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const sandboxTaskDef = new ecs.FargateTaskDefinition(this, 'SandboxTaskDef', {
      family: 'slack-agent-sandbox',
      cpu: 1024,        // 1.0 vCPU (max). Plan runs override to 0.5 vCPU at runtime.
      memoryLimitMiB: 2048,
      taskRole: sandboxTaskRole,
      executionRole: sandboxExecutionRole,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });
    const sandboxContainer = sandboxTaskDef.addContainer('sandbox', {
      containerName: 'sandbox',
      image: ecs.ContainerImage.fromEcrRepository(this.sandboxRepository, 'latest'),
      command: ['python', '-m', 'slackagent.fargate_entry', 'checks', ':'],
      logging: ecs.LogDrivers.awsLogs({
        logGroup: sandboxLogGroup,
        streamPrefix: 'sandbox',
      }),
      // No secrets, no API keys — network access only via the egress SG (443)
    });
    sandboxContainer.addUlimits({ name: ecs.UlimitName.CORE, softLimit: 0, hardLimit: 0 });

    const workerTaskDef = new ecs.FargateTaskDefinition(this, 'WorkerTaskDef', {
      family: 'slack-agent-worker',
      cpu: 512,    // 0.5 vCPU — hard budget: bot + 1 worker + 2 sandboxes ≤ 3.0 vCPU
      memoryLimitMiB: 1024,
      taskRole,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    workerTaskDef.addContainer('worker', {
      containerName: 'worker',
      image: ecs.ContainerImage.fromEcrRepository(this.botRepository, 'latest'),
      command: ['python', '-m', 'slackagent.worker_aws_v2'],
      environment: {
        ...commonEnvironment,
        WORKER_MODE: 'true',
        SQS_MODE: 'true',
        SLACKAGENT_TASKS_TABLE: props.taskTable.tableName,
        SLACKAGENT_AUDIT_TABLE: props.auditTable.tableName,
        AUDIT_TABLE: props.auditTable.tableName,
        SANDBOX_BUCKET: props.artifactsBucket.bucketName,
        SANDBOX_CLUSTER: this.cluster.clusterName,
        SANDBOX_TASK_DEFINITION: sandboxTaskDef.family,
        SANDBOX_CONTAINER_NAME: 'sandbox',
        SANDBOX_SUBNETS: cdk.Fn.join(
          ',',
          props.vpc.publicSubnets.map((s) => s.subnetId)
        ),
        SANDBOX_SECURITY_GROUPS: props.botSecurityGroup.securityGroupId,
        SANDBOX_ASSIGN_PUBLIC_IP: 'ENABLED',
        KILL_SWITCH_PARAM: '/slack-agent/kill-switch',
        MODEL_BACKEND_PARAM: '/slack-agent/model-backend', // resolved via SSM at runtime
        SANDBOX_IMAGE: this.sandboxRepository.repositoryUri + ':latest',
        // ── Sandbox vCPU budget (≤3.0 total across bot/worker/sandboxes) ──
        // bot 0.5 + worker 0.5 = 1.0 baseline; at most 2 sandboxes at 1.0 each.
        MAX_CONCURRENT_SANDBOXES: '2',
        SANDBOX_PLAN_CPU: '512',      // plan sandbox = 0.5 vCPU
        SANDBOX_IMPLEMENT_CPU: '1024', // implement sandbox = 1.0 vCPU
        SANDBOX_VCPU_BUDGET: '3.0',
        SANDBOX_VCPU_BASELINE: '1.0',
        GITHUB_OWNER: '',
      },
      secrets: commonSecrets,
      logging: ecs.LogDrivers.awsLogs({
        logGroup: workerLogGroup,
        streamPrefix: 'worker',
      }),
      linuxParameters: new ecs.LinuxParameters(this, 'WorkerLinuxParams', {
        initProcessEnabled: true,
      }),
    });

    // ── Fargate Service: Bot ──────────────────────────────────
    const botService = new ecs.FargateService(this, 'BotService', {
      serviceName: 'slack-agent-bot',
      cluster: this.cluster,
      taskDefinition: botTaskDef,
      desiredCount: 1,
      assignPublicIp: true,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      securityGroups: [props.botSecurityGroup],
      minHealthyPercent: 0,
      maxHealthyPercent: 200, // AZ rebalancing requires maximumPercent > 100
      circuitBreaker: { rollback: true },
      capacityProviderStrategies: [
        { capacityProvider: 'FARGATE', weight: 1 },
        { capacityProvider: 'FARGATE_SPOT', weight: 2 }, // Cost savings
      ],
    });

    // ── Fargate Service: Worker ───────────────────────────────
    const workerService = new ecs.FargateService(this, 'WorkerService', {
      serviceName: 'slack-agent-worker',
      cluster: this.cluster,
      taskDefinition: workerTaskDef,
      desiredCount: 1,
      assignPublicIp: true, // cheap demo: public subnets, no NAT
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      securityGroups: [props.botSecurityGroup],
      minHealthyPercent: 0,
      maxHealthyPercent: 200, // AZ rebalancing requires maximumPercent > 100
      circuitBreaker: { rollback: true },
    });

    // Keep the worker service at a single live task; the task queue is processed
    // shortly after the service starts and the autoscaling target can fail during
    // stack updates when legacy zero-count services exist. A fixed count is more
    // reliable for this demo deployment.

    // ── Dashboard ────────────────────────────────────────────
    // Served in-process by the site Lambda behind the HTTP API (SlackAgentApi)
    // — no Fargate service, no ECR image, no App Runner, no CloudFront/S3.

    // ── EC2 Dev Instance ──────────────────────────────────────
    const devRole = new iam.Role(this, 'DevInstanceRole', {
      assumedBy: new iam.ServicePrincipal('ec2.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore'),
        iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonEC2ContainerRegistryReadOnly'),
      ],
    });

    const devInstance = new ec2.Instance(this, 'DevInstance', {
      instanceName: 'slack-agent-dev',
      instanceType: ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MEDIUM),
      machineImage: ec2.MachineImage.latestAmazonLinux2023(),
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      role: devRole,
      userData: ec2.UserData.forLinux(),
    });
    devInstance.userData.addCommands(
      '#!/bin/bash',
      'set -e',
      'dnf update -y',
      'dnf install -y docker git python3.11 python3.11-pip',
      'systemctl enable docker && systemctl start docker',
      'usermod -aG docker ec2-user',
      'pip3.11 install awscli',
      `aws ecr get-login-password --region ${this.region} | docker login --username AWS --password-stdin ${this.account}.dkr.ecr.${this.region}.amazonaws.com`,
      'echo "Dev instance ready" > /home/ec2-user/READY'
    );

    // ── Lightsail (Dev/Fallback) ──────────────────────────────
    new cdk.CfnResource(this, 'LightsailInstance', {
      type: 'AWS::Lightsail::Instance',
      properties: {
        InstanceName: 'slack-agent-lightsail-dev',
        AvailabilityZone: `${this.region}a`,
        BlueprintId: 'amazon_linux_2023',
        BundleId: 'nano_2_0',
        UserData: [
          '#!/bin/bash',
          'dnf update -y',
          'dnf install -y docker git python3.11',
          'systemctl enable docker && systemctl start docker',
          '# Lightsail fallback / dev environment for Slack Coding Agent',
        ].join('\n'),
        Tags: [
          { Key: 'Project', Value: 'slack-coding-agent' },
          { Key: 'Role', Value: 'DevFallback' },
        ],
      },
    });

    // ── Outputs ───────────────────────────────────────────────
    new cdk.CfnOutput(this, 'ClusterArn', {
      value: this.cluster.clusterArn,
      exportName: 'SlackAgentClusterArn',
    });

    new cdk.CfnOutput(this, 'BotRepoUri', {
      value: this.botRepository.repositoryUri,
      exportName: 'SlackAgentBotRepoUri',
    });

    new cdk.CfnOutput(this, 'SandboxRepoUri', {
      value: this.sandboxRepository.repositoryUri,
      exportName: 'SlackAgentSandboxRepoUri',
    });

    new cdk.CfnOutput(this, 'DevInstanceId', {
      value: devInstance.instanceId,
      exportName: 'SlackAgentDevInstanceId',
    });
  }
}
