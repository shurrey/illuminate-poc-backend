"""DynamoDB storage for semantic-layer tenant overlays, versioned with history.

Shares the overlay table (tenant_id HASH, metric_id RANGE) with the legacy metric overlays. The
current overlay's sort key is its target (`measure:...`, `filter:...`, `metric:...`); every saved
version is also kept under `<target>#v<version>`. Saves are conditional on the caller's version.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from semantic_layer.overlays import Overlay

TABLE_NAME = os.environ.get("OVERLAY_TABLE", "")
_KINDS = ("measure:", "filter:", "metric:")
_FIELDS = ("target", "expr", "sql", "default_filters", "description", "version", "updated_by", "updated_at")

_table = None


class OverlayConflict(Exception):
    """The overlay changed since the caller read it."""


def _get_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(TABLE_NAME)
    return _table


def _history_key(target: str, version: int) -> str:
    return f"{target}#v{version:06d}"


def _from_item(item: dict) -> Overlay:
    fields = {k: item[k] for k in _FIELDS if item.get(k) is not None}
    fields["version"] = int(fields.get("version", 0))
    return Overlay(**fields)


def _query(tenant_id: str, prefix: str = "") -> list[dict]:
    cond = Key("tenant_id").eq(tenant_id) & Key("metric_id").begins_with(prefix) if prefix else Key("tenant_id").eq(tenant_id)
    items, kwargs = [], {"KeyConditionExpression": cond}
    while True:
        resp = _get_table().query(**kwargs)
        items += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def list_overlays(tenant_id: str) -> list[Overlay]:
    """The tenant's current semantic-layer overlays, in target order."""
    items = [i for i in _query(tenant_id) if i["metric_id"].startswith(_KINDS) and "#" not in i["metric_id"]]
    return [_from_item(i) for i in sorted(items, key=lambda i: i["metric_id"])]


def get_overlay(tenant_id: str, target: str) -> Optional[Overlay]:
    item = _get_table().get_item(Key={"tenant_id": tenant_id, "metric_id": target}).get("Item")
    return _from_item(item) if item else None


def history(tenant_id: str, target: str) -> list[Overlay]:
    """Every saved version, newest first."""
    return sorted((_from_item(i) for i in _query(tenant_id, f"{target}#v")), key=lambda o: -o.version)


def put_overlay(tenant_id: str, overlay: Overlay, updated_by: str, expected_version: int) -> Overlay:
    """Save as the next version; OverlayConflict unless the current version is expected_version (0: none)."""
    previous = history(tenant_id, overlay.target)
    version = (previous[0].version if previous else 0) + 1
    saved = overlay.model_copy(update={
        "version": version, "updated_by": updated_by, "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    item = {k: v for k, v in saved.model_dump().items() if v is not None}
    condition = Attr("metric_id").not_exists() if expected_version == 0 else Attr("version").eq(expected_version)
    try:
        _get_table().put_item(Item={**item, "tenant_id": tenant_id, "metric_id": overlay.target}, ConditionExpression=condition)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise OverlayConflict(overlay.target) from e
        raise
    _get_table().put_item(Item={**item, "tenant_id": tenant_id, "metric_id": _history_key(overlay.target, version)})
    return saved


def revert(tenant_id: str, target: str, version: int, updated_by: str, expected_version: int) -> Overlay:
    """Save an earlier version's content as the newest version; KeyError when that version doesn't exist."""
    item = _get_table().get_item(Key={"tenant_id": tenant_id, "metric_id": _history_key(target, version)}).get("Item")
    if not item:
        raise KeyError(f"{target} has no version {version}")
    return put_overlay(tenant_id, _from_item(item), updated_by, expected_version)


def delete_overlay(tenant_id: str, target: str) -> None:
    """Remove the current overlay; its history stays so it can be reverted to."""
    _get_table().delete_item(Key={"tenant_id": tenant_id, "metric_id": target})
