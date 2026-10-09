#!/bin/bash
# set-snowflake-secret.sh [environment] : write the SNOWFLAKE_* values from .env to the
# illuminate/<environment>/snowflake secret. Warm Lambdas keep the old credentials until they recycle.
set -euo pipefail
ENVIRONMENT="${1:-dev}"
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
aws secretsmanager put-secret-value --secret-id "illuminate/${ENVIRONMENT}/snowflake" \
  --secret-string "$SECRET" --query VersionId --output text
