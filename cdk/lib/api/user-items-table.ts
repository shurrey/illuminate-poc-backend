import { Construct } from 'constructs';
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';

export interface UserItemsTableProps {
  environment: string;
}

/**
 * Per-user profile items, written by user_items_store.py:
 *
 *   tenant_id   (HASH)   — the caller's custom:tenant_id
 *   item_key    (RANGE)  — user#<sub>#card#<id> and user#<sub>#query#<id>, or the singletons
 *                          user#<sub>#layout, user#<sub>#prefs and user#<sub>#imported
 *   kind, id (card and query only), body (map), version, updated_at
 */
export class UserItemsTable extends Construct {
  public readonly table: dynamodb.Table;
  public readonly tableName: string;

  constructor(scope: Construct, id: string, props: UserItemsTableProps) {
    super(scope, id);

    this.table = new dynamodb.Table(this, 'Table', {
      tableName: `illuminate-user-items-${props.environment}`,
      partitionKey: { name: 'tenant_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'item_key', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.tableName = this.table.tableName;
  }
}
