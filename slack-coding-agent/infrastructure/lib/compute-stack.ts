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
import * as apprunner from 'aws-cdk-lib/aws-apprunner';
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
  public readonly dashboardRepository: ecr.Repository;
  public dashboardService: ecs.FargateService | undefined;

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

    this.dashboardRepository = new ecr.Repository(this, 'DashboardRepo', {
      repositoryName: 'slack-agent-dashboard',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteImages: true,
      lifecycleRules: [{ maxImageCount: 10 }],
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

    // Kill-switch read (SSM parameter checked by the worker on each task)
    taskRole.addToPolicy(new iam.PolicyStatement({
      actions: ['ssm:GetParameter'],
      resources: [`arn:aws:ssm:${this.region}:${this.account}:parameter/slack-agent/kill-switch`],
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
      cpu: 1024,   // 1 vCPU
      memoryLimitMiB: 2048,
      taskRole,
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
      cpu: 2048,        // 2 vCPU — headroom for claude-code + git + checks
      memoryLimitMiB: 8192,
      taskRole: sandboxTaskRole,
      executionRole: sandboxExecutionRole,
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
      cpu: 2048,   // 2 vCPU (runs Docker-in-Docker sandbox)
      memoryLimitMiB: 4096,
      taskRole,
    });

    workerTaskDef.addContainer('worker', {
      containerName: 'worker',
      image: ecs.ContainerImage.fromEcrRepository(this.botRepository, 'latest'),
      command: ['python', '-m', 'slackagent.worker_aws_v2'],
      environment: {
        ...commonEnvironment,
        WORKER_MODE: 'true',
        SQS_MODE: 'true',
        CLAUDE_CODE_USE_BEDROCK: '1',
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
        KILL_SWITCH_PARAM: `/slack-agent/kill-switch`,
        SANDBOX_IMAGE: this.sandboxRepository.repositoryUri + ':latest',
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
      desiredCount: 0,
      assignPublicIp: true,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      securityGroups: [props.botSecurityGroup],
      minHealthyPercent: 0,
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
      desiredCount: 0,
      assignPublicIp: true, // cheap demo: public subnets, no NAT
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      securityGroups: [props.botSecurityGroup],
      minHealthyPercent: 0,
      circuitBreaker: { rollback: true },
    });

    // Auto-scale workers based on SQS queue depth
    const workerScaling = workerService.autoScaleTaskCount({
      minCapacity: 0,
      maxCapacity: 10,
    });
    workerScaling.scaleOnMetric('ScaleOnQueueDepth', {
      metric: props.taskQueue.metricApproximateNumberOfMessagesVisible(),
      scalingSteps: [
        { upper: 0, change: -1 },
        { lower: 1, change: +1 },
        { lower: 5, change: +2 },
        { lower: 20, change: +4 },
      ],
    });

    // ── App Runner: Dashboard ─────────────────────────────────
    // DISABLED: App Runner is not enabled/subscribed in this account for us-east-2.
    // To re-enable, subscribe to App Runner in the AWS console, then uncomment below.
    //
    // const appRunnerRole = new iam.Role(this, 'AppRunnerAccessRole', {
    //   assumedBy: new iam.ServicePrincipal('build.apprunner.amazonaws.com'),
    //   managedPolicies: [
    //     iam.ManagedPolicy.fromAwsManagedPolicyName(
    //       'service-role/AWSAppRunnerServicePolicyForECRAccess'
    //     ),
    //   ],
    // });
    //
    // const appRunnerInstanceRole = new iam.Role(this, 'AppRunnerInstanceRole', {
    //   assumedBy: new iam.ServicePrincipal('tasks.apprunner.amazonaws.com'),
    // });
    // props.taskTable.grantReadData(appRunnerInstanceRole);
    //
    // const dashboardService = new apprunner.CfnService(this, 'DashboardAppRunner', {
    //   serviceName: 'slack-agent-dashboard',
    //   sourceConfiguration: {
    //     authenticationConfiguration: {
    //       accessRoleArn: appRunnerRole.roleArn,
    //     },
    //     imageRepository: {
    //       imageIdentifier: `${this.account}.dkr.ecr.${this.region}.amazonaws.com/slack-agent-dashboard:latest`,
    //       imageRepositoryType: 'ECR',
    //       imageConfiguration: {
    //         port: '80',
    //         runtimeEnvironmentVariables: [
    //           { name: 'DYNAMODB_TABLE', value: props.taskTable.tableName },
    //           { name: 'AWS_REGION', value: this.region },
    //         ],
    //       },
    //     },
    //     autoDeploymentsEnabled: true,
    //   },
    //   instanceConfiguration: {
    //     cpu: '1 vCPU',
    //     memory: '2 GB',
    //     instanceRoleArn: appRunnerInstanceRole.roleArn,
    //   },
    //   healthCheckConfiguration: {
    //     path: '/health',
    //     protocol: 'HTTP',
    //     interval: 20,
    //     timeout: 5,
    //     healthyThreshold: 1,
    //     unhealthyThreshold: 3,
    //   },
    //   autoScalingConfigurationArn: new apprunner.CfnAutoScalingConfiguration(
    //     this, 'DashboardScaling', {
    //       autoScalingConfigurationName: 'dashboard-scaling',
    //       minSize: 1,
    //       maxSize: 5,
    //       maxConcurrency: 100,
    //     }
    //   ).attrAutoScalingConfigurationArn,
    // });

    // ── Fargate Service: Dashboard (nginx SPA) ────────────────
    // App Runner and CloudFront are blocked until this account is verified,
    // so the React dashboard is served by a small public Fargate task
    // (0.25 vCPU / 512 MB) on port 80. All us-east-2.
    const dashboardSg = new ec2.SecurityGroup(this, 'DashboardSg', {
      vpc: props.vpc,
      securityGroupName: 'slack-agent-dashboard-sg',
      description: 'Public access to the demo dashboard',
      allowAllOutbound: true,
    });
    dashboardSg.addIngressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(80),
      'Allow HTTP on dashboard'
    );

    const dashboardTaskDef = new ecs.FargateTaskDefinition(this, 'DashboardTaskDef', {
      family: 'slack-agent-dashboard',
      cpu: 256,
      memoryLimitMiB: 512,
    });

    dashboardTaskDef.addContainer('dashboard', {
      containerName: 'dashboard',
      image: ecs.ContainerImage.fromEcrRepository(this.dashboardRepository, 'latest'),
      portMappings: [{ containerPort: 80 }],
    });

    const dashboardService = new ecs.FargateService(this, 'DashboardService', {
      serviceName: 'slack-agent-dashboard',
      cluster: this.cluster,
      taskDefinition: dashboardTaskDef,
      desiredCount: 1,
      assignPublicIp: true,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      securityGroups: [dashboardSg],
      minHealthyPercent: 0,
    });
    this.dashboardService = dashboardService;

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

    // App Runner output disabled (service commented out above)
    // new cdk.CfnOutput(this, 'AppRunnerServiceUrl', {
    //   value: cdk.Fn.sub('https://${Service}', {
    //     Service: dashboardService.attrServiceUrl,
    //   }),
    //   exportName: 'SlackAgentAppRunnerUrl',
    //   description: 'App Runner dashboard URL',
    // });

    new cdk.CfnOutput(this, 'DevInstanceId', {
      value: devInstance.instanceId,
      exportName: 'SlackAgentDevInstanceId',
    });

    new cdk.CfnOutput(this, 'DashboardTaskArn', {
      value: dashboardService.cluster.clusterName,
      exportName: 'SlackAgentDashboardCluster',
      description: 'Run `aws ecs list-tasks --cluster <this> --service-name slack-agent-dashboard` for the public IP',
    });
  }
}
