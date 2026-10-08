import { Construct } from 'constructs';
import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as path from 'path';

export interface LambdaProxyProps {
  environment: string;
  conversationTableArn: string;
  conversationTableName: string;
  overlayTableArn: string;
  overlayTableName: string;
  userPoolId: string;
  userPoolClientId: string;
  artifactsBucketName: string;
  snowflakeSecretArn: string;
  /** CloudFront origin URL to add to CORS allowed origins */
  frontendOrigin?: string;
}

export class LambdaProxy extends Construct {
  public readonly fn: lambda.Function;
  public readonly functionUrl: lambda.FunctionUrl;

  constructor(scope: Construct, id: string, props: LambdaProxyProps) {
    super(scope, id);

    const isProd = props.environment === 'prod';
    const projectRoot = path.resolve(__dirname, '..', '..', '..');

    // Lambda execution role
    const role = new iam.Role(this, 'Role', {
      roleName: `illuminate-lambda-${props.environment}`,
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
    });

    // Bedrock model invocation (direct, no AgentCore)
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
      resources: [
        'arn:aws:bedrock:*::foundation-model/anthropic.claude-*',
        `arn:aws:bedrock:*:${cdk.Aws.ACCOUNT_ID}:inference-profile/us.anthropic.claude-*`,
      ],
    }));

    // Bedrock Converse API
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock:Converse', 'bedrock:ConverseStream'],
      resources: ['*'],
    }));

    // DynamoDB conversation memory
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:DeleteItem'],
      resources: [props.conversationTableArn],
    }));

    // DynamoDB per-tenant overlays (read for query path, read+write for admin)
    role.addToPolicy(new iam.PolicyStatement({
      actions: [
        'dynamodb:GetItem',
        'dynamodb:PutItem',
        'dynamodb:DeleteItem',
        'dynamodb:Query',
      ],
      resources: [props.overlayTableArn],
    }));

    // Secrets Manager (read + write for config endpoint)
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['secretsmanager:GetSecretValue', 'secretsmanager:PutSecretValue'],
      resources: [props.snowflakeSecretArn],
    }));

    // S3
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject', 's3:PutObject'],
      resources: [`arn:aws:s3:::${props.artifactsBucketName}/*`],
    }));

    // Cognito
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['cognito-idp:GetUser', 'cognito-idp:AdminGetUser'],
      resources: [
        `arn:aws:cognito-idp:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:userpool/${props.userPoolId}`,
      ],
    }));

    // Build CORS allowed origins.
    // Dev includes the existing CloudFront distribution URL inline — the
    // frontend stack's AddCorsOrigin custom resource was supposed to append
    // this dynamically but only read the config, never wrote. Hardcoding
    // here is more reliable than a buggy custom resource and the URL is
    // stable for the lifetime of the IlluminatePoc stack.
    const defaultOrigins = isProd
      ? 'https://illuminate.anthology.com'
      : 'http://localhost:3000,http://localhost:5173,https://dm5zbussw00dg.cloudfront.net';
    const allowedOrigins = props.frontendOrigin
      ? `${defaultOrigins},${props.frontendOrigin}`
      : defaultOrigins;

    // Lambda Web Adapter layer
    const lwaLayer = lambda.LayerVersion.fromLayerVersionArn(this, 'LWA',
      `arn:aws:lambda:${cdk.Aws.REGION}:753240598075:layer:LambdaAdapterLayerX86:27`,
    );

    this.fn = new lambda.Function(this, 'Function', {
      functionName: `illuminate-api-${props.environment}`,
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'run.sh',
      role,
      timeout: cdk.Duration.seconds(900),
      memorySize: 1024,
      layers: [lwaLayer],
      code: lambda.Code.fromAsset(projectRoot, {
        bundling: {
          image: lambda.Runtime.PYTHON_3_11.bundlingImage,
          command: [
            'bash', '-c', [
              'pip install -q -t /asset-output --platform manylinux2014_x86_64 --implementation cp --python-version 3.11 --only-binary=:all: -r requirements-lambda.txt',
              'cp lambda_handler.py chat_engine.py conversation_store.py snowflake_client.py tenant_store.py run.sh /asset-output/',
              'cp verified_queries.json /asset-output/ 2>/dev/null || true',
              // Semantic layer: ship the package + canonical metric YAML
              'cp -r semantic_layer /asset-output/',
              'cp -r canonical /asset-output/',
            ].join(' && '),
          ],
        },
      }),
      environment: {
        AWS_LAMBDA_EXEC_WRAPPER: '/opt/bootstrap',
        PORT: '8080',
        AWS_LWA_READINESS_CHECK_PATH: '/health',
        AWS_LWA_INVOKE_MODE: 'response_stream',
        CONVERSATION_TABLE: props.conversationTableName,
        OVERLAY_TABLE: props.overlayTableName,
        BEDROCK_MODEL_ID: 'us.anthropic.claude-sonnet-4-6',
        ARTIFACTS_BUCKET: props.artifactsBucketName,
        USER_POOL_ID: props.userPoolId,
        USER_POOL_CLIENT_ID: props.userPoolClientId,
        ALLOWED_ORIGINS: allowedOrigins,
        ACCOUNT_ID: cdk.Aws.ACCOUNT_ID,
        SNOWFLAKE_SECRET_NAME: `illuminate/${props.environment}/snowflake`,
        LOG_LEVEL: isProd ? 'WARN' : 'INFO',
      },
    });

    // Function URL with response streaming. Auth is the app's Cognito JWT check on every route:
    // AWS_IAM behind CloudFront OAC would need the browser to sign a SHA-256 of each POST body.
    this.functionUrl = this.fn.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.NONE,
      invokeMode: lambda.InvokeMode.RESPONSE_STREAM,
    });

    // Log group
    new logs.LogGroup(this, 'LogGroup', {
      logGroupName: `/aws/lambda/${this.fn.functionName}`,
      retention: isProd ? logs.RetentionDays.ONE_MONTH : logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
  }

  /** Extract the domain from the Function URL (for CloudFront origin) */
  get functionUrlDomain(): string {
    return cdk.Fn.select(2, cdk.Fn.split('/', this.functionUrl.url));
  }
}
