import { Construct } from 'constructs';
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';

export interface TenantTableProps {
  environment: string;
}

/**
 * Tenant registry, read by tenancy.py:
 *
 *   tenant_id   (HASH)   — the caller's custom:tenant_id
 *   name, license, mcp_enabled (default false), created_at, updated_at
 */
export class TenantTable extends Construct {
  public readonly table: dynamodb.Table;
  public readonly tableName: string;

  constructor(scope: Construct, id: string, props: TenantTableProps) {
    super(scope, id);

    this.table = new dynamodb.Table(this, 'Table', {
      tableName: `illuminate-tenants-${props.environment}`,
      partitionKey: { name: 'tenant_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.tableName = this.table.tableName;
  }
}
