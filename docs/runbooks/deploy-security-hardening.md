# Deploying the Phase 3 security changes

Phase 3 (#20–#27) changes Cognito, IAM and the base stack. Merging them deploys nothing. Use these
steps for the first `cdk deploy` that includes them.

## Before deploying

1. **Snapshot tenant assignments.** Until the app-client change (#20) is live, any user could rewrite their
   own `custom:tenant_id`. Record the current values so they can be checked:

   ```bash
   aws cognito-idp list-users --user-pool-id <pool-id> \
     --query 'Users[].{user:Username,tenant:Attributes[?Name==`custom:tenant_id`]|[0].Value}' --output table
   ```

   Compare each user's tenant with the tenant they should belong to, and correct any mismatch with
   `aws cognito-idp admin-update-user-attributes`.

2. **List who edits metric overlays today.** After deploy, only members of `illuminate-admins` can use
   `/api/v1/admin/*`, the Metric Definitions page, and the table preview on the Developer page.

## Deploy

```bash
cd cdk
npx cdk deploy IlluminateBase-<env> IlluminateApi-<env> -c environment=<env>
```

- The base stack **deletes** the VPC, subnets, NAT gateway, Elastic IP and the unattached WAF web ACL (#24).
  Nothing in the API used them.
- The initial user is added to `illuminate-admins` only when `COGNITO_USER_PASSWORD` is set for the deploy.
  A missing initial user is ignored rather than failing the stack.

## After deploying

1. **Add each tenant administrator to the group:**

   ```bash
   aws cognito-idp admin-add-user-to-group --user-pool-id <pool-id> --username <email> --group-name illuminate-admins
   ```

2. **Ask administrators to sign out and back in.** The group arrives in the ID token's `cognito:groups`
   claim, and existing tokens keep their old claims until they refresh, which takes up to an hour.

3. **Re-run the tenant listing** from step 1 and confirm nothing changed.
