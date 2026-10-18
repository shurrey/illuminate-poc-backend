import json

import pytest
from fastapi.testclient import TestClient

import lambda_handler
import mcp_access
import tenancy
from tenancy import Tenant
from tests.tenant_fakes import TENANT_A, TENANT_B, install_tenants, user_of

AUTH = {"Authorization": "Bearer test"}
ADMIN = user_of(TENANT_A, "illuminate-admins", sub="a1")
AUTHOR = user_of(TENANT_A, "illuminate-authors", sub="a2")
VIEWER = user_of(TENANT_A, sub="a3")
SETTING = "/api/v1/admin/settings/mcp"
SETUP = "/api/v1/admin/mcp/setup"
URL = "https://mcp.example.com/mcp"


class FakeTable:
    def __init__(self):
        self.updates = []

    def update_item(self, **kw):
        self.updates.append(kw)

    def get_item(self, Key, **_):
        return {"Item": {"tenant_id": Key["tenant_id"], "name": "Alpha", "license": "premium", "mcp_enabled": True}}


@pytest.fixture
def client(monkeypatch):
    install_tenants(monkeypatch, TENANT_A, TENANT_B, database="TESTDB")
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: ADMIN if a else None)
    monkeypatch.setattr(mcp_access, "_mcp_discovery", lambda: ("cid", URL))
    return TestClient(lambda_handler.app)


def _as(monkeypatch, user):
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user if a else None)


def test_may_use_mcp_follows_the_tenant_flag():
    on = Tenant("t", "T", "premium", True)
    off = Tenant("t", "T", "premium", False)
    assert mcp_access.may_use_mcp(on, {"sub": "u"}) is True
    assert mcp_access.may_use_mcp(off, {"sub": "u"}) is False
    assert mcp_access.MCP_REFUSAL == "MCP access is not enabled for your organization"


def test_set_mcp_enabled_updates_only_that_row_and_drops_the_cache(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(tenancy, "_table", table)
    tenancy._cache["t1"] = (0.0, Tenant("t1", "Alpha", "premium", False))
    tenant = tenancy.set_mcp_enabled("t1", True)
    assert tenant.mcp_enabled is True
    (call,) = table.updates
    assert call["Key"] == {"tenant_id": "t1"}
    assert call["UpdateExpression"] == "SET mcp_enabled = :e, updated_at = :u"
    assert call["ExpressionAttributeValues"][":e"] is True
    tenancy._cache.clear()


def test_setting_defaults_to_off(client):
    assert client.get(SETTING, headers=AUTH).json() == {"enabled": False}


def test_put_then_get_round_trips(client, monkeypatch):
    state = {"enabled": False}

    def fake_set(tenant_id, enabled):
        assert tenant_id == TENANT_A.tenant_id
        state["enabled"] = enabled
        return Tenant(tenant_id, "Alpha", "premium", enabled)

    monkeypatch.setattr(tenancy, "set_mcp_enabled", fake_set)
    monkeypatch.setattr(tenancy, "get_tenant",
                        lambda t: Tenant(t, "Alpha", "premium", state["enabled"]))
    assert client.put(SETTING, headers=AUTH, json={"enabled": True}).json() == {"enabled": True}
    assert client.get(SETTING, headers=AUTH).json() == {"enabled": True}


def test_put_writes_only_the_callers_row(client, monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(tenancy, "_table", table)
    r = client.put(SETTING, headers=AUTH, json={"enabled": True, "tenant_id": "tenant-b"})
    assert r.status_code == 200
    assert [u["Key"] for u in table.updates] == [{"tenant_id": "tenant-a"}]


@pytest.mark.parametrize("body", [{}, {"enabled": "yes"}, {"enabled": 1}, {"enabled": None}])
def test_put_rejects_a_non_boolean(client, body):
    assert client.put(SETTING, headers=AUTH, json=body).status_code == 400


@pytest.mark.parametrize("user", [AUTHOR, VIEWER])
@pytest.mark.parametrize("method,path", [("GET", SETTING), ("PUT", SETTING), ("GET", SETUP)])
def test_non_admins_are_refused(client, monkeypatch, user, method, path):
    _as(monkeypatch, user)
    kw = {"json": {"enabled": True}} if method == "PUT" else {}
    assert client.request(method, path, headers=AUTH, **kw).status_code == 403


def test_setup_without_discovery(client, monkeypatch):
    monkeypatch.setattr(mcp_access, "_mcp_discovery", lambda: (None, None))
    assert client.get(SETUP, headers=AUTH).json() == {"deployed": False}


def test_setup_with_discovery(client):
    body = client.get(SETUP, headers=AUTH).json()
    assert body["deployed"] is True and body["url"] == URL
    snippets = body["snippets"]
    assert snippets["claude_desktop"] == URL
    assert snippets["claude_code"] == f"claude mcp add --transport http illuminate {URL}"
    assert json.loads(snippets["vscode"]) == {"servers": {"illuminate": {"type": "http", "url": URL}}}


def test_discovery_reads_ssm_once_per_five_minutes(monkeypatch):
    calls = []

    class Ssm:
        def get_parameter(self, Name):
            calls.append(Name)
            if Name.endswith("mcp-url"):
                return {"Parameter": {"Value": URL}}
            raise self.exceptions.ParameterNotFound()

        class exceptions:
            class ParameterNotFound(Exception):
                pass

    now = [1000.0]
    monkeypatch.setattr(mcp_access, "_ssm", Ssm())
    monkeypatch.setattr(mcp_access, "_discovery", None)
    monkeypatch.setattr(mcp_access.time, "monotonic", lambda: now[0])
    monkeypatch.setenv("ENVIRONMENT", "qa")
    assert mcp_access._mcp_discovery() == (None, URL)
    assert calls == ["/illuminate/qa/mcp-client-id", "/illuminate/qa/mcp-url"]
    now[0] += 299
    mcp_access._mcp_discovery()
    assert len(calls) == 2
    now[0] += 2
    mcp_access._mcp_discovery()
    assert len(calls) == 4


def test_me_carries_the_flag(client, monkeypatch):
    monkeypatch.setattr(lambda_handler, "_record_in_directory", lambda *a: None)
    assert client.get("/api/v1/me", headers=AUTH).json()["mcp_enabled"] is False
    on = Tenant("tenant-a", "Alpha", "premium", True)
    monkeypatch.setattr(lambda_handler, "tenant_for", lambda u: on)
    assert client.get("/api/v1/me", headers=AUTH).json()["mcp_enabled"] is True
