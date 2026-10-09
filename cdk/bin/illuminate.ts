#!/usr/bin/env npx ts-node
import * as cdk from 'aws-cdk-lib';
import * as fs from 'fs';
import * as path from 'path';
import { BaseStack } from '../lib/base';
import { ApiStack } from '../lib/api';
// Frontend is deployed separately (its own project/stack)
// import { FrontendStack } from '../lib/frontend';

// Load .env file from project root
const envFile = path.resolve(__dirname, '..', '..', '.env');
const envVars: Record<string, string> = {};
if (fs.existsSync(envFile)) {
  for (const line of fs.readFileSync(envFile, 'utf-8').split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eqIdx = trimmed.indexOf('=');
    if (eqIdx > 0) {
      envVars[trimmed.slice(0, eqIdx)] = trimmed.slice(eqIdx + 1);
    }
  }
}

const app = new cdk.App();

// Environment from CDK context: cdk deploy -c environment=dev
const environment = app.node.tryGetContext('environment') || 'dev';

const env: cdk.Environment = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION || 'us-east-1',
};

// =============================================================================
// Stack 1: Base infrastructure (Cognito, S3, Secrets)
// =============================================================================
const base = new BaseStack(app, `IlluminateBase-${environment}`, {
  env,
  environment,
  // Initial admin user — only created on first deploy
  initialUserEmail: app.node.tryGetContext('initialUserEmail') || envVars['COGNITO_USER_EMAIL'] || 'admin@example.com',
  initialUserPassword: app.node.tryGetContext('initialUserPassword') || envVars['COGNITO_USER_PASSWORD'] || '',
  initialUserName: app.node.tryGetContext('initialUserName') || envVars['COGNITO_USER_NAME'] || 'Illuminate Admin',
});

// =============================================================================
// Stack 2: API (Lambda + Function URL)
// =============================================================================
const api = new ApiStack(app, `IlluminateApi-${environment}`, {
  env,
  environment,
  userPoolId: base.userPool.userPoolId,
  userPoolClientId: base.userPoolClient.userPoolClientId,
  artifactsBucketName: base.artifactsBucket.bucketName,
  snowflakeSecretArn: base.snowflakeSecret.secretArn,
});
api.addDependency(base);

// Frontend is deployed separately — keep code in lib/frontend/ for reference.
// To include it: uncomment FrontendStack import above and instantiate here.

app.synth();
