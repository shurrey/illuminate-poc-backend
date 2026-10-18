"""DynamoDB storage for per-user profile items (cards, queries, layout, prefs, imported flag).

Table keys are tenant_id (HASH) and item_key (RANGE): `user#{sub}#{kind}#{id}` for card and query,
`user#{sub}#{kind}` for singletons. Every item is returned as `{**body, "id": id (card/query only), "version": int, "updated_at": str}`.
"""

from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

TABLE_NAME = os.environ.get("USER_ITEMS_TABLE", "")
KEYED_KINDS = ("card", "query")
SINGLETON_KINDS = ("layout", "prefs", "imported")
DIRECTORY_KIND = "directory"
DIRECTORY_PREFIX = "directory#"
SHARE_KIND = "share"
SHAREID_KIND = "shareid"
SHARE_ITEM_KINDS = ("query", "card")
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SHARE_ID = re.compile(r"sh_[0-9a-f]{16}")

_table = None


class Conflict(Exception):
    """The item already exists, or changed since the caller read it."""


def _get_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(TABLE_NAME)
    return _table


def _validate_sub(sub: str) -> None:
    if not sub or "#" in sub:
        raise ValueError("invalid sub")


def _key(tenant_id: str, sub: str, kind: str, id: Optional[str]) -> str:
    if not tenant_id:
        raise ValueError("tenant_id is required")
    _validate_sub(sub)
    if kind == DIRECTORY_KIND:
        if id is not None:
            raise ValueError("directory takes no id")
        return f"{DIRECTORY_PREFIX}{sub}"
    if kind in KEYED_KINDS:
        if id is None or not _ID.fullmatch(id):
            raise ValueError("id must match [A-Za-z0-9_-]{1,64}")
        return f"user#{sub}#{kind}#{id}"
    if kind in SINGLETON_KINDS:
        if id is not None:
            raise ValueError(f"{kind} takes no id")
        return f"user#{sub}#{kind}"
    raise ValueError(f"unknown kind {kind!r}")


def _plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _to_dynamo(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dynamo(v) for v in value]
    return value


def _from_item(item: dict) -> dict:
    out = _plain(item["body"])
    if "id" in item:
        out["id"] = item["id"]
    out["version"] = int(item["version"])
    out["updated_at"] = item["updated_at"]
    return out


def _query(tenant_id: str, sub: str, kind: str, **extra) -> list[dict]:
    _key(tenant_id, sub, kind, "x" if kind in KEYED_KINDS else None)
    # Singleton kinds are listed by their exact key; keyed kinds by prefix, which ends in `#` so `u1` never matches `u10`.
    cond = Key("tenant_id").eq(tenant_id) & (
        Key("item_key").begins_with(f"user#{sub}#{kind}#") if kind in KEYED_KINDS else Key("item_key").eq(f"user#{sub}#{kind}")
    )
    items, kwargs = [], {"KeyConditionExpression": cond, "ConsistentRead": True, **extra}
    while True:
        resp = _get_table().query(**kwargs)
        items += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def get(tenant_id: str, sub: str, kind: str, id: Optional[str] = None, *, table=None) -> Optional[dict]:
    item = (table or _get_table()).get_item(Key={"tenant_id": tenant_id, "item_key": _key(tenant_id, sub, kind, id)}, ConsistentRead=True).get("Item")
    return _from_item(item) if item else None


def list_kind(tenant_id: str, sub: str, kind: str) -> list[dict]:
    """The user's items of this kind, in id order."""
    return [_from_item(i) for i in _query(tenant_id, sub, kind)]


def count(tenant_id: str, sub: str, kind: str) -> int:
    return len(_query(tenant_id, sub, kind, ProjectionExpression="item_key"))


def put(tenant_id: str, sub: str, kind: str, id: Optional[str], body: dict, *, expected_version: Optional[int] = None,
        table=None) -> dict:
    """Create (expected_version None, Conflict if it exists) or replace the item when its version matches."""
    item_key = _key(tenant_id, sub, kind, id)
    version = 1 if expected_version is None else expected_version + 1
    updated_at = datetime.now(timezone.utc).isoformat()
    item = {"tenant_id": tenant_id, "item_key": item_key, "kind": kind, "body": body, "version": version, "updated_at": updated_at}
    if id is not None:
        item["id"] = id
    condition = (
        {"ConditionExpression": "attribute_not_exists(item_key)"} if expected_version is None else
        {"ConditionExpression": "version = :expected", "ExpressionAttributeValues": {":expected": expected_version}}
    )
    try:
        (table or _get_table()).put_item(Item=_to_dynamo(item), **condition)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise Conflict(item_key) from e
        raise
    return _from_item(item)


def delete(tenant_id: str, sub: str, kind: str, id: Optional[str] = None) -> bool:
    """True when an item was removed."""
    resp = _get_table().delete_item(Key={"tenant_id": tenant_id, "item_key": _key(tenant_id, sub, kind, id)}, ReturnValues="ALL_OLD")
    return "Attributes" in resp


def list_directory(tenant_id: str) -> list[dict]:
    """Every directory entry in the tenant, `{sub, name, email, version, updated_at}`, in sub order."""
    if not tenant_id:
        raise ValueError("tenant_id is required")
    kwargs = {"KeyConditionExpression": Key("tenant_id").eq(tenant_id) & Key("item_key").begins_with(DIRECTORY_PREFIX),
              "ConsistentRead": True}
    items = []
    while True:
        resp = _get_table().query(**kwargs)
        items += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            return [_from_item(i) for i in items]
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def upsert_directory(tenant_id: str, sub: str, name: str, email: str, *, table=None) -> bool:
    """Write the user's directory entry unless it already holds this name and email. True when written."""
    body = {"sub": sub, "name": name, "email": email}
    current = get(tenant_id, sub, DIRECTORY_KIND, table=table)
    if current and (current["name"], current["email"]) == (name, email):
        return False
    put(tenant_id, sub, DIRECTORY_KIND, None, body, expected_version=current["version"] if current else None, table=table)
    return True


def _share_key(tenant_id: str, owner_sub: str, item_kind: str, item_id: str) -> str:
    if not tenant_id:
        raise ValueError("tenant_id is required")
    _validate_sub(owner_sub)
    if item_kind not in SHARE_ITEM_KINDS:
        raise ValueError(f"cannot share {item_kind!r}")
    if not _ID.fullmatch(item_id or ""):
        raise ValueError("id must match [A-Za-z0-9_-]{1,64}")
    return f"share#{owner_sub}#{item_kind}#{item_id}"


def _shareid_key(share_id: str) -> str:
    if not _SHARE_ID.fullmatch(share_id or ""):
        raise ValueError("invalid share id")
    return f"shareid#{share_id}"


def _share_item(tenant_id: str, item_key: str, kind: str, body: dict, version: int, updated_at: str) -> dict:
    return _to_dynamo({"tenant_id": tenant_id, "item_key": item_key, "kind": kind, "body": body,
                       "version": version, "updated_at": updated_at})


def _transact(items: list[dict], conflict: str) -> None:
    try:
        # The resource's client serialises plain Python values itself.
        _get_table().meta.client.transact_write_items(TransactItems=items)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("TransactionCanceledException", "ConditionalCheckFailedException"):
            raise Conflict(conflict) from e
        raise


def get_share(tenant_id: str, owner_sub: str, item_kind: str, item_id: str) -> Optional[dict]:
    item = _get_table().get_item(
        Key={"tenant_id": tenant_id, "item_key": _share_key(tenant_id, owner_sub, item_kind, item_id)},
        ConsistentRead=True).get("Item")
    return _from_item(item) if item else None


def get_share_pointer(tenant_id: str, share_id: str) -> Optional[dict]:
    """The `{owner_sub, item_kind, item_id}` a share id points at, or None for an unknown or malformed id."""
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if not _SHARE_ID.fullmatch(share_id or ""):
        return None
    item = _get_table().get_item(Key={"tenant_id": tenant_id, "item_key": _shareid_key(share_id)},
                                 ConsistentRead=True).get("Item")
    return _from_item(item) if item else None


def list_shares(tenant_id: str, owner_sub: str) -> list[dict]:
    """All of the owner's shares, in key order."""
    _share_key(tenant_id, owner_sub, "query", "x")
    kwargs = {"KeyConditionExpression": Key("tenant_id").eq(tenant_id) & Key("item_key").begins_with(f"share#{owner_sub}#"),
              "ConsistentRead": True}
    items = []
    while True:
        resp = _get_table().query(**kwargs)
        items += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            return [_from_item(i) for i in items]
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def put_share(tenant_id: str, owner_sub: str, item_kind: str, item_id: str, body: dict) -> dict:
    """Create the share and its pointer in one transaction with a new share id; Conflict if the item is already shared."""
    key = _share_key(tenant_id, owner_sub, item_kind, item_id)
    share_id = f"sh_{secrets.token_hex(8)}"
    now = datetime.now(timezone.utc).isoformat()
    full = {**body, "share_id": share_id, "owner_sub": owner_sub, "item_kind": item_kind, "item_id": item_id,
            "shared_at": now, "updated_at": now}
    pointer = {"owner_sub": owner_sub, "item_kind": item_kind, "item_id": item_id}
    table = _get_table().name
    _transact([
        {"Put": {"TableName": table, "Item": _share_item(tenant_id, key, SHARE_KIND, full, 1, now),
                 "ConditionExpression": "attribute_not_exists(item_key)"}},
        {"Put": {"TableName": table, "Item": _share_item(tenant_id, _shareid_key(share_id), SHAREID_KIND, pointer, 1, now),
                 "ConditionExpression": "attribute_not_exists(item_key)"}},
    ], key)
    return get_share(tenant_id, owner_sub, item_kind, item_id)


def update_share(tenant_id: str, owner_sub: str, item_kind: str, item_id: str, changes: dict, expected_version: int) -> dict:
    """Merge changes into the share, keeping its id and shared_at; Conflict unless the version matches."""
    key = _share_key(tenant_id, owner_sub, item_kind, item_id)
    current = get_share(tenant_id, owner_sub, item_kind, item_id)
    if current is None or current["version"] != expected_version:
        raise Conflict(key)
    body = {k: v for k, v in current.items() if k != "version"}
    body.update({k: v for k, v in changes.items() if k in ("recipients", "tenant")})
    body["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        _get_table().put_item(
            Item=_share_item(tenant_id, key, SHARE_KIND, body, expected_version + 1, body["updated_at"]),
            ConditionExpression="version = :expected", ExpressionAttributeValues={":expected": expected_version})
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise Conflict(key) from e
        raise
    return get_share(tenant_id, owner_sub, item_kind, item_id)


def delete_share(tenant_id: str, owner_sub: str, item_kind: str, item_id: str) -> bool:
    """Delete the share and its pointer in one transaction. True when a share was removed."""
    key = _share_key(tenant_id, owner_sub, item_kind, item_id)
    current = get_share(tenant_id, owner_sub, item_kind, item_id)
    if current is None:
        return False
    table = _get_table().name
    try:
        _transact([
            {"Delete": {"TableName": table, "Key": {"tenant_id": tenant_id, "item_key": key},
                        "ConditionExpression": "attribute_exists(item_key)"}},
            {"Delete": {"TableName": table, "Key": {"tenant_id": tenant_id, "item_key": _shareid_key(current["share_id"])}}},
        ], key)
    except Conflict:
        return False
    return True
