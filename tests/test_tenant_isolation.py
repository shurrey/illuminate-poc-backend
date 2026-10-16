"""Cross-tenant isolation: tenant B never sees anything of tenant A's, through any tenant route.

Only the edges are faked: the registry, overlay and conversation tables and Secrets Manager (moto),
snowflake.connector.connect and Bedrock. Later phases add their routes here.
"""
import json
import re
import types

import boto3
import pytest
import snowflake.connector
from fastapi.testclient import TestClient
from moto import mock_aws

import chat_engine
import conversation_store
import lambda_handler
import licenses
import overlay_store
import snowflake_client
import tenancy
from semantic_layer.catalog import default_catalog
from semantic_layer.catalog_view import public_catalog
from semantic_layer.overlays import Overlay
from tenancy import NOT_READY
from tests.conftest import REAL_LIST_OVERLAYS

MARK_A, MARK_B = "ALPHA-ONLY", "BETA-ONLY"
DB_A, DB_B = "DB_ALPHA_ONLY", "DB_BETA_ONLY"
A, B, C, C2 = "tenant-a", "tenant-b", "tenant-c", "tenant-c2"

ADMIN = "illuminate-admins"
USERS = {
    "Bearer a": {"sub": "u1", "custom:tenant_id": A, "cognito:groups": [ADMIN]},
    "Bearer b": {"sub": "u1", "custom:tenant_id": B, "cognito:groups": [ADMIN]},
    "Bearer c": {"sub": "u1", "custom:tenant_id": C, "cognito:groups": [ADMIN]},
    "Bearer c2": {"sub": "u1", "custom:tenant_id": C2, "cognito:groups": [ADMIN]},
    "Bearer ghost": {"sub": "u1", "custom:tenant_id": "ghost", "cognito:groups": [ADMIN]},
}
AS_A, AS_B, AS_C, AS_GHOST = ({"Authorization": f"Bearer {t}"} for t in ("a", "b", "c", "ghost"))

CONTRACT = {"metrics": ["metric.reportable_courses.v1"], "dimensions": ["term_name"]}
MEASURE = "measure:dataset.student_grade.v1:average_grade_percentage"
FILTER = "filter:dataset.student_grade.v1:alpha_honours"
REPORT, VISUAL = "report.assessment_grades.v1", "ungraded"
PREVIEW = "/api/v1/dictionary/preview?schema=CDM_LMS&table=COURSE"


def _clean(text: str) -> None:
    assert MARK_A not in text and DB_A not in text


class FakeConnection:
    """Answers every query with its own secret's marker, so a crossed connection shows up in the response."""

    def __init__(self, log, **kw):
        self.kw, self.log, self.closed = kw, log, False

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True

    def cursor(self):
        return FakeCursor(self)


class FakeCursor:
    def __init__(self, conn):
        self.conn, self.rows, self.description = conn, [], []

    def execute(self, sql, params=None):
        mark = self.conn.kw["account"]
        self.conn.log.append((mark, sql))
        if "term_start" in sql.lower():
            cols, self.rows = ["TERM_NAME", "TERM_START", "TERM_END"], [(f"{mark} Term", "2000-01-01", "2099-12-31")]
        else:
            cols, self.rows = ["TENANT_MARK", "DATABASE"], [(mark, self.conn.kw["database"])]
        self.description = [(c,) for c in cols]

    def fetchmany(self, n):
        return self.rows[:n]

    def close(self):
        pass


class ScriptedBedrock:
    """Asks for one governed query, then answers; records every Converse call."""

    def __init__(self):
        self.calls, self.tool_script = [], []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        last = kwargs["messages"][-1]["content"]
        if self.tool_script:
            name, tool_input = self.tool_script.pop(0)
            return {"output": {"message": {"role": "assistant", "content": [
                {"toolUse": {"toolUseId": "t1", "name": name, "input": tool_input}}]}}, "stopReason": "tool_use"}
        if any("toolResult" in block for block in last):
            return {"output": {"message": {"role": "assistant", "content": [{"text": "Done."}]}}, "stopReason": "end_turn"}
        return {"output": {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "t1", "name": "query_semantic", "input": CONTRACT}}]}}, "stopReason": "tool_use"}


def _table(dynamodb, name, keys, extra_attrs=(), **kw):
    attrs = [{"AttributeName": k, "AttributeType": "S"} for k in keys] + list(extra_attrs)
    return dynamodb.create_table(
        TableName=name, BillingMode="PAY_PER_REQUEST", AttributeDefinitions=attrs,
        KeySchema=[{"AttributeName": k, "KeyType": t} for k, t in zip(keys, ("HASH", "RANGE"))], **kw)


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        registry = _table(ddb, "tenants-test", ["tenant_id"])
        for tid, name in ((A, "Alpha University"), (B, "Beta College"), (C, "Gamma Institute")):
            registry.put_item(Item={"tenant_id": tid, "name": name, "license": "premium", "mcp_enabled": False})
        registry.put_item(Item={"tenant_id": C2, "name": "Core College", "license": "core", "mcp_enabled": False})
        overlays = _table(ddb, "overlays-test", ["tenant_id", "metric_id"])
        conversations = _table(
            ddb, "conversations-test", ["context_id"],
            [{"AttributeName": "owner_sub", "AttributeType": "S"}, {"AttributeName": "updated_at", "AttributeType": "N"}],
            GlobalSecondaryIndexes=[{
                "IndexName": conversation_store.OWNER_INDEX,
                "KeySchema": [{"AttributeName": "owner_sub", "KeyType": "HASH"},
                              {"AttributeName": "updated_at", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "INCLUDE", "NonKeyAttributes": ["title"]},
            }])
        sm = boto3.client("secretsmanager", region_name="us-east-1")
        for tid, mark, db in ((A, MARK_A, DB_A), (B, MARK_B, DB_B), (C2, MARK_B, DB_B)):
            sm.create_secret(Name=f"illuminate/tenants/{tid}/snowflake", SecretString=json.dumps({
                "account": mark, "user": f"{mark}-user", "password": f"{mark}-pw", "database": db,
                "warehouse": "WH", "role": "R"}))

        state = types.SimpleNamespace(log=[], connects=[], bedrock=ScriptedBedrock())

        def connect(**kw):
            state.connects.append(kw["account"])
            return FakeConnection(state.log, **kw)

        monkeypatch.setattr(tenancy, "_table", registry)
        monkeypatch.setattr(overlay_store, "_table", overlays)
        monkeypatch.setattr(overlay_store, "list_overlays", REAL_LIST_OVERLAYS)
        monkeypatch.setattr(conversation_store, "_table", conversations)
        monkeypatch.setattr(snowflake.connector, "connect", connect)
        monkeypatch.setattr(chat_engine, "_bedrock", state.bedrock)
        monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda auth: USERS.get(auth))
        caches = (tenancy._cache, snowflake_client._connections, snowflake_client._credentials,
                  lambda_handler._overlay_cache, lambda_handler._terms_cache)
        for cache in caches:
            cache.clear()
        state.client = TestClient(lambda_handler.app)
        yield state
        for cache in caches:
            cache.clear()


def _seed_a_overlays():
    overlay_store.put_overlay(A, Overlay(target=MEASURE, expr="ROUND(GRADE_PERCENTAGE, 0)",
                                         description=f"{MARK_A} rounding"), f"{MARK_A}-admin", 0)
    overlay_store.put_overlay(A, Overlay(target=FILTER, sql="GRADE_PERCENTAGE >= 90",
                                         description=f"{MARK_A} honours"), f"{MARK_A}-admin", 0)


def _save_a_conversation():
    owner = lambda_handler._owner(USERS["Bearer a"], tenancy.get_tenant(A))
    conversation_store.save_turn("c1", owner, f"{MARK_A} question", f"{MARK_A} answer")


def _query(client, auth, **kw):
    return client.post("/api/v1/semantic/query", headers=auth, json=CONTRACT, **kw)


def _report_run(client, auth, term, **kw):
    return client.post(f"/api/v1/reports/{REPORT}/run", headers=auth,
                       json={"visual": VISUAL, "query": "main", "values": {"term": [term]}, **kw.pop("extra", {})}, **kw)


def _chat(client, auth, **kw):
    return client.post("/api/chat", headers=auth, json={"message": "how many courses?", **kw.pop("extra", {})}, **kw)


def test_overlays_never_cross(world):
    _seed_a_overlays()
    c = world.client
    assert MARK_A in c.get("/api/v1/admin/overlays", headers=AS_A).text

    listed = c.get("/api/v1/admin/overlays", headers=AS_B)
    assert listed.status_code == 200 and listed.json() == {"tenant_id": B, "overlays": []}
    got = c.get(f"/api/v1/admin/overlay/{MEASURE}", headers=AS_B)
    assert got.status_code == 200 and got.json()["overlay"] is None
    history = c.get(f"/api/v1/admin/overlay/{MEASURE}/history", headers=AS_B)
    assert history.status_code == 200 and history.json()["history"] == []
    reverted = c.post(f"/api/v1/admin/overlay/{MEASURE}/revert", headers=AS_B, json={"version": 1, "expected_version": 1})
    deleted = c.delete(f"/api/v1/admin/overlay/{MEASURE}?expected_version=1", headers=AS_B)
    assert reverted.status_code == 404 and deleted.status_code == 404
    for r in (listed, got, history, reverted, deleted):
        _clean(r.text)

    assert overlay_store.get_overlay(A, MEASURE).version == 1
    assert [o.version for o in overlay_store.history(A, MEASURE)] == [1]
    assert overlay_store.list_overlays(B) == []


def test_semantic_catalog_never_crosses(world):
    _seed_a_overlays()
    c = world.client
    a_body = c.get("/api/v1/semantic/catalog", headers=AS_A)
    assert MARK_A in a_body.text

    b_body = c.get("/api/v1/semantic/catalog", headers=AS_B)
    assert b_body.status_code == 200
    _clean(b_body.text)
    assert b_body.json() == public_catalog(default_catalog(), allow_identity=True)
    assert b_body.headers["ETag"] != a_body.headers["ETag"]


def test_semantic_query_never_crosses(world):
    c = world.client
    assert MARK_A in _query(c, AS_A).text
    r = _query(c, AS_B)
    assert r.status_code == 200 and r.json()["rows"] == [{"tenant_mark": MARK_B, "database": DB_B}]
    assert f"{DB_B}." in r.json()["sql"]
    _clean(r.text)


def test_reports_never_cross(world):
    c = world.client
    a_report = c.get(f"/api/v1/reports/{REPORT}", headers=AS_A)
    assert a_report.json()["defaults"]["term"] == [f"{MARK_A} Term"]
    assert MARK_A in _report_run(c, AS_A, f"{MARK_A} Term").text

    b_report = c.get(f"/api/v1/reports/{REPORT}", headers=AS_B)
    assert b_report.status_code == 200 and b_report.json()["defaults"]["term"] == [f"{MARK_B} Term"]
    b_run = _report_run(c, AS_B, f"{MARK_B} Term")
    assert b_run.status_code == 200 and MARK_B in b_run.text and f"{DB_B}." in b_run.json()["sql"]
    b_list = c.get("/api/v1/reports", headers=AS_B)
    assert b_list.status_code == 200 and b_list.json()["reports"]
    for r in (b_report, b_run, b_list):
        _clean(r.text)


def test_preview_never_crosses(world):
    c = world.client
    assert MARK_A in c.get(PREVIEW, headers=AS_A).text
    r = c.get(PREVIEW, headers=AS_B)
    assert r.status_code == 200 and r.json()["rows"] == [{"TENANT_MARK": MARK_B, "DATABASE": DB_B}]
    _clean(r.text)


def test_me_never_crosses(world):
    c = world.client
    a_resp = c.get("/api/v1/me", headers=AS_A)
    assert a_resp.status_code == 200 and a_resp.json()["tenant"]["name"] == "Alpha University"

    b_resp = c.get("/api/v1/me", headers=AS_B)
    assert b_resp.status_code == 200 and b_resp.json()["tenant"]["name"] == "Beta College"
    _clean(b_resp.text)


@pytest.mark.parametrize("table", [f"{DB_A}.CDM_LMS.COURSE", f"{DB_A}.INFORMATION_SCHEMA.DATABASES"])
def test_raw_sql_naming_another_tenants_database_is_refused_before_any_connection(world, table):
    tenant_b = tenancy.get_tenant(B)
    result = snowflake_client.validate_and_execute(f"SELECT COUNT(*) FROM {table}", tenant=tenant_b)
    assert "not the tenant's database" in result["error"] and "warehouse_error" not in result
    assert world.connects == [] and world.log == []
    assert snowflake_client.validate_and_execute(f"SELECT COUNT(*) FROM {DB_B}.CDM_LMS.COURSE", tenant=tenant_b).get("rows")
    assert world.connects == [MARK_B]


def test_core_tenant_is_refused_learn_through_chat_raw_sql(world):
    world.bedrock.tool_script = [
        ("search_catalog", {"question": "learn users"}),
        ("execute_sql", {"sql": "SELECT COUNT(*) FROM LEARN.USERS", "reason": "no governed metric covers LEARN users"}),
    ]
    r = _chat(world.client, {"Authorization": "Bearer c2"})
    assert r.status_code == 200
    assert licenses.LEARN_REFUSAL in json.dumps(world.bedrock.calls[-1], default=str)
    assert world.connects == [] and world.log == []


def test_core_tenant_is_refused_learn_describe_and_preview(world):
    world.bedrock.tool_script = [
        ("search_catalog", {"question": "learn users"}),
        ("describe_cdm_table", {"schema": "LEARN", "table": "USERS"}),
    ]
    assert _chat(world.client, {"Authorization": "Bearer c2"}).status_code == 200
    assert licenses.LEARN_REFUSAL in json.dumps(world.bedrock.calls[-1], default=str)
    assert not re.search(r"\blearn\b", json.dumps(world.bedrock.calls[0].get("system"), default=str), re.I)
    r = world.client.get("/api/v1/dictionary/preview?schema=LEARN&table=USERS", headers={"Authorization": "Bearer c2"})
    assert r.status_code == 403 and r.json()["detail"] == licenses.PREVIEW_REFUSAL
    assert world.connects == [] and world.log == []


def test_premium_tenant_is_refused_learn_pii_through_chat_raw_sql(world):
    world.bedrock.tool_script = [
        ("search_catalog", {"question": "learn activity by user"}),
        ("execute_sql", {"sql": "SELECT a.USER_PK1 FROM LEARN.ACTIVITY_ACCUMULATOR a", "reason": "no governed metric"}),
    ]
    r = _chat(world.client, AS_A)
    assert r.status_code == 200
    assert "personally identifiable" in json.dumps(world.bedrock.calls[-1], default=str)
    assert world.connects == [] and world.log == []


def test_conversations_never_cross(world):
    c = world.client
    _save_a_conversation()
    assert MARK_A in c.get("/api/conversations/c1", headers=AS_A).text

    listed = c.get("/api/conversations", headers=AS_B)
    assert listed.status_code == 200 and listed.json() == {"conversations": []}
    got = c.get("/api/conversations/c1", headers=AS_B)
    deleted = c.delete("/api/conversations/c1", headers=AS_B)
    assert got.status_code == 404 and deleted.status_code == 404

    world.bedrock.calls.clear()
    chat = _chat(c, AS_B, extra={"context_id": "c1"})
    assert chat.status_code == 200 and chat.json()["context_id"] != "c1"
    assert world.bedrock.calls and MARK_B in chat.text
    assert MARK_A not in json.dumps(world.bedrock.calls, default=str)
    for r in (listed, got, deleted, chat):
        _clean(r.text)
    assert MARK_A in c.get("/api/conversations/c1", headers=AS_A).text


def test_alternating_requests_one_process(world):
    _seed_a_overlays()
    c = world.client
    steps = [
        ("query", lambda auth, mark: _query(c, auth)),
        ("report", lambda auth, mark: c.get(f"/api/v1/reports/{REPORT}", headers=auth)),
        ("run", lambda auth, mark: _report_run(c, auth, f"{mark} Term")),
        ("chat", lambda auth, mark: _chat(c, auth)),
        ("catalog", lambda auth, mark: c.get("/api/v1/semantic/catalog", headers=auth)),
    ]
    for name, call in steps:
        for auth, mine, other in ((AS_A, MARK_A, MARK_B), (AS_B, MARK_B, MARK_A), (AS_A, MARK_A, MARK_B), (AS_B, MARK_B, MARK_A)):
            seen = len(world.log)
            r = call(auth, mine)
            assert r.status_code == 200, (name, r.text)
            assert other not in r.text, name
            assert {mark for mark, _ in world.log[seen:]} <= {mine}, name
            if name != "catalog":
                assert mine in r.text, name

    assert sorted(world.connects) == [MARK_A, MARK_B]
    assert set(snowflake_client._connections) == {A, B}
    assert set(snowflake_client._credentials) == {A, B}
    assert set(lambda_handler._overlay_cache) == {A, B}
    assert set(lambda_handler._terms_cache) == {A, B}
    assert set(tenancy._cache) == {A, B}


FORGED = {"X-Tenant-Id": A}
FORGED_PARAMS = {"tenant_id": A}


def test_forged_tenant_fields_ignored(world, monkeypatch):
    _seed_a_overlays()
    c = world.client
    monkeypatch.setitem(USERS, "Bearer b", {**USERS["Bearer b"], "tenant_id": A, "custom:tenant": A})
    headers = {**AS_B, **FORGED}
    _save_a_conversation()

    listed = c.get("/api/conversations", headers=headers, params=FORGED_PARAMS)
    assert listed.status_code == 200 and listed.json() == {"conversations": []}
    assert c.get("/api/conversations/c1", headers=headers, params=FORGED_PARAMS).status_code == 404
    assert c.delete("/api/conversations/c1", headers=headers, params=FORGED_PARAMS).status_code == 404
    assert MARK_A in c.get("/api/conversations/c1", headers=AS_A).text
    chat = _chat(c, headers, params=FORGED_PARAMS, extra={"tenant_id": A, "context_id": "c1"})
    assert chat.status_code == 200 and chat.json()["context_id"] != "c1"
    assert world.bedrock.calls and MARK_A not in json.dumps(world.bedrock.calls, default=str)
    _clean(chat.text)

    responses = {
        "query": _query(c, headers, params=FORGED_PARAMS),
        "catalog": c.get("/api/v1/semantic/catalog", headers=headers, params=FORGED_PARAMS),
        "report": c.get(f"/api/v1/reports/{REPORT}", headers=headers, params=FORGED_PARAMS),
        "run": _report_run(c, headers, f"{MARK_B} Term", params=FORGED_PARAMS, extra={"tenant_id": A}),
        "preview": c.get(PREVIEW, headers=headers, params={"schema": "CDM_LMS", "table": "COURSE", **FORGED_PARAMS}),
        "chat": _chat(c, headers, params=FORGED_PARAMS, extra={"tenant_id": A}),
        "overlays": c.get("/api/v1/admin/overlays", headers=headers, params=FORGED_PARAMS),
        "overlay": c.get(f"/api/v1/admin/overlay/{MEASURE}", headers=headers, params=FORGED_PARAMS),
        "history": c.get(f"/api/v1/admin/overlay/{MEASURE}/history", headers=headers, params=FORGED_PARAMS),
    }
    for name, r in responses.items():
        assert r.status_code == 200, (name, r.text)
        _clean(r.text)
    for name in ("query", "run", "preview", "chat"):
        assert MARK_B in responses[name].text, name
    assert responses["overlays"].json() == {"tenant_id": B, "overlays": []}
    assert responses["overlay"].json()["overlay"] is None
    assert {mark for mark, _ in world.log} == {MARK_B}

    saved = c.put(f"/api/v1/admin/overlay/{MEASURE}", headers=headers, params=FORGED_PARAMS,
                  json={"expr": "FLOOR(GRADE_PERCENTAGE)", "expected_version": 0, "tenant_id": A})
    assert saved.status_code == 200 and saved.json()["tenant_id"] == B
    assert overlay_store.get_overlay(A, MEASURE).expr == "ROUND(GRADE_PERCENTAGE, 0)"


# Routes that run in the warehouse, and routes that do not; both fail closed without a secret.
SECRET_ROUTES = [
    ("POST", "/api/v1/semantic/query", {"json": CONTRACT}),
    ("POST", "/api/v1/semantic/compile", {"json": CONTRACT}),
    ("GET", f"/api/v1/reports/{REPORT}", {}),
    ("POST", f"/api/v1/reports/{REPORT}/run", {"json": {"visual": VISUAL, "query": "main", "values": {"term": ["T"]}}}),
    ("GET", PREVIEW, {}),
    ("POST", "/api/chat", {"json": {"message": "q"}}),
    ("POST", "/api/chat/stream", {"json": {"message": "q"}}),
]
NO_SECRET_ROUTES = [
    ("GET", "/api/v1/semantic/catalog", {}),
    ("GET", "/api/v1/reports", {}),
    ("GET", "/api/v1/me", {}),
    ("GET", "/api/conversations", {}),
    ("GET", "/api/conversations/c1", {}),
    ("DELETE", "/api/conversations/c1", {}),
    ("GET", "/api/v1/admin/overlays", {}),
    ("GET", f"/api/v1/admin/overlay/{MEASURE}", {}),
    ("PUT", f"/api/v1/admin/overlay/{MEASURE}", {"json": {"expr": "FLOOR(GRADE_PERCENTAGE)", "expected_version": 0}}),
    ("DELETE", f"/api/v1/admin/overlay/{MEASURE}?expected_version=1", {}),
    ("GET", f"/api/v1/admin/overlay/{MEASURE}/history", {}),
    ("POST", f"/api/v1/admin/overlay/{MEASURE}/revert", {"json": {"version": 1, "expected_version": 1}}),
    ("POST", "/api/chat/cancel/r1", {}),
]
TENANT_ROUTES = SECRET_ROUTES + NO_SECRET_ROUTES


def _route_id(route):
    return f"{route[0]} {route[1].split('?')[0]}"


@pytest.mark.parametrize("route", SECRET_ROUTES, ids=_route_id)
def test_missing_secret_fails_closed(world, route):
    method, path, kw = route
    assert tenancy.get_tenant(C).tenant_id == C
    r = world.client.request(method, path, headers=AS_C, **kw)
    assert r.status_code == 403 and r.json() == {"detail": NOT_READY}
    assert C not in r.text
    assert world.connects == [] and world.bedrock.calls == []


@pytest.mark.parametrize("route", NO_SECRET_ROUTES, ids=_route_id)
def test_missing_secret_fails_closed_without_snowflake(world, route):
    method, path, kw = route
    assert tenancy.get_tenant(C).tenant_id == C
    r = world.client.request(method, path, headers=AS_C, **kw)
    assert r.status_code == 403 and r.json() == {"detail": NOT_READY}
    assert C not in r.text
    assert overlay_store.list_overlays(C) == []


@pytest.mark.parametrize("route", TENANT_ROUTES, ids=_route_id)
def test_unknown_tenant_403_everywhere(world, route):
    method, path, kw = route
    r = world.client.request(method, path, headers=AS_GHOST, **kw)
    assert r.status_code == 403 and r.json() == {"detail": NOT_READY}
    assert "ghost" not in r.text
    assert world.connects == [] and world.bedrock.calls == []
    assert overlay_store.list_overlays("ghost") == []
