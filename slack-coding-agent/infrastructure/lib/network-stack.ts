import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export class NetworkStack extends cdk.Stack {
  public readonly vpc: ec2.Vpc;
  public readonly botSecurityGroup: ec2.SecurityGroup;
  public readonly dbSecurityGroup: ec2.SecurityGroup;
  public readonly appRunnerSecurityGroup: ec2.SecurityGroup;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ── VPC ──────────────────────────────────────────────────
    // Cheap demo networking: public subnets only (no NAT gateway — $0/mo).
    // Egress is restricted to HTTPS via security groups; this is an
    // allow-PORT-443 rule, NOT a domain allowlist. Production hardening =
    // private subnets + VPC endpoints + an allowlisted proxy.
    this.vpc = new ec2.Vpc(this, 'SlackAgentVpc', {
      vpcName: 'slack-agent-vpc',
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        {
          name: 'Public',
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
        {
          name: 'Isolated',
          subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
          cidrMask: 28,
        },
      ],
    });

    // ── VPC Flow Logs ─────────────────────────────────────────
    const flowLogBucket = new s3.Bucket(this, 'FlowLogsBucket', {
      bucketName: `slack-agent-flow-logs-${this.account}-${this.region}`,
      lifecycleRules: [{ expiration: cdk.Duration.days(30) }],
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
    });

    this.vpc.addFlowLog('VpcFlowLog', {
      destination: ec2.FlowLogDestination.toS3(flowLogBucket),
      trafficType: ec2.FlowLogTrafficType.ALL,
    });

    // ── Security Groups ───────────────────────────────────────
    this.botSecurityGroup = new ec2.SecurityGroup(this, 'BotSG', {
      vpc: this.vpc,
      securityGroupName: 'slack-agent-bot-sg',
      description: 'Security group for Slack bot ECS tasks',
      allowAllOutbound: false,
    });

    // Bot can reach HTTPS (Slack API, GitHub API, Gemini API)
    this.botSecurityGroup.addEgressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      'Allow HTTPS outbound'
    );
    // Bot can reach PostgreSQL
    this.botSecurityGroup.addEgressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(5432),
      'Allow PostgreSQL outbound'
    );

    this.dbSecurityGroup = new ec2.SecurityGroup(this, 'DbSG', {
      vpc: this.vpc,
      securityGroupName: 'slack-agent-db-sg',
      description: 'Security group for Aurora PostgreSQL',
      allowAllOutbound: false,
    });

    // Aurora only accepts connections from bot SG
    this.dbSecurityGroup.addIngressRule(
      this.botSecurityGroup,
      ec2.Port.tcp(5432),
      'Allow PostgreSQL from bot'
    );

    this.appRunnerSecurityGroup = new ec2.SecurityGroup(this, 'AppRunnerSG', {
      vpc: this.vpc,
      securityGroupName: 'slack-agent-apprunner-sg',
      description: 'Security group for App Runner VPC connector',
      allowAllOutbound: true,
    });

    // ── VPC Endpoints (reduce NAT costs) ─────────────────────
    this.vpc.addGatewayEndpoint('S3Endpoint', {
      service: ec2.GatewayVpcEndpointAwsService.S3,
    });

    this.vpc.addGatewayEndpoint('DynamoDbEndpoint', {
      service: ec2.GatewayVpcEndpointAwsService.DYNAMODB,
    });

    // ── Outputs ───────────────────────────────────────────────
    new cdk.CfnOutput(this, 'VpcId', {
      value: this.vpc.vpcId,
      exportName: 'SlackAgentVpcId',
      description: 'VPC ID for Slack Coding Agent',
    });

    new cdk.CfnOutput(this, 'BotSecurityGroupId', {
      value: this.botSecurityGroup.securityGroupId,
      exportName: 'SlackAgentBotSGId',
    });
  }
}
