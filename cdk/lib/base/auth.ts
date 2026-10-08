import { Construct } from 'constructs';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as iam from 'aws-cdk-lib/aws-iam';

export interface AuthProps {
  environment: string;
  /** Initial admin user email. If set, creates the user on first deploy. */
  initialUserEmail?: string;
  /** Initial admin user password. Must meet the pool's password policy. */
  initialUserPassword?: string;
  /** Display name for the initial admin user. */
  initialUserName?: string;
  /**
   * Tenant id stamped on the initial user. Drives per-tenant overlay lookup
   * in the API. Defaults to "blackboard-dev" if not set.
   */
  initialTenantId?: string;
}

export class Auth extends Construct {
  public readonly userPool: cognito.UserPool;
  public readonly userPoolClient: cognito.UserPoolClient;

  constructor(scope: Construct, id: string, props: AuthProps) {
    super(scope, id);

    this.userPool = new cognito.UserPool(this, 'UserPool', {
      userPoolName: `illuminate-users-${props.environment}`,
      signInAliases: { email: true },
      autoVerify: { email: true },
      passwordPolicy: {
        minLength: 12,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: true,
      },
      standardAttributes: {
        email: { required: true, mutable: false },
      },
      // Custom attribute: tenant_id selects which overlay applies to this
      // user's queries. Mutable so admins can re-tenant a user later.
      // MaxLen=256 matches the existing pool's schema; Cognito custom
      // attributes are immutable once added, so this must not drift.
      customAttributes: {
        tenant_id: new cognito.StringAttribute({ minLen: 1, maxLen: 256, mutable: true }),
      },
    });

    // Set UserPoolTier to LITE (not exposed in L2 construct)
    const cfnUserPool = this.userPool.node.defaultChild as cognito.CfnUserPool;
    cfnUserPool.addPropertyOverride('UserPoolTier', 'LITE');

    this.userPoolClient = new cognito.UserPoolClient(this, 'UserPoolClient', {
      userPool: this.userPool,
      userPoolClientName: `illuminate-api-${props.environment}`,
      generateSecret: false,
      authFlows: {
        userPassword: true,
        userSrp: true,
      },
      // Without an explicit list every attribute is user-writable, which would let a user
      // re-tenant themselves via UpdateUserAttributes. custom:tenant_id is admin-only.
      writeAttributes: new cognito.ClientAttributes().withStandardAttributes({ fullname: true }),
    });

    // Create initial admin user on first deploy (idempotent — ignores if user exists)
    if (props.initialUserEmail && props.initialUserPassword) {
      const tenantId = props.initialTenantId ?? 'blackboard-dev';

      const createUser = new cr.AwsCustomResource(this, 'InitialUser', {
        onCreate: {
          service: 'CognitoIdentityServiceProvider',
          action: 'adminCreateUser',
          parameters: {
            UserPoolId: this.userPool.userPoolId,
            Username: props.initialUserEmail,
            UserAttributes: [
              { Name: 'email', Value: props.initialUserEmail },
              { Name: 'email_verified', Value: 'true' },
              { Name: 'custom:tenant_id', Value: tenantId },
              ...(props.initialUserName
                ? [{ Name: 'name', Value: props.initialUserName }]
                : []),
            ],
            MessageAction: 'SUPPRESS',
            TemporaryPassword: props.initialUserPassword,
          },
          physicalResourceId: cr.PhysicalResourceId.of(`initial-user-${props.initialUserEmail}`),
          ignoreErrorCodesMatching: 'UsernameExistsException',
        },
        policy: cr.AwsCustomResourcePolicy.fromStatements([
          new iam.PolicyStatement({
            actions: ['cognito-idp:AdminCreateUser', 'cognito-idp:AdminSetUserPassword'],
            resources: [this.userPool.userPoolArn],
          }),
        ]),
      });

      // Backfill: ensure the tenant_id attribute is set even on existing users
      // (the InitialUser resource above no-ops if the user already exists).
      const setTenantId = new cr.AwsCustomResource(this, 'InitialUserTenantId', {
        onCreate: {
          service: 'CognitoIdentityServiceProvider',
          action: 'adminUpdateUserAttributes',
          parameters: {
            UserPoolId: this.userPool.userPoolId,
            Username: props.initialUserEmail,
            UserAttributes: [{ Name: 'custom:tenant_id', Value: tenantId }],
          },
          physicalResourceId: cr.PhysicalResourceId.of(
            `initial-user-tenant-${props.initialUserEmail}-${tenantId}`,
          ),
        },
        onUpdate: {
          service: 'CognitoIdentityServiceProvider',
          action: 'adminUpdateUserAttributes',
          parameters: {
            UserPoolId: this.userPool.userPoolId,
            Username: props.initialUserEmail,
            UserAttributes: [{ Name: 'custom:tenant_id', Value: tenantId }],
          },
          physicalResourceId: cr.PhysicalResourceId.of(
            `initial-user-tenant-${props.initialUserEmail}-${tenantId}`,
          ),
        },
        policy: cr.AwsCustomResourcePolicy.fromStatements([
          new iam.PolicyStatement({
            actions: ['cognito-idp:AdminUpdateUserAttributes'],
            resources: [this.userPool.userPoolArn],
          }),
        ]),
      });
      setTenantId.node.addDependency(createUser);

      // Set the permanent password (moves user from FORCE_CHANGE_PASSWORD to CONFIRMED)
      const setPassword = new cr.AwsCustomResource(this, 'InitialUserPassword', {
        onCreate: {
          service: 'CognitoIdentityServiceProvider',
          action: 'adminSetUserPassword',
          parameters: {
            UserPoolId: this.userPool.userPoolId,
            Username: props.initialUserEmail,
            Password: props.initialUserPassword,
            Permanent: true,
          },
          physicalResourceId: cr.PhysicalResourceId.of(`initial-user-password-${props.initialUserEmail}`),
        },
        policy: cr.AwsCustomResourcePolicy.fromStatements([
          new iam.PolicyStatement({
            actions: ['cognito-idp:AdminSetUserPassword'],
            resources: [this.userPool.userPoolArn],
          }),
        ]),
      });

      setPassword.node.addDependency(createUser);
    }
  }
}
