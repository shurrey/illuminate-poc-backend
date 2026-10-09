import { Construct } from 'constructs';
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';

export interface OverlayTableProps {
  environment: string;
}

/**
 * Per-tenant semantic-layer overlays, written by overlay_store.py:
 *
 *   tenant_id   (HASH)   — the caller's custom:tenant_id
 *   metric_id   (RANGE)  — the target (measure:<dataset>:<name>, filter:..., metric:<id>);
 *                          each saved version is also kept under <target>#v<000001>
 *   expr | sql | default_filters — the overridden field, by target kind
 *   description, version, updated_by, updated_at
 */
export class OverlayTable extends Construct {
  public readonly table: dynamodb.Table;
  public readonly tableName: string;

  constructor(scope: Construct, id: string, props: OverlayTableProps) {
    super(scope, id);

    this.table = new dynamodb.Table(this, 'Table', {
      tableName: `illuminate-overlays-${props.environment}`,
      partitionKey: { name: 'tenant_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'metric_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.tableName = this.table.tableName;
  }
}
