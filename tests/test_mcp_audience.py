import types

import boto3
import pytest
import snowflake.connector
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import lambda_handler
import mcp_access
import snowflake_client
import tenancy
import user_items_store
from tenancy import Tenant

AUTH = {"Authorization": "Bearer t"}
PUBLIC_PATHS = {"/health"}
DOCS_PATHS = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
ADMIN_PREFIX = "/api/v1/admin/"
WEB_ID, MCP_ID = "web-client", "mcp-client"


class FakeTenants:
    def __init__(self, **flags):
        self.rows = {t: {"tenant_id": t, "name": t, "license": "premium", "mcp_enabled": on} for t, on in flags.items()}
        self.reads = 0

    def get_item(self, Key, **_):
        self.reads += 1
        row = self.rows.get(Key["tenant_id"])
        return {"Item": dict(row)} if row else {}

    def update_item(self, Key, ExpressionAttributeValues, **_):
        self.rows[Key["tenant_id"]]["mcp_enabled"] = ExpressionAttributeValues[":e"]


class FakeSessions:
    def __init__(self):
        self.items, self.calls = {}, []

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ReturnValues):
        self.calls.append(Key["pk"])
        assert UpdateExpression == "ADD n :one SET expires_at = :exp" and ReturnValues == "UPDATED_NEW"
        item = self.items.setdefault(Key["pk"], {"n": 0})
        item["n"] += ExpressionAttributeValues[":one"]
        item["expires_at"] = ExpressionAttributeValues[":exp"]
        return {"Attributes": dict(item)}


def mcp_user(tenant_id="ta", *groups, sub="u1"):
    return {"sub": sub, "custom:tenant_id": tenant_id, "cognito:groups": list(groups), "_via_mcp": True}


@pytest.fixture
def world(monkeypatch):
    clock = types.SimpleNamespace(mono=1000.0, wall=60_000.0)
    tenants, sessions = FakeTenants(ta=False, tb=True), FakeSessions()
    monkeypatch.setattr(tenancy, "_table", tenants)
    monkeypatch.setattr(tenancy, "time", types.SimpleNamespace(monotonic=lambda: clock.mono))
    monkeypatch.setattr(lambda_handler, "_now", lambda: clock.wall)
    monkeypatch.setattr(mcp_access, "_sessions_table", sessions)
    monkeypatch.setattr(snowflake_client, "tenant_database", lambda tenant: "DB")
    monkeypatch.setattr(user_items_store, "upsert_directory", lambda *a: None)
    tenancy._cache.clear()
    state = types.SimpleNamespace(clock=clock, tenants=tenants, sessions=sessions, client=TestClient(lambda_handler.app))

    def as_user(user):
        monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user if a else None)
    state.as_user = as_user
    yield state
    tenancy._cache.clear()


def _no_backend(monkeypatch):
    calls = []

    def refuse(name):
        def call(*a, **k):
            calls.append(name)
            raise AssertionError(f"{name} reached")
        return call
    monkeypatch.setattr(boto3, "client", refuse("boto3.client"))
    monkeypatch.setattr(boto3, "resource", refuse("boto3.resource"))
    monkeypatch.setattr(snowflake.connector, "connect", refuse("snowflake"))
    return calls


def _sweep():
    """Every route the app serves, with path parameters filled in; non-API routes must be the docs."""
    out = []
    for route in lambda_handler.app.routes:
        if not isinstance(route, APIRoute):
            assert route.path in DOCS_PATHS, f"unexpected non-API route {route.path}"
            continue
        if route.path in PUBLIC_PATHS:
            continue
        path = route.path
        for name in route.param_convertors:
            path = path.replace("{" + name + "}", "x").replace("{" + name + ":path}", "x")
        for method in sorted(route.methods - {"HEAD"}):
            out.append((method, path))
    return out


SWEEP = _sweep()


def test_sweep_covers_the_api():
    paths = {p for _, p in SWEEP}
    assert len(SWEEP) > 50
    assert {"/api/v1/me", "/api/v1/sql/run", "/api/chat/stream", "/api/v1/admin/settings/mcp"} <= paths


@pytest.mark.parametrize("method,path", SWEEP)
def test_every_route_refuses_mcp_while_off_before_any_backend_call(world, monkeypatch, method, path):
    world.as_user(mcp_user("ta", "illuminate-admins"))
    calls = _no_backend(monkeypatch)
    kw = {"json": {}} if method in ("POST", "PUT") else {}
    r = world.client.request(method, path, headers=AUTH, **kw)
    assert (r.status_code, r.json()) == (403, {"detail": mcp_access.MCP_REFUSAL})
    assert calls == [] and world.sessions.calls == []


@pytest.mark.parametrize("method,path", [(m, p) for m, p in SWEEP if p.startswith(ADMIN_PREFIX)])
def test_admin_routes_refuse_mcp_even_when_on(world, monkeypatch, method, path):
    world.as_user(mcp_user("tb", "illuminate-admins"))
    calls = _no_backend(monkeypatch)
    kw = {"json": {}} if method in ("POST", "PUT") else {}
    r = world.client.request(method, path, headers=AUTH, **kw)
    assert (r.status_code, r.json()) == (403, {"detail": mcp_access.MCP_ADMIN_REFUSAL})
    assert calls == []


def test_admin_only_routes_outside_admin_prefix_refuse_mcp(world):
    world.as_user(mcp_user("tb", "illuminate-admins"))
    r = world.client.get("/api/v1/dictionary/preview?schema=CDM_LMS&table=COURSE", headers=AUTH)
    assert (r.status_code, r.json()) == (403, {"detail": mcp_access.MCP_ADMIN_REFUSAL})


def test_mcp_token_is_accepted_when_on(world):
    world.as_user(mcp_user("tb"))
    r = world.client.get("/api/v1/me", headers=AUTH)
    assert r.status_code == 200 and r.json()["mcp_enabled"] is True


def test_turning_mcp_off_elsewhere_takes_effect_once_the_cache_expires(world):
    world.as_user(mcp_user("tb"))
    assert world.client.get("/api/v1/me", headers=AUTH).status_code == 200
    world.tenants.rows["tb"]["mcp_enabled"] = False
    world.clock.mono += 30
    assert world.client.get("/api/v1/me", headers=AUTH).status_code == 200
    world.clock.mono += 31
    r = world.client.get("/api/v1/me", headers=AUTH)
    assert (r.status_code, r.json()) == (403, {"detail": mcp_access.MCP_REFUSAL})


def test_set_mcp_enabled_drops_a_fresh_cache_entry(world):
    assert tenancy.get_tenant("ta").mcp_enabled is False
    reads = world.tenants.reads
    assert tenancy.get_tenant("ta").mcp_enabled is False and world.tenants.reads == reads
    assert tenancy.set_mcp_enabled("ta", True).mcp_enabled is True
    assert tenancy.get_tenant("ta").mcp_enabled is True


def test_admin_turning_mcp_on_admits_the_next_mcp_request_at_once(world):
    world.as_user(mcp_user("ta"))
    assert world.client.get("/api/v1/me", headers=AUTH).status_code == 403
    world.as_user({"sub": "admin", "custom:tenant_id": "ta", "cognito:groups": ["illuminate-admins"]})
    assert world.client.put("/api/v1/admin/settings/mcp", headers=AUTH, json={"enabled": True}).json() == {"enabled": True}
    world.as_user(mcp_user("ta"))
    assert world.client.get("/api/v1/me", headers=AUTH).status_code == 200


def test_the_121st_call_in_a_minute_is_429_and_the_next_minute_is_allowed(world):
    world.as_user(mcp_user("tb"))
    world.clock.wall = 60_000.0 + 15.2
    for _ in range(mcp_access.RATE_LIMIT):
        assert world.client.get("/api/v1/me", headers=AUTH).status_code == 200
    r = world.client.get("/api/v1/me", headers=AUTH)
    assert r.status_code == 429 and r.headers["Retry-After"] == "45"
    assert r.json() == {"detail": mcp_access.MCP_RATE_REFUSAL}
    world.clock.wall = 60_000.0 + 60
    assert world.client.get("/api/v1/me", headers=AUTH).status_code == 200


def test_web_app_tokens_are_never_rate_limited(world):
    world.as_user({"sub": "u1", "custom:tenant_id": "ta", "cognito:groups": []})
    for _ in range(mcp_access.RATE_LIMIT + 5):
        assert world.client.get("/api/v1/me", headers=AUTH).status_code == 200
    assert world.sessions.calls == []


def test_charge_counts_per_tenant_user_and_minute(monkeypatch):
    sessions = FakeSessions()
    monkeypatch.setattr(mcp_access, "_sessions_table", sessions)
    now = 120 * 60 + 59.5
    for _ in range(mcp_access.RATE_LIMIT):
        assert mcp_access.charge("ta", "u1", now) is None
    assert mcp_access.charge("ta", "u1", now) == 1
    assert mcp_access.charge("tb", "u1", now) is None
    assert mcp_access.charge("ta", "u2", now) is None
    assert mcp_access.charge("ta", "u1", now + 1) is None
    assert set(sessions.items) == {"rate#ta#u1#120", "rate#tb#u1#120", "rate#ta#u2#120", "rate#ta#u1#121"}
    assert sessions.items["rate#ta#u1#120"]["expires_at"] == 122 * 60


# --- audience ---------------------------------------------------------------------------------------------------

@pytest.fixture
def decoding(monkeypatch):
    monkeypatch.setattr(lambda_handler, "USER_POOL_CLIENT_ID", WEB_ID)
    monkeypatch.setattr(lambda_handler, "_get_jwks", lambda: {"keys": [{"kid": "k1"}]})
    monkeypatch.setattr(lambda_handler.jwt, "get_unverified_header", lambda token: {"kid": "k1"})
    monkeypatch.setattr(mcp_access, "_mcp_discovery", lambda: (MCP_ID, "https://mcp.example.com/mcp"))

    def use(claims):
        monkeypatch.setattr(lambda_handler.jwt, "decode", lambda *a, **k: dict(claims))
    return use


def test_mcp_audience_is_accepted_and_marked(decoding):
    decoding({"token_use": "id", "aud": MCP_ID, "sub": "u1"})
    assert lambda_handler._validate_token("t")["_via_mcp"] is True


def test_web_audience_is_unmarked_and_a_forged_mark_is_discarded(decoding):
    decoding({"token_use": "id", "aud": WEB_ID, "sub": "u1", "_via_mcp": True})
    claims = lambda_handler._validate_token("t")
    assert claims["sub"] == "u1" and "_via_mcp" not in claims


def test_forged_false_mark_on_an_mcp_token_is_overwritten(decoding):
    decoding({"token_use": "id", "aud": MCP_ID, "sub": "u1", "_via_mcp": False})
    assert lambda_handler._validate_token("t")["_via_mcp"] is True


def test_unknown_audience_is_refused(decoding):
    decoding({"token_use": "id", "aud": "stranger", "sub": "u1"})
    assert lambda_handler._validate_token("t") is None


def test_mcp_access_tokens_are_refused(decoding):
    decoding({"token_use": "access", "client_id": MCP_ID, "aud": MCP_ID, "sub": "u1"})
    assert lambda_handler._validate_token("t") is None


def test_missing_discovery_refuses_mcp_tokens(decoding, monkeypatch):
    monkeypatch.setattr(mcp_access, "_mcp_discovery", lambda: (None, None))
    decoding({"token_use": "id", "aud": MCP_ID, "sub": "u1"})
    assert lambda_handler._validate_token("t") is None


def test_a_discovery_error_refuses_mcp_tokens_and_leaves_web_tokens_alone(decoding, monkeypatch):
    looked = []

    def broken():
        looked.append(1)
        raise RuntimeError("AccessDenied")
    monkeypatch.setattr(mcp_access, "_mcp_discovery", broken)
    decoding({"token_use": "id", "aud": WEB_ID, "sub": "u1"})
    assert lambda_handler._validate_token("t")["sub"] == "u1" and looked == []
    decoding({"token_use": "id", "aud": MCP_ID, "sub": "u1"})
    assert lambda_handler._validate_token("t") is None


class BrokenSsm:
    class exceptions:
        class ParameterNotFound(Exception):
            pass

    def get_parameter(self, Name):
        raise RuntimeError("AccessDenied")


def test_any_ssm_error_reads_as_not_deployed_for_30_seconds(monkeypatch):
    clock = types.SimpleNamespace(now=500.0)
    monkeypatch.setattr(mcp_access, "time", types.SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(mcp_access, "_ssm", BrokenSsm())
    monkeypatch.setattr(mcp_access, "_discovery", None)
    assert mcp_access._mcp_discovery() == (None, None)
    monkeypatch.setattr(mcp_access, "_ssm", None)
    clock.now += 30
    assert mcp_access._mcp_discovery() == (None, None)
    monkeypatch.setattr(mcp_access, "_ssm", types.SimpleNamespace(
        exceptions=BrokenSsm.exceptions, get_parameter=lambda Name: {"Parameter": {"Value": Name}}))
    clock.now += 1
    assert mcp_access._mcp_discovery() == ("/illuminate/dev/mcp-client-id", "/illuminate/dev/mcp-url")


def test_setup_route_survives_an_ssm_error(monkeypatch):
    monkeypatch.setattr(mcp_access, "_ssm", BrokenSsm())
    monkeypatch.setattr(mcp_access, "_discovery", None)
    monkeypatch.setattr(tenancy, "get_tenant", lambda t: Tenant(t, "A", "premium", False))
    monkeypatch.setattr(snowflake_client, "tenant_database", lambda tenant: "DB")
    monkeypatch.setattr(lambda_handler, "_get_user_from_token",
                        lambda a: {"sub": "a", "custom:tenant_id": "ta", "cognito:groups": ["illuminate-admins"]})
    r = TestClient(lambda_handler.app).get("/api/v1/admin/mcp/setup", headers=AUTH)
    assert r.status_code == 200 and r.json() == {"deployed": False}


# --- one validation per request ---------------------------------------------------------------------------------

def _flaky(value):
    """Raises on the first call and returns value after that."""
    calls = []

    def call():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return value
    return call


@pytest.fixture
def real_tokens(world, decoding, monkeypatch):
    decodes = []

    def use(claims):
        def decode(*a, **k):
            decodes.append(1)
            return dict(claims)
        monkeypatch.setattr(lambda_handler.jwt, "decode", decode)
    world.use, world.decodes = use, decodes
    return world


@pytest.mark.parametrize("source", ["discovery", "jwks"])
@pytest.mark.parametrize("tenant", ["ta", "tb"])
def test_a_transient_lookup_failure_never_lets_an_mcp_token_skip_the_gate(real_tokens, monkeypatch, source, tenant):
    w = real_tokens
    if source == "discovery":
        monkeypatch.setattr(mcp_access, "_mcp_discovery", _flaky((MCP_ID, "https://mcp.example.com/mcp")))
    else:
        monkeypatch.setattr(lambda_handler, "_get_jwks", _flaky({"keys": [{"kid": "k1"}]}))
    w.use({"token_use": "id", "aud": MCP_ID, "sub": "u1", "custom:tenant_id": tenant})

    first = w.client.get("/api/v1/me", headers=AUTH)
    assert (first.status_code, first.json()) == (401, {"detail": "Invalid or expired token"})
    assert w.decodes == [1] if source == "discovery" else w.decodes == []
    assert w.sessions.calls == []

    second = w.client.get("/api/v1/me", headers=AUTH)
    if tenant == "ta":
        assert (second.status_code, second.json()) == (403, {"detail": mcp_access.MCP_REFUSAL})
        assert w.sessions.calls == []
    else:
        assert second.status_code == 200 and len(w.sessions.calls) == 1


def test_a_request_validates_its_token_once(real_tokens):
    w = real_tokens
    w.use({"token_use": "id", "aud": WEB_ID, "sub": "u1", "custom:tenant_id": "ta"})
    assert w.client.get("/api/v1/me", headers=AUTH).status_code == 200
    assert w.decodes == [1]
