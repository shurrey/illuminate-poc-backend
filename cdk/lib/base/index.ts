import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { Auth } from './auth';
import { Storage } from './storage';
import { Discovery } from './discovery';

export interface BaseStackProps extends cdk.StackProps {
  environment: string;
  initialUserEmail?: string;
  initialUserPassword?: string;
  initialUserName?: string;
}

export class BaseStack extends cdk.Stack {
  public readonly userPool: cognito.UserPool;
  public readonly userPoolClient: cognito.UserPoolClient;
  public readonly artifactsBucket: s3.Bucket;
  public readonly snowflakeSecret: secretsmanager.Secret;

  constructor(scope: Construct, id: string, props: BaseStackProps) {
    super(scope, id, props);

    const auth = new Auth(this, 'Auth', {
      environment: props.environment,
      initialUserEmail: props.initialUserEmail,
      initialUserPassword: props.initialUserPassword,
      initialUserName: props.initialUserName,
    });
    this.userPool = auth.userPool;
    this.userPoolClient = auth.userPoolClient;

    const storage = new Storage(this, 'Storage', {
      environment: props.environment,
    });
    this.artifactsBucket = storage.artifactsBucket;
    this.snowflakeSecret = storage.snowflakeSecret;

    // Publish discovery parameters to SSM
    new Discovery(this, 'Discovery', {
      environment: props.environment,
      parameters: {
        'cognito-pool-id': auth.userPool.userPoolId,
        'cognito-client-id': auth.userPoolClient.userPoolClientId,
        'artifacts-bucket': storage.artifactsBucket.bucketName,
        'snowflake-secret-arn': storage.snowflakeSecret.secretArn,
      },
    });
  }
}
