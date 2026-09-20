import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as events from 'aws-cdk-lib/aws-events';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as apigatewayv2 from 'aws-cdk-lib/aws-apigatewayv2';
import * as integrations from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as path from 'path';
import * as fs from 'fs';
import { Construct } from 'constructs';

export interface ApiStackProps extends cdk.StackProps {
  taskTable: dynamodb.Table;
  auditTable: dynamodb.Table;
  artifactsBucket: s3.Bucket;
  taskQueue: sqs.Queue;
  notificationsTopic: sns.Topic;
  alertsTopic: sns.Topic;
  eventBus: events.EventBus;
}

export class ApiStack extends cdk.Stack {
  public readonly api: apigateway.RestApi;
  public readonly taskWorkflow: sfn.StateMachine;
  public readonly taskProcessorFn: lambda.Function;
  public readonly webhookHandlerFn: lambda.Function;
  public readonly dashboardApiFn: lambda.Function;
  public readonly siteFn: lambda.Function;
  public readonly httpApi: apigatewayv2.HttpApi;

  constructor(scope: Construct, id: string, props: ApiStackProps) {
    super(scope, id, props);

    // ── Shared Lambda Config ──────────────────────────────────
    const lambdaEnv: Record<string, string> = {
      DYNAMODB_TABLE: props.taskTable.tableName,
      SLACKAGENT_TASKS_TABLE: props.taskTable.tableName,
      AUDIT_TABLE: props.auditTable.tableName,
      SLACKAGENT_AUDIT_TABLE: props.auditTable.tableName,
      SQS_QUEUE_URL: props.taskQueue.queueUrl,
      EVENTBRIDGE_BUS: props.eventBus.eventBusName,
      SNS_TOPIC_ARN: props.notificationsTopic.topicArn,
      ALERTS_TOPIC_ARN: props.alertsTopic.topicArn,
      S3_BUCKET: props.artifactsBucket.bucketName,
      KILL_SWITCH_PARAM: '/slack-agent/kill-switch',
      MODEL_BACKEND_PARAM: '/slack-agent/model-backend',
      MODEL_BACKEND: process.env.MODEL_BACKEND || 'bedrock',
      POWERTOOLS_SERVICE_NAME: 'slack-coding-agent',
      LOG_LEVEL: 'INFO',
    };

    const lambdaRole = new iam.Role(this, 'LambdaRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          'service-role/AWSLambdaBasicExecutionRole'
        ),
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          'AWSXRayDaemonWriteAccess'
        ),
      ],
    });

    // The dashboard lambda gets its own execution role. It is the only
    // function that starts the Step Functions workflow, and its
    // `states:StartExecution` grant must not live in the same role as the
    // workflow's task lambdas, or the CFN dependency graph cycles:
    // rolePolicy -> workflow -> rolePolicy(task lambda) -> lambda -> rolePolicy.
    const dashboardRole = new iam.Role(this, 'DashboardRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          'service-role/AWSLambdaBasicExecutionRole'
        ),
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          'AWSXRayDaemonWriteAccess'
        ),
      ],
    });

    const attachSharedGrants = (role: iam.Role): void => {
      props.taskTable.grantReadWriteData(role);
      props.auditTable.grantReadWriteData(role);
      props.taskQueue.grantSendMessages(role);
      props.eventBus.grantPutEventsTo(role);
      props.notificationsTopic.grantPublish(role);
      props.alertsTopic.grantPublish(role);
      props.artifactsBucket.grantRead(role);
      role.addToPolicy(new iam.PolicyStatement({
        actions: ['ssm:PutParameter', 'ssm:GetParameter'],
        resources: [
          `arn:aws:ssm:${this.region}:${this.account}:parameter/slack-agent/kill-switch`,
          `arn:aws:ssm:${this.region}:${this.account}:parameter/slack-agent/model-backend`,
        ],
      }));
    };

    attachSharedGrants(lambdaRole);
    attachSharedGrants(dashboardRole);

    // Kill-switch parameter (created in this stack; worker reads it each task)
    const killSwitchParam = new ssm.CfnParameter(this, 'KillSwitchParam', {
      name: '/slack-agent/kill-switch',
      type: 'String',
      value: '0',
      description: 'Slack Coding Agent kill switch: "1" halts the worker',
    });

    // Model backend switch: sim | anthropic | bedrock. The worker and the
    // dashboard read this parameter at runtime; MODEL_BACKEND env is a fallback.
    // Values are validated at runtime (invalid → bedrock), so no CFN constraint.
    const modelBackendParam = new ssm.CfnParameter(this, 'ModelBackendParam', {
      name: '/slack-agent/model-backend',
      type: 'String',
      value: process.env.MODEL_BACKEND || 'bedrock',
      description: 'Model backend: sim (no LLM), anthropic (API key via Secrets Manager), bedrock (task-role auth)',
    });

    // ── Lambda: Task Processor (approve/reject) ───────────────
    this.taskProcessorFn = new lambda.Function(this, 'TaskProcessor', {
      functionName: 'slack-agent-task-processor',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(
        path.join(__dirname, '../lambda/task_processor')
      ),
      memorySize: 512,
      timeout: cdk.Duration.seconds(30),
      environment: lambdaEnv,
      role: lambdaRole,
      tracing: lambda.Tracing.ACTIVE,
      logRetention: logs.RetentionDays.TWO_WEEKS,
    });

    // ── Lambda: Webhook Handler (Slack events) ────────────────
    this.webhookHandlerFn = new lambda.Function(this, 'WebhookHandler', {
      functionName: 'slack-agent-webhook-handler',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'webhook.handler',
      code: lambda.Code.fromAsset(
        path.join(__dirname, '../lambda/webhook_handler')
      ),
      memorySize: 256,
      timeout: cdk.Duration.seconds(10),
      environment: {
        ...lambdaEnv,
        SLACK_SIGNING_SECRET_NAME: 'slack-agent/SLACK_SIGNING_SECRET',
      },
      role: lambdaRole,
      tracing: lambda.Tracing.ACTIVE,
      logRetention: logs.RetentionDays.TWO_WEEKS,
    });

    this.webhookHandlerFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['secretsmanager:GetSecretValue'],
      resources: ['*'],
    }));

    // ── Lambda: Dashboard API (stats / task list) ─────────────
    this.dashboardApiFn = new lambda.Function(this, 'DashboardApi', {
      functionName: 'slack-agent-dashboard-api',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'dashboard.handler',
      code: lambda.Code.fromAsset(
        path.join(__dirname, '../lambda/dashboard_api')
      ),
      memorySize: 256,
      timeout: cdk.Duration.seconds(10),
      environment: lambdaEnv,
      role: dashboardRole,
      tracing: lambda.Tracing.ACTIVE,
      logRetention: logs.RetentionDays.TWO_WEEKS,
    });

    // ── Step Functions Workflow ───────────────────────────────
    // Individual step lambdas (reuse the processor function for simplicity)
    const validateTask = new tasks.LambdaInvoke(this, 'ValidateTask', {
      lambdaFunction: this.taskProcessorFn,
      payload: sfn.TaskInput.fromObject({
        action: 'validate',
        'taskId.$': '$.taskId',
      }),
      resultPath: '$.validation',
      retryOnServiceExceptions: true,
    });

    const planningPhase = new tasks.LambdaInvoke(this, 'PlanningPhase', {
      lambdaFunction: this.taskProcessorFn,
      payload: sfn.TaskInput.fromObject({
        action: 'plan',
        'taskId.$': '$.taskId',
      }),
      resultPath: '$.plan',
      heartbeat: cdk.Duration.minutes(30),
      taskTimeout: sfn.Timeout.duration(cdk.Duration.minutes(10)),
    });
    planningPhase.addRetry({ maxAttempts: 2, backoffRate: 2 });

    const awaitPlanApproval = new sfn.Wait(this, 'AwaitPlanApproval', {
      time: sfn.WaitTime.timestampPath('$.planApprovalDeadline'),
    });

    const checkPlanApproved = new tasks.DynamoGetItem(this, 'CheckPlanApproved', {
      table: props.taskTable,
      key: {
        taskId: tasks.DynamoAttributeValue.fromString(
          sfn.JsonPath.stringAt('$.taskId')
        ),
        timestamp: tasks.DynamoAttributeValue.fromString('LATEST'),
      },
      resultPath: '$.taskRecord',
    });

    const isPlanApproved = new sfn.Choice(this, 'IsPlanApproved')
      .when(
        sfn.Condition.stringEquals('$.taskRecord.Item.status.S', 'PLAN_APPROVED'),
        new sfn.Pass(this, 'PlanApprovedPass')
      )
      .otherwise(
        new sfn.Fail(this, 'PlanRejected', {
          cause: 'Plan was rejected or timed out',
          error: 'PlanRejected',
        })
      );

    const implementationPhase = new tasks.LambdaInvoke(this, 'ImplementationPhase', {
      lambdaFunction: this.taskProcessorFn,
      payload: sfn.TaskInput.fromObject({
        action: 'implement',
        'taskId.$': '$.taskId',
      }),
      resultPath: '$.implementation',
      heartbeat: cdk.Duration.minutes(30),
      taskTimeout: sfn.Timeout.duration(cdk.Duration.minutes(30)),
    });
    implementationPhase.addRetry({ maxAttempts: 2, backoffRate: 2 });

    const awaitImplApproval = new sfn.Wait(this, 'AwaitImplApproval', {
      time: sfn.WaitTime.timestampPath('$.implApprovalDeadline'),
    });

    const checkImplApproved = new tasks.DynamoGetItem(this, 'CheckImplApproved', {
      table: props.taskTable,
      key: {
        taskId: tasks.DynamoAttributeValue.fromString(
          sfn.JsonPath.stringAt('$.taskId')
        ),
        timestamp: tasks.DynamoAttributeValue.fromString('LATEST'),
      },
      resultPath: '$.implRecord',
    });

    const isImplApproved = new sfn.Choice(this, 'IsImplApproved')
      .when(
        sfn.Condition.stringEquals('$.implRecord.Item.status.S', 'IMPL_APPROVED'),
        new sfn.Pass(this, 'ImplApprovedPass')
      )
      .otherwise(
        new sfn.Fail(this, 'ImplRejected', {
          cause: 'Implementation was rejected or timed out',
          error: 'ImplRejected',
        })
      );

    const createPr = new tasks.LambdaInvoke(this, 'CreatePullRequest', {
      lambdaFunction: this.taskProcessorFn,
      payload: sfn.TaskInput.fromObject({
        action: 'create_pr',
        'taskId.$': '$.taskId',
      }),
      resultPath: '$.pr',
    });

    const notifySuccess = new tasks.SnsPublish(this, 'NotifySuccess', {
      topic: props.notificationsTopic,
      message: sfn.TaskInput.fromObject({
        type: 'TaskCompleted',
        'taskId.$': '$.taskId',
        'prUrl.$': '$.pr.Payload.prUrl',
      }),
    });

    const notifyFailure = new tasks.SnsPublish(this, 'NotifyFailure', {
      topic: props.alertsTopic,
      message: sfn.TaskInput.fromObject({
        type: 'TaskFailed',
        'taskId.$': '$.taskId',
        'error.$': '$$.Error',
        'cause.$': '$$.Cause',
      }),
    });

    // Wire up the state machine definition
    const definition = validateTask
      .next(planningPhase)
      .next(awaitPlanApproval)
      .next(checkPlanApproved)
      .next(isPlanApproved);

    // After isPlanApproved passes, continue to implementation
    const implChain = implementationPhase
      .next(awaitImplApproval)
      .next(checkImplApproved)
      .next(isImplApproved);

    const prAndNotify = createPr.next(notifySuccess);

    this.taskWorkflow = new sfn.StateMachine(this, 'TaskWorkflow', {
      stateMachineName: 'SlackAgentTaskWorkflow',
      definitionBody: sfn.DefinitionBody.fromChainable(definition),
      stateMachineType: sfn.StateMachineType.EXPRESS,
      timeout: cdk.Duration.hours(72),
      logs: {
        destination: new logs.LogGroup(this, 'SfnLogGroup', {
          logGroupName: '/aws/states/slack-agent-workflow',
          retention: logs.RetentionDays.TWO_WEEKS,
          removalPolicy: cdk.RemovalPolicy.DESTROY,
        }),
        level: sfn.LogLevel.ALL,
      },
      tracingEnabled: true,
    });

    // Only the dashboard lambda starts executions. Granting startExecution on
    // the shared lambda role would create an IAM circular dependency with the
    // state machine's own role invoking the task lambdas; scoping the grant to
    // the dashboard function keeps the CFN dependency graph acyclic.
    this.taskWorkflow.grantStartExecution(this.dashboardApiFn);

    // ── Stale-approval sweeper ────────────────────────────────
    // Every 12h, mark approvals that waited too long as expired.
    // The worker also enforces per-task deadlines independently.
    const staleApprovalRule = new cdk.aws_events.Rule(this, 'StaleApprovalSweeper', {
      schedule: cdk.aws_events.Schedule.rate(cdk.Duration.hours(12)),
    });
    staleApprovalRule.addTarget(
      new cdk.aws_events_targets.LambdaFunction(this.taskProcessorFn, {
        event: cdk.aws_events.RuleTargetInput.fromObject({
          action: 'expire_stale',
        }),
      })
    );

    // ── API Gateway ───────────────────────────────────────────
    const apiLogGroup = new logs.LogGroup(this, 'ApiLogGroup', {
      logGroupName: '/slack-agent/api',
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.api = new apigateway.RestApi(this, 'SlackAgentApi', {
      restApiName: 'slack-agent-api',
      description: 'REST API for Slack Coding Agent',
      deployOptions: {
        stageName: 'prod',
        throttlingBurstLimit: 1000,
        throttlingRateLimit: 500,
        loggingLevel: apigateway.MethodLoggingLevel.INFO,
        accessLogDestination: new apigateway.LogGroupLogDestination(apiLogGroup),
        accessLogFormat: apigateway.AccessLogFormat.jsonWithStandardFields(),
        tracingEnabled: true,
        metricsEnabled: true,
      },
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: apigateway.Cors.ALL_METHODS,
        allowHeaders: ['Content-Type', 'Authorization', 'X-Slack-Signature', 'X-Slack-Request-Timestamp'],
      },
    });

    // API Key for webhook endpoint
    const apiKey = this.api.addApiKey('SlackWebhookKey', {
      apiKeyName: 'slack-webhook-key',
      description: 'API key for Slack webhook endpoint',
    });

    const usagePlan = this.api.addUsagePlan('WebhookUsagePlan', {
      name: 'slack-webhook-plan',
      throttle: { rateLimit: 100, burstLimit: 200 },
    });
    usagePlan.addApiKey(apiKey);
    usagePlan.addApiStage({ stage: this.api.deploymentStage });

    // Health endpoint
    const healthResource = this.api.root.addResource('health');
    healthResource.addMethod('GET', new apigateway.MockIntegration({
      integrationResponses: [{ statusCode: '200', responseTemplates: { 'application/json': '{"status":"ok"}' } }],
      requestTemplates: { 'application/json': '{"statusCode": 200}' },
    }), {
      methodResponses: [{ statusCode: '200' }],
    });

    // Webhook endpoint
    const webhookResource = this.api.root.addResource('webhook');
    webhookResource.addMethod('POST',
      new apigateway.LambdaIntegration(this.webhookHandlerFn, { proxy: true }),
      { apiKeyRequired: true }
    );

    // Tasks endpoints
    const tasksResource = this.api.root.addResource('tasks');
    tasksResource.addMethod('GET',
      new apigateway.LambdaIntegration(this.dashboardApiFn, { proxy: true })
    );
    tasksResource.addMethod('POST',
      new apigateway.LambdaIntegration(this.dashboardApiFn, { proxy: true })
    );

    const taskResource = tasksResource.addResource('{id}');
    taskResource.addMethod('GET',
      new apigateway.LambdaIntegration(this.dashboardApiFn, { proxy: true })
    );

    const cancelResource = taskResource.addResource('cancel');
    cancelResource.addMethod('POST',
      new apigateway.LambdaIntegration(this.dashboardApiFn, { proxy: true })
    );

    const approveResource = taskResource.addResource('approve');
    approveResource.addMethod('POST',
      new apigateway.LambdaIntegration(this.taskProcessorFn, { proxy: true })
    );

    const rejectResource = taskResource.addResource('reject');
    rejectResource.addMethod('POST',
      new apigateway.LambdaIntegration(this.taskProcessorFn, { proxy: true })
    );

    // Stats + metrics endpoints
    const statsResource = this.api.root.addResource('stats');
    statsResource.addMethod('GET',
      new apigateway.LambdaIntegration(this.dashboardApiFn, { proxy: true })
    );

    const metricsResource = this.api.root.addResource('metrics');
    metricsResource.addMethod('GET',
      new apigateway.LambdaIntegration(this.dashboardApiFn, { proxy: true })
    );

    // Audit trail
    const auditResource = this.api.root.addResource('audit');
    auditResource.addMethod('GET',
      new apigateway.LambdaIntegration(this.dashboardApiFn, { proxy: true })
    );

    // Admin: kill switch (try-to-break-it panel calls this)
    const adminResource = this.api.root.addResource('admin');
    const killResource = adminResource.addResource('kill');
    killResource.addMethod('GET',
      new apigateway.LambdaIntegration(this.dashboardApiFn, { proxy: true })
    );
    killResource.addMethod('POST',
      new apigateway.LambdaIntegration(this.dashboardApiFn, { proxy: true })
    );

    // ── Site Lambda + HTTP API: dashboard served in-process over HTTPS ──
    // No Fargate service, App Runner or CloudFront. The HTTP API proxy routes
    // every request to one Lambda that serves the built React SPA (bundled at
    // `../lambda/dashboard_api/spa`) and the dashboard API in-process.
    const spaDir = path.join(__dirname, '../lambda/dashboard_api/spa');
    if (!fs.existsSync(path.join(spaDir, 'index.html'))) {
      throw new Error(
        'Dashboard SPA is not built. Run: (cd dashboard && npm ci && npm run build) ' +
        '&& (cd infrastructure && npm run build:spa) before cdk synth/deploy.'
      );
    }

    this.siteFn = new lambda.Function(this, 'DashboardSite', {
      functionName: 'slack-agent-dashboard-site',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'dashboard_site.handler',
      code: lambda.Code.fromAsset(
        path.join(__dirname, '../lambda/dashboard_api')
      ),
      memorySize: 512,
      timeout: cdk.Duration.seconds(30),
      environment: {
        ...lambdaEnv,
        REST_API_URL: this.api.url, // forward approve/reject to the REST API
      },
      role: dashboardRole,
      tracing: lambda.Tracing.ACTIVE,
      logRetention: logs.RetentionDays.TWO_WEEKS,
    });

    this.httpApi = new apigatewayv2.HttpApi(this, 'SlackAgentHttpApi', {
      apiName: 'slack-agent-dashboard',
      description: 'HTTPS dashboard proxy (SPA + dashboard API) for Slack Coding Agent',
    });
    this.httpApi.addRoutes({
      path: '/{proxy+}',
      methods: [apigatewayv2.HttpMethod.GET, apigatewayv2.HttpMethod.POST, apigatewayv2.HttpMethod.OPTIONS],
      integration: new integrations.HttpLambdaIntegration('DashboardSiteIntegration', this.siteFn),
    });

    // ── Outputs ───────────────────────────────────────────────
    new cdk.CfnOutput(this, 'ApiUrl', {
      value: this.api.url,
      exportName: 'SlackAgentApiUrl',
      description: 'API Gateway URL',
    });

    new cdk.CfnOutput(this, 'WebhookUrl', {
      value: `${this.api.url}webhook`,
      exportName: 'SlackAgentWebhookUrl',
      description: 'Slack webhook endpoint — configure in Slack App settings',
    });

    new cdk.CfnOutput(this, 'StateMachineArn', {
      value: this.taskWorkflow.stateMachineArn,
      exportName: 'SlackAgentStateMachineArn',
    });

    new cdk.CfnOutput(this, 'DashboardUrl', {
      value: this.httpApi.url ?? '',
      exportName: 'SlackAgentDashboardUrl',
      description: 'HTTPS dashboard URL (HTTP API + site Lambda)',
    });
  }
}
