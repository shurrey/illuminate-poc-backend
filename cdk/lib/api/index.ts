import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { LambdaProxy } from './lambda-proxy';
import { ConversationTable } from './conversation-table';
import { OverlayTable } from './overlay-table';
import { Discovery } from '../base/discovery';

export interface ApiStackProps extends cdk.StackProps {
  environment: string;
  userPoolId: string;
  userPoolClientId: string;
  snowflakeSecretArn: string;
}

export class ApiStack extends cdk.Stack {
  public readonly functionUrl: lambda.FunctionUrl;
  public readonly functionUrlDomain: string;
  public readonly lambdaFunction: lambda.Function;

  constructor(scope: Construct, id: string, props: ApiStackProps) {
    super(scope, id, props);

    const conversationTable = new ConversationTable(this, 'ConversationTable', {
      environment: props.environment,
    });

    const overlayTable = new OverlayTable(this, 'OverlayTable', {
      environment: props.environment,
    });

    const proxy = new LambdaProxy(this, 'LambdaProxy', {
      environment: props.environment,
      conversationTableArn: conversationTable.table.tableArn,
      conversationTableName: conversationTable.tableName,
      overlayTableArn: overlayTable.table.tableArn,
      overlayTableName: overlayTable.tableName,
      userPoolId: props.userPoolId,
      userPoolClientId: props.userPoolClientId,
      snowflakeSecretArn: props.snowflakeSecretArn,
    });

    this.functionUrl = proxy.functionUrl;
    this.functionUrlDomain = proxy.functionUrlDomain;
    this.lambdaFunction = proxy.fn;

    // Publish discovery parameters to SSM
    new Discovery(this, 'Discovery', {
      environment: props.environment,
      parameters: {
        'api-url': proxy.functionUrl.url,
      },
    });
  }
}
