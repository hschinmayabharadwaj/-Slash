import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import { Construct } from 'constructs';

/**
 * Static demo console for the Slack Coding Agent dashboard.
 *
 * Fully us-east-2. The React SPA (built against the API Gateway URL) lives in a
 * private S3 bucket and is served through CloudFront via Origin Access Control,
 * using the default *.cloudfront.net cert (no ACM / us-east-1 dependency).
 * S3 static-website endpoints are not resolvable in this account, so CloudFront
 * is the origin for the SPA.
 */
export class StaticSiteStack extends cdk.Stack {
  public readonly bucket: s3.Bucket;
  public readonly distribution: cloudfront.Distribution;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    this.bucket = new s3.Bucket(this, 'DashboardBucket', {
      bucketName: `slack-agent-dashboard-${this.account}`,
      versioned: false,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      lifecycleRules: [{ expiration: cdk.Duration.days(90) }],
    });

    const oac = new cloudfront.OriginAccessIdentity(this, 'Oai', {
      comment: 'serve slack-agent dashboard SPA from S3',
    });

    const distBucketOrigin = new origins.S3Origin(this.bucket, {
      originAccessIdentity: oac,
    });

    this.distribution = new cloudfront.Distribution(this, 'DashboardDistribution', {
      comment: 'slack-agent dashboard (us-east-2 S3 origin)',
      defaultBehavior: {
        origin: distBucketOrigin,
        compress: true,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      },
      defaultRootObject: 'index.html',
      errorResponses: [
        {
          httpStatus: 404,
          responseHttpStatus: 200,
          responsePagePath: '/index.html',
          ttl: cdk.Duration.seconds(60),
        },
      ],
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
    });

    new cdk.CfnOutput(this, 'DashboardUrl', {
      value: `https://${this.distribution.distributionDomainName}`,
      exportName: 'SlackAgentDashboardUrl',
      description: 'CloudFront URL for the demo dashboard',
    });
  }
}