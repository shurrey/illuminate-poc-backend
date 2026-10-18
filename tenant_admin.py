"""Operator-side tenant administration: migrate the original deployment into a registry tenant, and list tenants.

Every function takes its boto3 clients as parameters. Each step is idempotent, so a failed run is finished by running it again.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from licenses import LICENSES
from roles import GROUP_ROLES


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


def _record_in_directory(dynamodb, env: str, tenant_id: str, attributes: list[dict]) -> None:
    """Write the user's directory entry from their Cognito attributes; the name defaults to the email's local part."""
    import user_items_store

    attrs = {a["Name"]: a["Value"] for a in attributes}
    sub, email = attrs.get("sub"), attrs.get("email")
    if not (sub and email):
        return
    user_items_store.upsert_directory(tenant_id, sub, attrs.get("name") or email.split("@")[0], email,
                                      table=dynamodb.Table(f"illuminate-user-items-{env}"))


def _is_code(exc: ClientError, *codes: str) -> bool:
    return exc.response["Error"]["Code"] in codes


class SourceSecretMissing(Exception):
    """The deployment's original Snowflake secret is gone and the tenant has no secret of its own."""


class SecretUnusable(ValueError):
    """A Snowflake secret that cannot be read or is not a complete connection; the message names no secret value."""

    def __init__(self, secret_id: str, reason: str):
        super().__init__(f"The secret {secret_id} cannot be used: {reason}.")
        self.reason = reason


_UNREADABLE_SECRET_CODES = ("ResourceNotFoundException", "AccessDeniedException", "InvalidRequestException",
                            "DecryptionFailure")


def _read_connection(secrets, secret_id: str) -> str:
    """The secret's value when it is a complete Snowflake connection, else SecretUnusable."""
    try:
        response = secrets.get_secret_value(SecretId=secret_id)
    except ClientError as e:
        if _is_code(e, *_UNREADABLE_SECRET_CODES):
            raise SecretUnusable(secret_id, e.response["Error"]["Code"]) from e
        raise
    if "SecretString" not in response:
        raise SecretUnusable(secret_id, "binary secret (no SecretString)")
    try:
        connection = json.loads(response["SecretString"])
    except ValueError:
        raise SecretUnusable(secret_id, "malformed JSON") from None
    if not isinstance(connection, dict):
        raise SecretUnusable(secret_id, "not a JSON object")
    missing = [k for k in REQUIRED_CONNECTION_KEYS if not connection.get(k)]
    if missing:
        raise SecretUnusable(secret_id, f"missing {', '.join(missing)}")
    return response["SecretString"]


def _secret_state(secrets, secret_id: str) -> str:
    """'missing', 'deleting' (scheduled for deletion, so unreadable) or 'present'."""
    try:
        described = secrets.describe_secret(SecretId=secret_id)
    except ClientError as e:
        if _is_code(e, "ResourceNotFoundException"):
            return "missing"
        if _is_code(e, "AccessDeniedException"):
            raise SecretUnusable(secret_id, "AccessDeniedException") from e
        raise
    return "deleting" if described.get("DeletedDate") else "present"


def _secret_exists(secrets, secret_id: str) -> bool:
    return _secret_state(secrets, secret_id) == "present"


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
    try:
        _read_connection(secrets, source)
    except SecretUnusable as e:
        raise SourceSecretMissing(f"The source secret {source} cannot be used ({e.reason}) "
                                  "and the tenant has no secret of its own.") from e


def _find_or_create_tenant(dynamodb, env, old_tenant_id, name, license, dry_run) -> tuple[str, int]:
    _check_license(license)
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
    state = _secret_state(secrets, target)
    if state == "deleting":
        raise ValueError(f"The secret {target} is scheduled for deletion; restore it or set a new connection first.")
    if state == "present":
        return 0
    if not dry_run:
        secrets.create_secret(Name=target, SecretString=_read_connection(secrets, f"illuminate/{env}/snowflake"))
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
    if not dry_run:
        # Every member of the tenant, not just those moved now, so a rerun after a failure completes the directory.
        for user in users:
            if _attr(user, "custom:tenant_id") == tenant_id:
                _record_in_directory(dynamodb, env, tenant_id, user["Attributes"])
    conversations, orphaned, other_tenant, ownerless = _rekey_conversations(dynamodb, env, users, tenant_id, dry_run)
    return {
        "tenant_id": tenant_id, "created": created, "secret_copied": secret_copied, "users": moved_users,
        "overlays": overlays, "conflicts": conflicts, "conversations": conversations,
        "orphaned": orphaned, "other_tenant": other_tenant, "ownerless": ownerless,
    }


def list_tenants(*, dynamodb, env: str) -> list[dict]:
    """The registry rows, by name. Secrets are never read."""
    return sorted(_scan(dynamodb.Table(f"illuminate-tenants-{env}")), key=lambda r: r.get("name", ""))


REQUIRED_CONNECTION_KEYS = ("account", "user", "password", "database", "warehouse", "role")
ROLE_GROUPS = {role.capitalize(): group for group, role in GROUP_ROLES.items()} | {"Viewer": None}


def _check_license(license: str) -> None:
    if license not in LICENSES:
        raise ValueError(f"Unknown license {license!r}; expected one of {sorted(LICENSES)}.")


class UserPhaseFailed(Exception):
    """Creating users failed after the tenant was written; carries what exists so the operator can finish with add-users."""

    def __init__(self, tenant_id: str, users_created: list[str], cause: Exception):
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.tenant_id, self.users_created, self.cause = tenant_id, users_created, cause


def _check_users(users, send_email: bool, temporary_password: str | None) -> list[tuple[str, str]]:
    """Returns the users with emails stripped and lower-cased; raises ValueError on a blank or invalid email or unknown role."""
    users = [(email.strip().lower(), role.strip()) for email, role in users]
    invalid = [e for e, _ in users if not re.fullmatch(r"[^@\s]+@[^@\s]+", e)]
    if invalid:
        raise ValueError(f"Invalid email address: {invalid}.")
    bad = sorted({role for _, role in users if role not in ROLE_GROUPS})
    if bad:
        raise ValueError(f"Unknown role {bad}; expected one of {sorted(ROLE_GROUPS)}.")
    if not send_email and not temporary_password:
        raise ValueError("A temporary password is required when the invitation email is suppressed.")
    return users


def _secret_id(tenant_id: str) -> str:
    return f"illuminate/tenants/{tenant_id}/snowflake"


def _connection_string(dynamodb, secrets, env, connection: dict | None, copy_connection_from: str | None) -> str:
    """The secret value to store: the given connection JSON, or another tenant's secret read server-side."""
    if (connection is None) == (copy_connection_from is None):
        raise ValueError("Give exactly one of connection or copy_connection_from.")
    if connection is not None:
        missing = [k for k in REQUIRED_CONNECTION_KEYS if not connection.get(k)]
        if missing:
            raise ValueError(f"The connection is missing: {', '.join(missing)}.")
        return json.dumps(connection)
    _tenant_row(dynamodb, env, copy_connection_from)
    try:
        return _read_connection(secrets, _secret_id(copy_connection_from))
    except SecretUnusable as e:
        if e.reason == "ResourceNotFoundException":
            raise ValueError(f"Tenant {copy_connection_from} has no Snowflake secret to copy.") from e
        raise


def _put_secret(secrets, tenant_id: str, value: str) -> None:
    try:
        secrets.create_secret(Name=_secret_id(tenant_id), SecretString=value)
    except ClientError as e:
        if not _is_code(e, "ResourceExistsException"):
            raise
        secrets.put_secret_value(SecretId=_secret_id(tenant_id), SecretString=value)


def _tenant_row(dynamodb, env: str, tenant_id: str) -> dict:
    item = dynamodb.Table(f"illuminate-tenants-{env}").get_item(Key={"tenant_id": tenant_id}, ConsistentRead=True).get("Item")
    if not item:
        raise ValueError(f"Tenant {tenant_id} does not exist.")
    return item


def _find_user(cognito, user_pool_id: str, email: str) -> dict | None:
    try:
        return cognito.admin_get_user(UserPoolId=user_pool_id, Username=email)
    except ClientError as e:
        if _is_code(e, "UserNotFoundException"):
            return None
        raise


def add_users(*, dynamodb, env: str, cognito, user_pool_id: str, tenant_id: str, users, send_email: bool = True,
              temporary_password: str | None = None, _created: list[str] | None = None) -> dict:
    """Create each (email, role) user in the tenant; returns {users_created, users_refused}.

    An email that exists with another tenant or none is refused and left unchanged.
    An existing user of this tenant only gains the role's group if missing. The tenant, roles and emails are checked first.
    """
    users = _check_users(users, send_email, temporary_password)
    _tenant_row(dynamodb, env, tenant_id)
    created = _created if _created is not None else []
    refused = []
    for email, role in users:
        group = ROLE_GROUPS[role]
        existing = _find_user(cognito, user_pool_id, email)
        if existing is not None:
            if _attr({"Attributes": existing["UserAttributes"]}, "custom:tenant_id") != tenant_id:
                refused.append(email)
                continue
        else:
            kwargs = {"MessageAction": "SUPPRESS", "TemporaryPassword": temporary_password} if not send_email else {}
            cognito.admin_create_user(
                UserPoolId=user_pool_id, Username=email, **kwargs,
                UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"},
                                {"Name": "custom:tenant_id", "Value": tenant_id}])
            created.append(email)
            existing = cognito.admin_get_user(UserPoolId=user_pool_id, Username=email)
        _record_in_directory(dynamodb, env, tenant_id, existing["UserAttributes"])
        if group:
            current = cognito.admin_list_groups_for_user(UserPoolId=user_pool_id, Username=email)["Groups"]
            if group not in {g["GroupName"] for g in current}:
                cognito.admin_add_user_to_group(UserPoolId=user_pool_id, Username=email, GroupName=group)
    return {"users_created": created, "users_refused": refused}


def create_tenant(*, dynamodb, secrets, cognito, user_pool_id: str, env: str, name: str, license: str,
                  connection: dict | None = None, copy_connection_from: str | None = None, users=(),
                  send_email: bool = True, temporary_password: str | None = None) -> dict:
    """Create a tenant with its Snowflake secret and users; returns {tenant_id, users_created, users_refused}.

    The registry row is written after the secret, so a failure never leaves a tenant without one. Raises
    UserPhaseFailed, carrying the tenant_id, if creating users fails once the tenant exists.
    """
    _check_license(license)
    users = _check_users(users, send_email, temporary_password)
    value = _connection_string(dynamodb, secrets, env, connection, copy_connection_from)
    tenant_id = str(uuid.uuid4())
    _put_secret(secrets, tenant_id, value)
    now = datetime.now(timezone.utc).isoformat()
    dynamodb.Table(f"illuminate-tenants-{env}").put_item(Item={
        "tenant_id": tenant_id, "name": name, "license": license, "mcp_enabled": False,
        "created_at": now, "updated_at": now})
    created: list[str] = []
    try:
        result = add_users(dynamodb=dynamodb, env=env, cognito=cognito, user_pool_id=user_pool_id, tenant_id=tenant_id,
                           users=users, send_email=send_email, temporary_password=temporary_password, _created=created)
    except Exception as exc:
        raise UserPhaseFailed(tenant_id, created, exc) from exc
    return {"tenant_id": tenant_id, **result}


def set_connection(*, dynamodb, secrets, env: str, tenant_id: str, connection: dict | None = None,
                   copy_connection_from: str | None = None) -> None:
    """Replace the tenant's Snowflake secret. Warm Lambdas pick it up within the credential cache TTL."""
    _tenant_row(dynamodb, env, tenant_id)
    _put_secret(secrets, tenant_id, _connection_string(dynamodb, secrets, env, connection, copy_connection_from))


def set_license(*, dynamodb, env: str, tenant_id: str, license: str) -> None:
    """Change the tenant's license. Warm Lambdas pick it up within the registry cache (60 s)."""
    _check_license(license)
    try:
        dynamodb.Table(f"illuminate-tenants-{env}").update_item(
            Key={"tenant_id": tenant_id}, UpdateExpression="SET license = :l, updated_at = :u",
            ConditionExpression="attribute_exists(tenant_id)",
            ExpressionAttributeValues={":l": license, ":u": datetime.now(timezone.utc).isoformat()})
    except ClientError as e:
        if _is_code(e, "ConditionalCheckFailedException"):
            raise ValueError(f"Tenant {tenant_id} does not exist.") from e
        raise
