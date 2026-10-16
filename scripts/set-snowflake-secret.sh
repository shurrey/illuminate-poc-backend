#!/bin/bash
# set-snowflake-secret.sh <tenant_id> : write the SNOWFLAKE_* values from .env to the tenant's secret,
# illuminate/tenants/<tenant_id>/snowflake, creating it if missing. Warm Lambdas keep the old credentials until they recycle.
set -euo pipefail
TENANT_ID="${1:?usage: set-snowflake-secret.sh <tenant_id>}"
SECRET_ID="illuminate/tenants/${TENANT_ID}/snowflake"
cd "$(dirname "$0")/.."
# .env is raw KEY=VALUE (no quoting), parsed the same way as cdk/bin/illuminate.ts.
SECRET=$(python3 - <<'PY'
import json, sys
env = {}
for line in open(".env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        env[key] = value
fields = {"account": "", "user": "SVC_BLACKBOARD_DATA", "password": "", "database": "",
          "warehouse": "BLACKBOARD_DATA_WH", "role": "BBDATA_USER_ROLE"}
secret = {k: env.get("SNOWFLAKE_" + k.upper()) or d for k, d in fields.items()}
missing = [k for k in ("account", "password", "database") if not secret[k]]
if missing:
    sys.exit("missing in .env: " + ", ".join("SNOWFLAKE_" + k.upper() for k in missing))
print(json.dumps(secret))
PY
)
if ! DESCRIBE_ERR=$(aws secretsmanager describe-secret --secret-id "$SECRET_ID" 2>&1 >/dev/null); then
  case "$DESCRIBE_ERR" in
    *ResourceNotFoundException*) EXISTS=0 ;;
    *) echo "$DESCRIBE_ERR" >&2; exit 1 ;;
  esac
else
  EXISTS=1
fi
if [ "$EXISTS" = 1 ]; then
  aws secretsmanager put-secret-value --secret-id "$SECRET_ID" \
    --secret-string "$SECRET" --query VersionId --output text
else
  aws secretsmanager create-secret --name "$SECRET_ID" \
    --secret-string "$SECRET" --query ARN --output text
fi
