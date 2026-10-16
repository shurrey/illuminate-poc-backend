"""Tenant registry: one DynamoDB row per customer organization, keyed by tenant_id."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import boto3

from licenses import LICENSES

TABLE_NAME = os.environ.get("TENANT_TABLE", "illuminate-tenants-dev")
NOT_READY = "Your organization is not set up yet"
_CACHE_SECONDS = 60

_table = None
_cache: dict[str, tuple[float, "Tenant"]] = {}


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    name: str
    license: str
    mcp_enabled: bool


class TenantNotReady(Exception):
    """No registry row exists for the tenant, or its license is not a known one."""


def _get_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(TABLE_NAME)
    return _table


def get_tenant(tenant_id: str) -> Tenant:
    """The tenant's registry row, cached per Lambda instance for 60 s. Raises TenantNotReady if absent."""
    now = time.monotonic()
    hit = _cache.get(tenant_id)
    if hit and now - hit[0] <= _CACHE_SECONDS:
        return hit[1]
    item = _get_table().get_item(Key={"tenant_id": tenant_id}).get("Item")
    if not item:
        raise TenantNotReady(tenant_id)
    if item.get("license") not in LICENSES:
        raise TenantNotReady(tenant_id)
    tenant = Tenant(
        tenant_id=item["tenant_id"],
        name=item.get("name", ""),
        license=item["license"],
        mcp_enabled=bool(item.get("mcp_enabled", False)),
    )
    _cache[tenant_id] = (now, tenant)
    return tenant
