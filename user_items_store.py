"""DynamoDB storage for per-user profile items (cards, queries, layout, prefs, imported flag).

Table keys are tenant_id (HASH) and item_key (RANGE): `user#{sub}#{kind}#{id}` for card and query,
`user#{sub}#{kind}` for singletons. Every item is returned as `{**body, "id": id (card/query only), "version": int, "updated_at": str}`.
"""

from __future__ import annotations

import os
import re
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
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

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
