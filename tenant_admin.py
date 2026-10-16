"""Operator-side tenant administration: migrate the original deployment into a registry tenant, and list tenants.

Every function takes its boto3 clients as parameters. Each step is idempotent, so a failed run is finished by running it again.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from botocore.exceptions import ClientError


def _scan(table, **kwargs) -> list[dict]:
    items = []
    while True:
        resp = table.scan(**kwargs)
        items += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _query_partition(table, tenant_id: str) -> list[dict]:
    from boto3.dynamodb.conditions import Key

    items, kwargs = [], {"KeyConditionExpression": Key("tenant_id").eq(tenant_id), "ConsistentRead": True}
    while True:
        resp = table.query(**kwargs)
        items += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _list_users(cognito, user_pool_id: str) -> list[dict]:
    users, kwargs = [], {"UserPoolId": user_pool_id}
    while True:
        resp = cognito.list_users(**kwargs)
        users += resp.get("Users", [])
        if not resp.get("PaginationToken"):
            return users
        kwargs["PaginationToken"] = resp["PaginationToken"]


def _attr(user: dict, name: str):
    return next((a["Value"] for a in user.get("Attributes", []) if a["Name"] == name), None)


def _is_code(exc: ClientError, *codes: str) -> bool:
    return exc.response["Error"]["Code"] in codes


class SourceSecretMissing(Exception):
    """The deployment's original Snowflake secret is gone and the tenant has no secret of its own."""


def _secret_exists(secrets, secret_id: str) -> bool:
    try:
        secrets.describe_secret(SecretId=secret_id)
        return True
    except ClientError as e:
        if _is_code(e, "ResourceNotFoundException"):
            return False
        raise


def _find_tenant(dynamodb, env, old_tenant_id) -> str | None:
    from boto3.dynamodb.conditions import Attr

    found = _scan(dynamodb.Table(f"illuminate-tenants-{env}"),
                  FilterExpression=Attr("migrated_from").eq(old_tenant_id), ConsistentRead=True)
    return found[0]["tenant_id"] if found else None


def _require_a_secret_to_use(dynamodb, secrets, env, old_tenant_id) -> None:
    """Raise SourceSecretMissing unless the tenant's own secret or the source secret exists, before anything is written."""
    existing = _find_tenant(dynamodb, env, old_tenant_id)
    if existing and _secret_exists(secrets, f"illuminate/tenants/{existing}/snowflake"):
        return
    source = f"illuminate/{env}/snowflake"
    if not _secret_exists(secrets, source):
        raise SourceSecretMissing(f"The source secret {source} does not exist and the tenant has no secret of its own.")


def _find_or_create_tenant(dynamodb, env, old_tenant_id, name, license, dry_run) -> tuple[str, int]:
    table = dynamodb.Table(f"illuminate-tenants-{env}")
    existing = _find_tenant(dynamodb, env, old_tenant_id)
    if existing:
        return existing, 0
    tenant_id = str(uuid.uuid4())
    if not dry_run:
        now = datetime.now(timezone.utc).isoformat()
        table.put_item(Item={
            "tenant_id": tenant_id, "name": name, "license": license, "mcp_enabled": False,
            "migrated_from": old_tenant_id, "created_at": now, "updated_at": now,
        })
    return tenant_id, 1


def _copy_secret(secrets, env, tenant_id, dry_run) -> int:
    target = f"illuminate/tenants/{tenant_id}/snowflake"
    try:
        secrets.describe_secret(SecretId=target)
        return 0
    except ClientError as e:
        if not _is_code(e, "ResourceNotFoundException"):
            raise
    if not dry_run:
        value = secrets.get_secret_value(SecretId=f"illuminate/{env}/snowflake")["SecretString"]
        secrets.create_secret(Name=target, SecretString=value)
    return 1


def _move_users(cognito, user_pool_id, users, old_tenant_id, tenant_id, dry_run) -> int:
    moved = 0
    for user in users:
        if _attr(user, "custom:tenant_id") != old_tenant_id:
            continue
        if not dry_run:
            cognito.admin_update_user_attributes(
                UserPoolId=user_pool_id, Username=user["Username"],
                UserAttributes=[{"Name": "custom:tenant_id", "Value": tenant_id}])
        # Later steps read the new tenant from here, including on a dry run.
        user["Attributes"] = [a for a in user["Attributes"] if a["Name"] != "custom:tenant_id"] + [
            {"Name": "custom:tenant_id", "Value": tenant_id}]
        moved += 1
    return moved


def _move_overlays(dynamodb, env, old_tenant_id, tenant_id, dry_run) -> tuple[int, int]:
    """Copy each row to the new partition, then delete the original. Returns (moved, conflicts).

    A row whose target already holds different content is left in place: the new tenant changed it since the copy.
    """
    table = dynamodb.Table(f"illuminate-overlays-{env}")
    moved = conflicts = 0
    for item in _query_partition(table, old_tenant_id):
        target = {**item, "tenant_id": tenant_id}
        key = {"tenant_id": tenant_id, "metric_id": item["metric_id"]}
        existing = None
        if dry_run:
            existing = table.get_item(Key=key, ConsistentRead=True).get("Item")
        else:
            try:
                table.put_item(Item=target, ConditionExpression="attribute_not_exists(metric_id)")
            except ClientError as e:
                if not _is_code(e, "ConditionalCheckFailedException"):
                    raise
                existing = table.get_item(Key=key, ConsistentRead=True).get("Item")
        if existing is not None and existing != target:
            conflicts += 1
            continue
        if not dry_run:
            table.delete_item(Key={"tenant_id": old_tenant_id, "metric_id": item["metric_id"]})
        moved += 1
    return moved, conflicts


def _rekey_conversations(dynamodb, env, users, tenant_id, dry_run) -> tuple[int, int, int, int]:
    table = dynamodb.Table(f"illuminate-conversations-{env}")
    tenant_by_sub = {_attr(u, "sub"): _attr(u, "custom:tenant_id") for u in users}
    moved = orphaned = other_tenant = ownerless = 0
    for item in _scan(table):
        owner = item.get("owner_sub")
        if owner is None:
            ownerless += 1
        elif "#" in owner:
            continue
        elif owner not in tenant_by_sub:
            orphaned += 1
        elif tenant_by_sub[owner] != tenant_id:
            other_tenant += 1
        else:
            if not dry_run:
                try:
                    table.update_item(
                        Key={"context_id": item["context_id"]},
                        UpdateExpression="SET owner_sub = :new",
                        ConditionExpression="owner_sub = :old",
                        ExpressionAttributeValues={":new": f"{tenant_id}#{owner}", ":old": owner})
                except ClientError as e:
                    if not _is_code(e, "ConditionalCheckFailedException"):
                        raise
                    continue
            moved += 1
    return moved, orphaned, other_tenant, ownerless


def migrate_current(*, dynamodb, secrets, cognito, user_pool_id: str, env: str, old_tenant_id: str = "blackboard-dev",
                    name: str = "Northbridge University", license: str = "premium", dry_run: bool = False) -> dict:
    """Move the original single-tenant deployment into a registry tenant; returns counts of what changed.

    dynamodb is a boto3 resource; secrets and cognito are clients. dry_run reports the counts and writes nothing.
    Raises SourceSecretMissing, having written nothing, when there is no secret to give the tenant.
    """
    _require_a_secret_to_use(dynamodb, secrets, env, old_tenant_id)
    tenant_id, created = _find_or_create_tenant(dynamodb, env, old_tenant_id, name, license, dry_run)
    secret_copied = _copy_secret(secrets, env, tenant_id, dry_run)
    users = _list_users(cognito, user_pool_id)
    # Overlays first, so no user lands on a tenant whose overlays are only partly copied.
    overlays, conflicts = _move_overlays(dynamodb, env, old_tenant_id, tenant_id, dry_run)
    moved_users = _move_users(cognito, user_pool_id, users, old_tenant_id, tenant_id, dry_run)
    conversations, orphaned, other_tenant, ownerless = _rekey_conversations(dynamodb, env, users, tenant_id, dry_run)
    return {
        "tenant_id": tenant_id, "created": created, "secret_copied": secret_copied, "users": moved_users,
        "overlays": overlays, "conflicts": conflicts, "conversations": conversations,
        "orphaned": orphaned, "other_tenant": other_tenant, "ownerless": ownerless,
    }


def list_tenants(*, dynamodb, env: str) -> list[dict]:
    """The registry rows, by name. Secrets are never read."""
    return sorted(_scan(dynamodb.Table(f"illuminate-tenants-{env}")), key=lambda r: r.get("name", ""))
