#!/usr/bin/env python3
"""Tenant administration CLI: migrate-current, list, create, add-users, set-connection, set-license.

Uses AWS_PROFILE credentials; prints counts and ids, never secrets or passwords.
"""
import argparse
import csv
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tenant_admin  # noqa: E402
from licenses import LICENSES  # noqa: E402


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


def _read_users(path: str) -> list[tuple[str, str]]:
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if [h.strip() for h in reader.fieldnames or []] != ["email", "role"]:
            raise ValueError(f"{path}: the header must be exactly: email,role")
        return [((row["email"] or "").strip().lower(), (row["role"] or "").strip()) for row in reader]


def _read_connection(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def _add_user_options(p):
    p.add_argument("--users", help="CSV file with header email,role")
    p.add_argument("--no-email", action="store_true",
                   help="suppress Cognito's invitation; the password comes from ILLUMINATE_TEMP_PASSWORD")


def _add_connection_options(p):
    p.add_argument("--snowflake", help="JSON file with account, user, password, database, warehouse, role")
    p.add_argument("--copy-connection-from", help="tenant id whose Snowflake secret to copy")


def _user_kwargs(args) -> dict:
    password = os.environ.get("ILLUMINATE_TEMP_PASSWORD") if args.no_email else None
    return {"users": _read_users(args.users) if args.users else [], "send_email": not args.no_email,
            "temporary_password": password}


def _run(args) -> dict | list:
    if args.command == "list":
        dynamodb, _, _, _ = _clients(args.env, need_pool=False)
        return tenant_admin.list_tenants(dynamodb=dynamodb, env=args.env)
    if args.command == "set-license":
        dynamodb, _, _, _ = _clients(args.env, need_pool=False)
        tenant_admin.set_license(dynamodb=dynamodb, env=args.env, tenant_id=args.tenant_id, license=args.license)
        return {"tenant_id": args.tenant_id, "license": args.license}
    if args.command == "set-connection":
        dynamodb, secrets, _, _ = _clients(args.env, need_pool=False)
        tenant_admin.set_connection(
            dynamodb=dynamodb, secrets=secrets, env=args.env, tenant_id=args.tenant_id,
            connection=_read_connection(args.snowflake) if args.snowflake else None,
            copy_connection_from=args.copy_connection_from)
        return {"tenant_id": args.tenant_id, "connection": "updated"}
    dynamodb, secrets, cognito, pool = _clients(args.env, args.user_pool_id)
    if args.command == "create":
        return tenant_admin.create_tenant(
            dynamodb=dynamodb, secrets=secrets, cognito=cognito, user_pool_id=pool, env=args.env,
            name=args.name, license=args.license,
            connection=_read_connection(args.snowflake) if args.snowflake else None,
            copy_connection_from=args.copy_connection_from, **_user_kwargs(args))
    if args.command == "add-users":
        return tenant_admin.add_users(dynamodb=dynamodb, env=args.env, cognito=cognito, user_pool_id=pool, tenant_id=args.tenant_id, **_user_kwargs(args))
    return tenant_admin.migrate_current(
        dynamodb=dynamodb, secrets=secrets, cognito=cognito, user_pool_id=pool, env=args.env,
        old_tenant_id=args.old_tenant_id, name=args.name, license=args.license, dry_run=args.dry_run)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tenant")
    parser.add_argument("--env", default="dev")
    sub = parser.add_subparsers(dest="command", required=True)
    migrate = sub.add_parser("migrate-current", help="move today's deployment into the first tenant")
    migrate.add_argument("--old-tenant-id", default="blackboard-dev")
    migrate.add_argument("--name", default="Northbridge University")
    migrate.add_argument("--license", default="premium", choices=sorted(LICENSES))
    migrate.add_argument("--user-pool-id")
    migrate.add_argument("--dry-run", action="store_true")
    sub.add_parser("list", help="list registered tenants")
    create = sub.add_parser("create", help="create a tenant with its connection and users")
    create.add_argument("--name", required=True)
    create.add_argument("--license", required=True, choices=sorted(LICENSES))
    create.add_argument("--user-pool-id")
    _add_connection_options(create)
    _add_user_options(create)
    add = sub.add_parser("add-users", help="add users to a tenant")
    add.add_argument("tenant_id")
    add.add_argument("--user-pool-id")
    _add_user_options(add)
    setconn = sub.add_parser("set-connection", help="replace a tenant's Snowflake connection")
    setconn.add_argument("tenant_id")
    _add_connection_options(setconn)
    setlic = sub.add_parser("set-license", help="change a tenant's license")
    setlic.add_argument("tenant_id")
    setlic.add_argument("license", choices=sorted(LICENSES))
    args = parser.parse_args(argv)

    try:
        result = _run(args)
    except tenant_admin.SourceSecretMissing as exc:
        print(f"{exc} Nothing was written.", file=sys.stderr)
        return 1
    except tenant_admin.UserPhaseFailed as exc:
        print(json.dumps({"tenant_id": exc.tenant_id, "users_created": exc.users_created}, indent=2))
        print(f"The tenant exists but creating its users failed ({exc}). "
              f"Finish with: tenant.py add-users {exc.tenant_id} --users <csv>", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    sys.exit(main())
