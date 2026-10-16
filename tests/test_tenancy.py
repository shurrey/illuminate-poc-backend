import pytest
from fastapi import HTTPException

import lambda_handler
import snowflake_client
import tenancy
from tenancy import NOT_READY, Tenant, TenantNotReady


class FakeTable:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def get_item(self, Key, **_):
        self.calls += 1
        row = self.rows.get(Key["tenant_id"])
        return {"Item": row} if row else {}


@pytest.fixture
def table(monkeypatch):
    fake = FakeTable({"t1": {"tenant_id": "t1", "name": "Acme", "license": "pro", "mcp_enabled": True}})
    monkeypatch.setattr(tenancy, "_table", fake)
    monkeypatch.setattr(snowflake_client, "tenant_database", lambda t: f"DB_{t.tenant_id}")
    tenancy._cache.clear()
    yield fake
    tenancy._cache.clear()


def test_get_tenant_returns_row(table):
    assert tenancy.get_tenant("t1") == Tenant("t1", "Acme", "pro", True)


def test_get_tenant_defaults_mcp_enabled_to_false(table):
    table.rows["t2"] = {"tenant_id": "t2", "name": "Beta", "license": "basic"}
    assert tenancy.get_tenant("t2").mcp_enabled is False


def test_get_tenant_missing_raises(table):
    with pytest.raises(TenantNotReady):
        tenancy.get_tenant("nope")


def test_get_tenant_caches_for_60_seconds(table, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(tenancy.time, "monotonic", lambda: now[0])
    tenancy.get_tenant("t1")
    now[0] += 59
    tenancy.get_tenant("t1")
    assert table.calls == 1
    now[0] += 2
    tenancy.get_tenant("t1")
    assert table.calls == 2


def test_tenant_for_returns_the_callers_tenant(table):
    assert lambda_handler.tenant_for({"custom:tenant_id": "t1"}).name == "Acme"


@pytest.mark.parametrize("user", [None, {}, {"sub": "u1"}])
def test_tenant_for_without_claim_is_403(table, user):
    with pytest.raises(HTTPException) as exc:
        lambda_handler.tenant_for(user)
    assert exc.value.status_code == 403
    assert exc.value.detail == NOT_READY


def test_tenant_for_unknown_tenant_is_403(table):
    with pytest.raises(HTTPException) as exc:
        lambda_handler.tenant_for({"custom:tenant_id": "ghost"})
    assert exc.value.status_code == 403
    assert exc.value.detail == NOT_READY


def test_tenant_for_without_snowflake_secret_is_403(table, monkeypatch):
    def not_ready(tenant):
        raise TenantNotReady(tenant.tenant_id)

    monkeypatch.setattr(snowflake_client, "tenant_database", not_ready)
    with pytest.raises(HTTPException) as exc:
        lambda_handler.tenant_for({"custom:tenant_id": "t1"})
    assert exc.value.status_code == 403
    assert exc.value.detail == NOT_READY
