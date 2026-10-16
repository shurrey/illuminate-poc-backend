#!/usr/bin/env python3
"""Tenant administration CLI: `migrate-current` and `list`. Uses AWS_PROFILE credentials; prints counts, never secrets."""
import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tenant_admin  # noqa: E402


def _clients(env: str, user_pool_id: str | None = None, *, need_pool: bool = True):
    # Corporate root CAs live in the system trust store, not certifi; patch ssl before any client builds a context.
    import truststore

    truststore.inject_into_ssl()
    import boto3

    session = boto3.Session()
    pool = user_pool_id
    if need_pool and not pool:
        pool = session.client("ssm").get_parameter(Name=f"/illuminate/{env}/cognito-pool-id")["Parameter"]["Value"]
    return session.resource("dynamodb"), session.client("secretsmanager"), session.client("cognito-idp"), pool


def _json_default(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral() else float(o)
    raise TypeError(type(o))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tenant")
    parser.add_argument("--env", default="dev")
    sub = parser.add_subparsers(dest="command", required=True)
    migrate = sub.add_parser("migrate-current", help="move today's deployment into the first tenant")
    migrate.add_argument("--old-tenant-id", default="blackboard-dev")
    migrate.add_argument("--name", default="Northbridge University")
    migrate.add_argument("--license", default="premium", choices=["core", "premium"])
    migrate.add_argument("--user-pool-id")
    migrate.add_argument("--dry-run", action="store_true")
    sub.add_parser("list", help="list registered tenants")
    args = parser.parse_args(argv)

    if args.command == "list":
        dynamodb, _, _, _ = _clients(args.env, need_pool=False)
        result = tenant_admin.list_tenants(dynamodb=dynamodb, env=args.env)
    else:
        dynamodb, secrets, cognito, pool = _clients(args.env, args.user_pool_id)
        try:
            result = tenant_admin.migrate_current(
                dynamodb=dynamodb, secrets=secrets, cognito=cognito, user_pool_id=pool, env=args.env,
                old_tenant_id=args.old_tenant_id, name=args.name, license=args.license, dry_run=args.dry_run)
        except tenant_admin.SourceSecretMissing as exc:
            print(f"{exc} Nothing was written.", file=sys.stderr)
            return 1
    print(json.dumps(result, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    sys.exit(main())
