import * as cdk from 'aws-cdk-lib';
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
   * Tenant id stamped on the initial user. The seed user is created only when
   * this is set.
   */
  initialTenantId?: string;
}

/** Members may edit their tenant's metric overlays; the API reads it from the cognito:groups claim. */
export const ADMIN_GROUP_NAME = 'illuminate-admins';

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

    const adminGroup = new cognito.CfnUserPoolGroup(this, 'AdminGroup', {
      userPoolId: this.userPool.userPoolId,
      groupName: ADMIN_GROUP_NAME,
      description: 'Can edit tenant metric overlays',
    });
    // Roles (roles.py): Authors and Developers see identity in reports; users in no group are Viewers.
    new cognito.CfnUserPoolGroup(this, 'AuthorGroup', {
      userPoolId: this.userPool.userPoolId,
      groupName: 'illuminate-authors',
      description: 'Illuminate Authors',
    });
    new cognito.CfnUserPoolGroup(this, 'DeveloperGroup', {
      userPoolId: this.userPool.userPoolId,
      groupName: 'illuminate-developers',
      description: 'Illuminate Developers',
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
      // re-tenant themselves via UpdateUserAttributes. custom:tenant_id is admin-only. Cognito
      // requires required attributes to be writable; email is immutable, so users still can't change it.
      writeAttributes: new cognito.ClientAttributes().withStandardAttributes({ email: true, fullname: true }),
    });

    // Seed admin user, stamped with its tenant at creation. Every call is onCreate only (an Update runs none) and the
    // create fails rather than adopt an existing user, so no deploy changes a user this stack did not create.
    // Later calls use the create's physical id (the created username): a no-op Update keeps it but returns no data.
    if (props.initialUserEmail && props.initialUserPassword && props.initialTenantId) {
      const tenantId = props.initialTenantId;

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
          physicalResourceId: cr.PhysicalResourceId.fromResponse('User.Username'),
          outputPaths: ['User.Username'],
        },
        policy: cr.AwsCustomResourcePolicy.fromStatements([
          new iam.PolicyStatement({
            actions: ['cognito-idp:AdminCreateUser'],
            resources: [this.userPool.userPoolArn],
          }),
        ]),
      });
      const createdUsername = (createUser.node.defaultChild as cdk.CustomResource).ref;

      // Set the permanent password (moves user from FORCE_CHANGE_PASSWORD to CONFIRMED)
      const setPassword = new cr.AwsCustomResource(this, 'InitialUserPassword', {
        onCreate: {
          service: 'CognitoIdentityServiceProvider',
          action: 'adminSetUserPassword',
          parameters: {
            UserPoolId: this.userPool.userPoolId,
            Username: createdUsername,
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

      const addToAdmins = new cr.AwsCustomResource(this, 'InitialUserAdminGroup', {
        onCreate: {
          service: 'CognitoIdentityServiceProvider',
          action: 'adminAddUserToGroup',
          parameters: {
            UserPoolId: this.userPool.userPoolId,
            Username: createdUsername,
            GroupName: ADMIN_GROUP_NAME,
          },
          physicalResourceId: cr.PhysicalResourceId.of(`initial-user-admin-${props.initialUserEmail}`),
          ignoreErrorCodesMatching: 'UserNotFoundException',
        },
        policy: cr.AwsCustomResourcePolicy.fromStatements([
          new iam.PolicyStatement({
            actions: ['cognito-idp:AdminAddUserToGroup'],
            resources: [this.userPool.userPoolArn],
          }),
        ]),
      });
      addToAdmins.node.addDependency(adminGroup);
    }
  }
}
