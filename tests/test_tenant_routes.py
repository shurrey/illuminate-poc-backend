import boto3
import pytest
from fastapi.testclient import TestClient

from moto import mock_aws

import conversation_store
import lambda_handler
import overlay_store
import snowflake_client
from tenancy import NOT_READY, TenantNotReady
from tests.tenant_fakes import TENANT_A, TENANT_B, install_tenants, user_of

AUTH = {"Authorization": "Bearer test"}
CONTRACT = {"metrics": ["metric.reportable_courses.v1"], "dimensions": ["term_name"]}
EMPTY = {"columns": [], "rows": []}


@pytest.fixture
def as_user(monkeypatch):
    install_tenants(monkeypatch, TENANT_A, TENANT_B)
    lambda_handler._terms_cache.clear()
    lambda_handler._overlay_cache.clear()

    def login(user):
        monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user)
        return TestClient(lambda_handler.app)

    yield login
    lambda_handler._terms_cache.clear()
    lambda_handler._overlay_cache.clear()


def test_semantic_query_uses_callers_tenant(as_user, monkeypatch):
    seen = []
    monkeypatch.setattr(snowflake_client, "validate_and_execute",
                        lambda sql, params=None, **k: seen.append(k["tenant"]) or EMPTY)
    assert as_user(user_of(TENANT_B)).post("/api/v1/semantic/query", headers=AUTH, json=CONTRACT).status_code == 200
    assert [t.tenant_id for t in seen] == [TENANT_B.tenant_id]


def test_compiled_sql_uses_tenant_database(as_user, monkeypatch):
    monkeypatch.delenv("TEST_SNOWFLAKE_DATABASE", raising=False)
    monkeypatch.setattr(snowflake_client, "tenant_database",
                        lambda t: {TENANT_A.tenant_id: "DB_A", TENANT_B.tenant_id: "DB_B"}[t.tenant_id])
    r = as_user(user_of(TENANT_B)).post("/api/v1/semantic/compile", headers=AUTH, json=CONTRACT)
    assert r.status_code == 200
    assert "DB_B." in r.json()["sql"] and "DB_A." not in r.json()["sql"]


def test_terms_cache_is_per_tenant(as_user, monkeypatch):
    def fake(sql, params=None, **k):
        name = f"term-of-{k['tenant'].tenant_id}"
        return {"columns": ["TERM_NAME", "TERM_START", "TERM_END"],
                "rows": [{"TERM_NAME": name, "TERM_START": "2026-01-01", "TERM_END": "2026-05-01"}]}

    monkeypatch.setattr(snowflake_client, "validate_and_execute", fake)
    assert [r["term_name"] for r in lambda_handler._current_terms(TENANT_A)] == ["term-of-tenant-a"]
    assert [r["term_name"] for r in lambda_handler._current_terms(TENANT_B)] == ["term-of-tenant-b"]


def test_preview_without_tenant_is_403(as_user, monkeypatch):
    monkeypatch.setattr(snowflake_client, "query_preview", lambda *a, **k: pytest.fail("reached Snowflake"))
    user = {"sub": "u1", "cognito:groups": ["illuminate-admins"]}
    r = as_user(user).get("/api/v1/dictionary/preview?schema=CDM_LMS&table=COURSE", headers=AUTH)
    assert r.status_code == 403 and r.json() == {"detail": NOT_READY}


def test_preview_runs_in_callers_tenant(as_user, monkeypatch):
    seen = []
    monkeypatch.setattr(snowflake_client, "query_preview",
                        lambda schema, table, limit=20, *, tenant=None: seen.append(tenant) or EMPTY)
    r = as_user(user_of(TENANT_B, "illuminate-admins")).get(
        "/api/v1/dictionary/preview?schema=CDM_LMS&table=COURSE", headers=AUTH)
    assert r.status_code == 200 and seen == [TENANT_B]


def test_preview_tenant_not_ready_inside_route_is_403(as_user, monkeypatch):
    def not_ready(*a, **k):
        raise TenantNotReady(TENANT_B.tenant_id)

    monkeypatch.setattr(snowflake_client, "query_preview", not_ready)
    r = as_user(user_of(TENANT_B, "illuminate-admins")).get(
        "/api/v1/dictionary/preview?schema=CDM_LMS&table=COURSE", headers=AUTH)
    assert r.status_code == 403 and r.json() == {"detail": NOT_READY}
    assert TENANT_B.tenant_id not in r.text


def test_report_run_unknown_tenant_is_403_before_snowflake(as_user, monkeypatch):
    monkeypatch.setattr(snowflake_client, "validate_and_execute", lambda *a, **k: pytest.fail("reached Snowflake"))
    report = next(iter(lambda_handler._reports().values()))
    visual = next(v for p in report.pages for v in p.visuals if v.queries)
    body = {"visual": visual.id, "query": next(iter(visual.queries)), "values": {}}
    ghost = {"sub": "u1", "custom:tenant_id": "ghost"}
    r = as_user(ghost).post(f"/api/v1/reports/{report.id}/run", headers=AUTH, json=body)
    assert r.status_code == 403 and r.json() == {"detail": NOT_READY}


def test_admin_overlay_routes_use_registry_tenant(as_user, monkeypatch):
    seen = []
    monkeypatch.setattr(overlay_store, "list_overlays", lambda tenant_id: seen.append(tenant_id) or [])
    r = as_user(user_of(TENANT_B, "illuminate-admins")).get("/api/v1/admin/overlays", headers=AUTH)
    assert r.status_code == 200 and seen == [TENANT_B.tenant_id]
    assert r.json()["tenant_id"] == TENANT_B.tenant_id


def test_admin_overlay_route_unregistered_tenant_is_403(as_user, monkeypatch):
    monkeypatch.setattr(overlay_store, "list_overlays", lambda tenant_id: pytest.fail("read overlays"))
    ghost = {"sub": "u1", "custom:tenant_id": "ghost", "cognito:groups": ["illuminate-admins"]}
    r = as_user(ghost).get("/api/v1/admin/overlays", headers=AUTH)
    assert r.status_code == 403 and r.json() == {"detail": NOT_READY}


def test_tenant_not_ready_inside_route_is_403(as_user, monkeypatch):
    monkeypatch.delenv("TEST_SNOWFLAKE_DATABASE", raising=False)
    calls = []

    # tenant_for's own check passes; the secret is gone by the time the route resolves the database.
    def not_ready(tenant):
        calls.append(tenant)
        if len(calls) > 1:
            raise TenantNotReady(tenant.tenant_id)
        return "DB_A"

    monkeypatch.setattr(snowflake_client, "tenant_database", not_ready)
    r = as_user(user_of(TENANT_A)).post("/api/v1/semantic/compile", headers=AUTH, json=CONTRACT)
    assert r.status_code == 403 and r.json() == {"detail": NOT_READY}
    assert TENANT_A.tenant_id not in r.text
    assert len(calls) == 2


class _NoBedrock:
    def converse(self, **kwargs):
        pytest.fail("reached Bedrock")


@pytest.fixture
def no_bedrock(monkeypatch):
    import chat_engine
    import conversation_store
    monkeypatch.setattr(chat_engine, "_bedrock", _NoBedrock())
    monkeypatch.setattr(conversation_store, "load_history", lambda cid, owner: [])
    monkeypatch.setattr(conversation_store, "save_turn", lambda *a, **k: None)


def _not_ready_database(monkeypatch):
    monkeypatch.delenv("TEST_SNOWFLAKE_DATABASE", raising=False)

    def not_ready(tenant):
        raise TenantNotReady(tenant.tenant_id)

    monkeypatch.setattr(snowflake_client, "tenant_database", not_ready)


@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
def test_chat_unknown_tenant_403_before_bedrock(as_user, no_bedrock, path):
    ghost = {"sub": "u1", "custom:tenant_id": "ghost"}
    r = as_user(ghost).post(path, headers=AUTH, json={"message": "q"})
    assert r.status_code == 403 and r.json() == {"detail": NOT_READY}
    assert "ghost" not in r.text


@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
def test_chat_tenant_without_snowflake_secret_403_before_bedrock(as_user, no_bedrock, monkeypatch, path):
    _not_ready_database(monkeypatch)
    r = as_user(user_of(TENANT_A)).post(path, headers=AUTH, json={"message": "q"})
    assert r.status_code == 403 and r.json() == {"detail": NOT_READY}
    assert TENANT_A.tenant_id not in r.text


def test_chat_stream_tenant_not_ready_mid_stream_is_a_generic_error_event(as_user, no_bedrock, monkeypatch):
    import chat_engine

    async def not_ready(message, history, **kw):
        raise TenantNotReady(TENANT_A.tenant_id)
        yield

    monkeypatch.setattr(chat_engine, "send_message_streaming", not_ready)
    r = as_user(user_of(TENANT_A)).post("/api/chat/stream", headers=AUTH, json={"message": "q"})
    assert NOT_READY in r.text and TENANT_A.tenant_id not in r.text


def test_chat_tenant_not_ready_mid_request_is_403(as_user, no_bedrock, monkeypatch):
    import chat_engine

    def not_ready(message, history, **kw):
        raise TenantNotReady(TENANT_A.tenant_id)

    monkeypatch.setattr(chat_engine, "send_message", not_ready)
    r = as_user(user_of(TENANT_A)).post("/api/chat", headers=AUTH, json={"message": "q"})
    assert r.status_code == 403 and r.json() == {"detail": NOT_READY}


def test_engine_kwargs_prompt_names_tenant_database(as_user, monkeypatch):
    monkeypatch.delenv("TEST_SNOWFLAKE_DATABASE", raising=False)
    monkeypatch.setattr(snowflake_client, "tenant_database",
                        lambda t: {TENANT_A.tenant_id: "DB_A", TENANT_B.tenant_id: "DB_B"}[t.tenant_id])
    kwargs = lambda_handler._engine_kwargs(TENANT_B)
    assert "DB_B" in kwargs["system_prompt"] and "DB_A" not in kwargs["system_prompt"]
    assert kwargs["tools"].database == "DB_B" and kwargs["tools"].tenant == TENANT_B


def test_chat_engine_import_reads_no_secret(monkeypatch):
    import importlib
    import sys
    import boto3
    from unittest.mock import MagicMock

    def client(service, *a, **k):
        if service == "secretsmanager":
            raise AssertionError("chat_engine read a secret at import")
        return MagicMock()

    monkeypatch.setattr(boto3, "client", client)
    monkeypatch.delitem(sys.modules, "chat_engine", raising=False)
    importlib.import_module("chat_engine")


@pytest.fixture
def conversations(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        t = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="conversations-test",
            KeySchema=[{"AttributeName": "context_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "context_id", "AttributeType": "S"},
                                  {"AttributeName": "owner_sub", "AttributeType": "S"},
                                  {"AttributeName": "updated_at", "AttributeType": "N"}],
            GlobalSecondaryIndexes=[{
                "IndexName": conversation_store.OWNER_INDEX,
                "KeySchema": [{"AttributeName": "owner_sub", "KeyType": "HASH"},
                              {"AttributeName": "updated_at", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "INCLUDE", "NonKeyAttributes": ["title"]},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(conversation_store, "_table", t)
        yield t


def _owner_a(sub="u1"):
    return lambda_handler._owner(user_of(TENANT_A, sub=sub), TENANT_A)


def test_same_sub_other_tenant_cannot_read_conversation(as_user, conversations):
    conversation_store.save_turn("c1", _owner_a(), "hi", "hello")
    assert as_user(user_of(TENANT_A)).get("/api/conversations/c1", headers=AUTH).status_code == 200
    assert as_user(user_of(TENANT_B)).get("/api/conversations/c1", headers=AUTH).status_code == 404
    assert as_user(user_of(TENANT_B)).delete("/api/conversations/c1", headers=AUTH).status_code == 404
    assert conversation_store.load_history("c1", _owner_a()) != []


def test_same_sub_other_tenant_gets_new_context_id(as_user, conversations, monkeypatch):
    import chat_engine
    monkeypatch.setattr(lambda_handler, "_engine_kwargs", lambda tenant: {})
    monkeypatch.setattr(chat_engine, "send_message", lambda *a, **k: ("answer", [], []))
    conversation_store.save_turn("c1", _owner_a(), "hi", "hello")
    r = as_user(user_of(TENANT_B)).post("/api/chat", headers=AUTH, json={"message": "mine", "context_id": "c1"})
    assert r.status_code == 200
    assert r.json()["context_id"] != "c1"
    assert [m["content"] for m in conversation_store.load_history("c1", _owner_a())] == ["hi", "hello"]


def test_conversation_list_is_per_tenant(as_user, conversations):
    conversation_store.save_turn("c1", _owner_a(), "hi", "hello")
    listed = lambda u: [c["context_id"] for c in as_user(u).get("/api/conversations", headers=AUTH).json()["conversations"]]
    assert listed(user_of(TENANT_A)) == ["c1"]
    assert listed(user_of(TENANT_B)) == []


def test_legacy_owner_without_tenant_is_unreadable(as_user, conversations):
    conversation_store.save_turn("old", "u1", "hi", "hello")
    assert as_user(user_of(TENANT_A)).get("/api/conversations/old", headers=AUTH).status_code == 404
    assert as_user(user_of(TENANT_A)).get("/api/conversations", headers=AUTH).json() == {"conversations": []}


def test_cancel_from_other_tenant_is_404(as_user, monkeypatch):
    monkeypatch.setattr(lambda_handler, "_request_owners", {"r1": _owner_a()})
    monkeypatch.setattr(lambda_handler, "_cancelled_requests", set())
    assert as_user(user_of(TENANT_B)).post("/api/chat/cancel/r1", headers=AUTH).status_code == 404
    assert as_user(user_of(TENANT_A)).post("/api/chat/cancel/r1", headers=AUTH).status_code == 200


@pytest.mark.parametrize("method,path", [("get", "/api/conversations"), ("get", "/api/conversations/c1"),
                                         ("delete", "/api/conversations/c1"), ("post", "/api/chat/cancel/r1")])
def test_conversation_routes_403_when_tenant_not_ready(as_user, monkeypatch, method, path):
    import tenancy
    monkeypatch.setattr(tenancy, "get_tenant", lambda tid: (_ for _ in ()).throw(TenantNotReady(tid)))
    r = getattr(as_user(user_of(TENANT_A)), method)(path, headers=AUTH)
    assert r.status_code == 403 and r.json()["detail"] == NOT_READY
