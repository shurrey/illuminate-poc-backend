import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { LambdaProxy } from './lambda-proxy';
import { ConversationTable } from './conversation-table';
import { OverlayTable } from './overlay-table';
import { UserItemsTable } from './user-items-table';
import { TenantTable } from './tenant-table';
import { McpSessionsTable } from './mcp-sessions-table';
import { Discovery } from '../base/discovery';

export interface ApiStackProps extends cdk.StackProps {
  environment: string;
  userPoolId: string;
  userPoolClientId: string;
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

    const userItemsTable = new UserItemsTable(this, 'UserItemsTable', {
      environment: props.environment,
    });

    const tenantTable = new TenantTable(this, 'TenantTable', {
      environment: props.environment,
    });

    const mcpSessionsTable = new McpSessionsTable(this, 'McpSessionsTable', {
      environment: props.environment,
    });

    const proxy = new LambdaProxy(this, 'LambdaProxy', {
      environment: props.environment,
      conversationTableArn: conversationTable.table.tableArn,
      conversationTableName: conversationTable.tableName,
      overlayTableArn: overlayTable.table.tableArn,
      overlayTableName: overlayTable.tableName,
      userItemsTableArn: userItemsTable.table.tableArn,
      userItemsTableName: userItemsTable.tableName,
      tenantTableArn: tenantTable.table.tableArn,
      tenantTableName: tenantTable.tableName,
      mcpSessionsTableArn: mcpSessionsTable.table.tableArn,
      mcpSessionsTableName: mcpSessionsTable.tableName,
      userPoolId: props.userPoolId,
      userPoolClientId: props.userPoolClientId,
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
