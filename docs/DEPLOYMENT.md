# Deployment Guide

Infrastructure is an AWS CDK app in `cdk/` (TypeScript, entry point `cdk/bin/illuminate.ts`). The
frontend is deployed from its own repository.

## Prerequisites

- AWS CLI v2 with credentials for the target account
- Node.js and npm (`cd cdk && npm install` installs the CDK CLI locally)
- Docker, running: the Lambda asset is bundled in the Python 3.11 build image
- A CDK-bootstrapped account and region: `npx cdk bootstrap` (once per account/region)
- Bedrock model access for Claude Sonnet 4.6 (inference profile `us.anthropic.claude-sonnet-4-6`)
  in the deploy region
- A Snowflake service user with read access to the `CDM_*` schemas

Region comes from `CDK_DEFAULT_REGION`, defaulting to `us-east-1`; account from `CDK_DEFAULT_ACCOUNT`.

## Configuration

```bash
cp .env.example .env
```

`.env` is raw `KEY=VALUE`, one per line, with no quoting. Two things read it:

- **`scripts/set-snowflake-secret.sh`** reads the Snowflake values: `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_PASSWORD` and `SNOWFLAKE_DATABASE` (all required), plus `SNOWFLAKE_USER` (default `SVC_BLACKBOARD_DATA`), `SNOWFLAKE_WAREHOUSE` (default `BLACKBOARD_DATA_WH`) and `SNOWFLAKE_ROLE` (default `BBDATA_USER_ROLE`). See [Secrets](#secrets). `SNOWFLAKE_DATABASE` is also the database the semantic layer's `{{ database }}` resolves to.
- **`cdk/bin/illuminate.ts`** reads the Cognito values. Each can be overridden with a CDK context flag (`-c <key>=<value>`):

| `.env` | Context key | Used for |
|--------|-------------|----------|
| `COGNITO_USER_EMAIL` | `initialUserEmail` | Initial Cognito user (default `admin@example.com`) |
| `COGNITO_USER_PASSWORD` | `initialUserPassword` | Initial user's password. The initial user is created, given `custom:tenant_id = blackboard-dev` and added to `illuminate-admins` only when this is set. |
| `COGNITO_USER_NAME` | `initialUserName` | Initial user's display name |

The environment name comes from `-c environment=<env>` (default `dev`, set in `cdk/cdk.json`) and
is part of every resource name.

`.env` is gitignored. Snowflake credentials never pass through CDK or the CloudFormation template.

## Stacks

| Stack | Resources |
|-------|-----------|
| `IlluminateBase-<env>` | Cognito user pool `illuminate-users-<env>` (LITE tier, email sign-in, `custom:tenant_id` attribute), app client `illuminate-api-<env>` (users cannot write `custom:tenant_id`), group `illuminate-admins`, the initial user; S3 bucket `illuminate-artifacts-<env>-<account>` (retained on delete); Secrets Manager secret `illuminate/<env>/snowflake`; SSM parameters |
| `IlluminateApi-<env>` | Lambda `illuminate-api-<env>` (Python 3.11, x86_64, 1024 MB, 900 s timeout, Lambda Web Adapter layer) with a Function URL (auth `NONE`, `RESPONSE_STREAM`); DynamoDB tables `illuminate-conversations-<env>` and `illuminate-overlays-<env>`; IAM role `illuminate-lambda-<env>`; log group (one week, one month in `prod`); SSM parameter `api-url` |

`IlluminateApi-<env>` depends on `IlluminateBase-<env>`. Both DynamoDB tables have
`RemovalPolicy.DESTROY`: deleting the API stack deletes all conversations and tenant overlays.

The Lambda bundle is `pip install -r requirements-lambda.txt` plus `lambda_handler.py`,
`chat_engine.py`, `conversation_store.py`, `snowflake_client.py`, `overlay_store.py`, `run.sh`,
`semantic_layer/` and `canonical/`. A change to any of these, including dataset and metric YAML,
ships with an API stack deploy.

### SSM discovery parameters

Published under `/illuminate/<env>/`: `cognito-pool-id`, `cognito-client-id`, `artifacts-bucket`,
`snowflake-secret-arn` (base stack) and `api-url` (API stack). The frontend reads these.
The artifacts bucket is not used by the API.

## Deploy

```bash
cd cdk
npm install
npx cdk deploy IlluminateBase-dev -c environment=dev  # first deploy: base stack
../scripts/set-snowflake-secret.sh dev                 # first deploy, and whenever credentials change
npx cdk deploy IlluminateApi-dev -c environment=dev   # API (code or definition changes)
npx cdk deploy --all -c environment=dev               # both stacks, once the secret is set
npx cdk diff -c environment=dev                       # preview
```

On a first deploy, the secret holds a generated placeholder until the script runs, and the API
cannot reach Snowflake.

`npm run deploy:api` and `npm run deploy:base` wrap `cdk deploy IlluminateApi-*` and
`cdk deploy IlluminateBase-*`.

### After an API deploy: redeploy the frontend infrastructure

CDK sets the Lambda's `ALLOWED_ORIGINS` to fixed defaults:

- `prod`: `https://illuminate.anthology.com`
- other environments: `http://localhost:3000`, `http://localhost:5173` and `https://dm5zbussw00dg.cloudfront.net`

The frontend's infrastructure stack appends its own CloudFront origin to `ALLOWED_ORIGINS` with a
custom resource. **Redeploying this API stack resets `ALLOWED_ORIGINS` to the defaults above**, so
redeploy the frontend infrastructure stack afterwards. Its custom resource runs on every deploy of
that stack. To confirm the origin is present:

```bash
aws lambda get-function-configuration --function-name illuminate-api-dev \
  --query 'Environment.Variables.ALLOWED_ORIGINS' --output text
```

## Environment variables

Set on the Lambda by `cdk/lib/api/lambda-proxy.ts`:

| Variable | Value | Read by |
|----------|-------|---------|
| `USER_POOL_ID` | Base stack user pool | Token validation (JWKS URL, issuer) |
| `USER_POOL_CLIENT_ID` | Base stack app client | Token validation (`aud`) |
| `ALLOWED_ORIGINS` | See above | CORS |
| `CONVERSATION_TABLE` | `illuminate-conversations-<env>` | `conversation_store.py` |
| `OVERLAY_TABLE` | `illuminate-overlays-<env>` | `overlay_store.py` |
| `SNOWFLAKE_SECRET_NAME` | `illuminate/<env>/snowflake` | `snowflake_client.py`, `chat_engine.py` |
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-sonnet-4-6` | `chat_engine.py` |
| `API_DOCS` | `off` | Disables `/docs`, `/redoc`, `/openapi.json` |
| `LOG_LEVEL` | `INFO` (`WARN` in `prod`) | Logging |
| `PORT`, `AWS_LAMBDA_EXEC_WRAPPER`, `AWS_LWA_READINESS_CHECK_PATH`, `AWS_LWA_INVOKE_MODE` | `8080`, `/opt/bootstrap`, `/health`, `response_stream` | Lambda Web Adapter and uvicorn |

Read by the application with defaults, not set by CDK:

| Variable | Default | Effect |
|----------|---------|--------|
| `AWS_REGION` | `us-east-1` (Lambda sets it) | Cognito, Bedrock, DynamoDB, Secrets Manager region |
| `SNOWFLAKE_DATABASE` | The secret's `database` | Database for `{{ database }}` and the chat prompt |
| `CONVERSATION_TTL` | `2592000` (30 days) | Conversation expiry, seconds after last update |
| `CONVERSATION_MAX_MESSAGES` | `50` | Messages kept per conversation |
| `DATA_DICTIONARY_URL` | `https://us.data.api.blackboard.com/api/v1/data/dictionary` | Dictionary used by the `describe_cdm_table` chat tool (the `/api/v1/dictionary/*` routes use a fixed URL) |
| `DATA_DICTIONARY_TIMEOUT` | `15` | Seconds, for that dictionary fetch |

## Secrets

`illuminate/<env>/snowflake` holds JSON with `account`, `user`, `password`, `database`,
`warehouse` and `role`. The base stack creates it with a generated placeholder and never writes it
again, so redeploys leave the credentials alone. To set the credentials from `.env`:

```bash
scripts/set-snowflake-secret.sh dev
```

The Lambda role can only read the secret. Each Lambda instance caches the
credentials and connection after first use, so after changing the secret, new values take effect as
instances are replaced (for example after an API deploy).

## IAM

The Lambda role (`illuminate-lambda-<env>`) has: Bedrock `InvokeModel`/`InvokeModelWithResponseStream` on
Anthropic Claude models and `us.anthropic.claude-*` inference profiles, and `Converse`/`ConverseStream`; `GetItem`/`PutItem`/`DeleteItem` on the conversation table;
`GetItem`/`PutItem`/`DeleteItem`/`Query` on the overlay table; `GetSecretValue` on the Snowflake
secret; `cognito-idp:GetUser`/`AdminGetUser` on the
user pool; and basic Lambda logging.

## Administrators and tenants

Admin routes require membership of `illuminate-admins` and a `custom:tenant_id` on the user:

```bash
aws cognito-idp admin-update-user-attributes --user-pool-id <pool-id> --username <email> \
  --user-attributes Name=custom:tenant_id,Value=<tenant>
aws cognito-idp admin-add-user-to-group --user-pool-id <pool-id> --username <email> \
  --group-name illuminate-admins
```

The change appears in the user's next ID token, so they must sign in again (or refresh their tokens).

## Verify

```bash
API_URL=$(aws ssm get-parameter --name /illuminate/dev/api-url --query Parameter.Value --output text)
curl -s "${API_URL%/}/health"
# {"status":"healthy","version":"0.4.0","mode":"chat_engine"}

curl -N "${API_URL%/}/api/chat/stream" \
  -H "Authorization: Bearer $ID_TOKEN" -H "Content-Type: application/json" \
  -d '{"message": "How many courses are running right now?"}'
```

The stream should emit `status` events and end with a `complete` event.

`cdk/test/check_template.py` checks security properties of the synthesized base stack (writable
attributes, admin group, no VPC or WAF, no Snowflake credentials in the template) and that the API
cannot write the Snowflake secret. Its
docstring has the synth command.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `401` on every route | The client must send the **ID token** for app client `USER_POOL_CLIENT_ID`; access tokens are rejected |
| `403` on admin routes | User is in `illuminate-admins` and has `custom:tenant_id`; sign in again after changes |
| Browser CORS errors after an API deploy | `ALLOWED_ORIGINS` lost the frontend origin; see above |
| Chat returns `502` or `error` events | Bedrock model access in the region; Lambda logs at `/aws/lambda/illuminate-api-<env>` |
| Queries return `502` "The warehouse could not run this query." | Snowflake secret values and the role's grants; the full error is in the Lambda logs |
| Overlays not applied | The admin list shows `status: skipped` with `problems`; the overlay cache is per instance and lasts 60 seconds |

```bash
aws logs tail /aws/lambda/illuminate-api-dev --follow
aws ssm get-parameters-by-path --path /illuminate/dev/
```
