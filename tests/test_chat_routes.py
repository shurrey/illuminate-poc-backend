import json
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import lambda_handler
import snowflake_client
from tests.tenant_fakes import TENANT_A, install_tenants, user_of

AUTH = {"Authorization": "Bearer test"}
ARTIFACT = {"id": "a1", "type": "table", "title": "t",
            "data": {"columns": ["N", "D"], "rows": [{"N": Decimal("0.25"), "D": date(2026, 9, 1)}]},
            "provenance": {"governed": True}}


@pytest.fixture
def client(monkeypatch):
    import conversation_store
    install_tenants(monkeypatch, TENANT_A)
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user_of(TENANT_A) if a else None)
    monkeypatch.setattr(conversation_store, "load_history", lambda cid, owner: [])
    monkeypatch.setattr(conversation_store, "save_turn", lambda *a, **k: None)
    return TestClient(lambda_handler.app)


def _events(body: str) -> list[dict]:
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


def test_stream_completes_with_tool_artifacts_serialised_as_json(client, monkeypatch):
    import chat_engine

    async def fake_stream(message, history, **kw):
        yield {"type": "status", "message": "Running a governed query..."}
        yield {"type": "raw_complete", "text": "Answer for jane@example.edu", "messages": [], "artifacts": [ARTIFACT]}

    monkeypatch.setattr(chat_engine, "send_message_streaming", fake_stream)
    r = client.post("/api/chat/stream", headers=AUTH, json={"message": "q"})
    complete = _events(r.text)[-1]
    assert complete["type"] == "complete"
    assert complete["data"]["artifacts"][0]["data"]["rows"] == [{"N": 0.25, "D": "2026-09-01"}]
    assert "[EMAIL REDACTED]" in complete["data"]["text"]


def test_non_streaming_chat_returns_tool_artifacts(client, monkeypatch):
    import chat_engine

    monkeypatch.setattr(chat_engine, "send_message", lambda message, history, **kw: ("Three.", [], [ARTIFACT]))
    r = client.post("/api/chat", headers=AUTH, json={"message": "q"})
    assert r.status_code == 200
    assert r.json()["artifacts"][0]["id"] == "a1"
    assert r.json()["artifacts"][0]["data"]["rows"] == [{"N": 0.25, "D": "2026-09-01"}]


@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
def test_binary_and_nan_values_in_artifacts_do_not_break_either_chat_path(client, monkeypatch, path):
    import chat_engine

    artifact = {**ARTIFACT, "data": {"columns": ["B", "F"], "rows": [{"B": b"\xff", "F": float("nan")}]}}

    async def fake_stream(message, history, **kw):
        yield {"type": "raw_complete", "text": "Done.", "messages": [], "artifacts": [artifact]}

    monkeypatch.setattr(chat_engine, "send_message_streaming", fake_stream)
    monkeypatch.setattr(chat_engine, "send_message", lambda message, history, **kw: ("Done.", [], [artifact]))
    r = client.post(path, headers=AUTH, json={"message": "q"})
    assert r.status_code == 200
    body = r.json() if path == "/api/chat" else _events(r.text)[-1]["data"]
    assert body["artifacts"][0]["data"]["rows"] == [{"B": "ff", "F": None}]


@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
def test_chat_queries_use_the_callers_tenant_overlays(client, monkeypatch, path):
    import chat_engine
    from semantic_layer.overlays import Overlay

    target = "measure:dataset.student_grade.v1:average_grade_percentage"
    install_tenants(monkeypatch, TENANT_A)
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user_of(TENANT_A))
    import overlay_store
    monkeypatch.setattr(overlay_store, "list_overlays",
                        lambda tid: [Overlay(target=target, expr="ROUND(GRADE_PERCENTAGE, 0)", version=2)])
    lambda_handler._overlay_cache.clear()
    seen = {}

    async def fake_stream(message, history, **kw):
        seen.update(kw)
        yield {"type": "raw_complete", "text": "Done.", "messages": [], "artifacts": []}

    def fake_send(message, history, **kw):
        seen.update(kw)
        return "Done.", [], []

    monkeypatch.setattr(chat_engine, "send_message_streaming", fake_stream)
    monkeypatch.setattr(chat_engine, "send_message", fake_send)
    client.post(path, headers=AUTH, json={"message": "q"})
    measure = seen["tools"].catalog.datasets["dataset.student_grade.v1"].measure("average_grade_percentage")
    assert measure.expr == "ROUND(GRADE_PERCENTAGE, 0)"


def test_chat_prompt_lists_the_tenants_own_filters(client, monkeypatch):
    import chat_engine
    import overlay_store
    from semantic_layer.overlays import Overlay

    install_tenants(monkeypatch, TENANT_A)
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user_of(TENANT_A))
    monkeypatch.setattr(overlay_store, "list_overlays",
                        lambda tid: [Overlay(target="filter:dataset.student_grade.v1:honours", sql="GRADE_PERCENTAGE >= 90", version=1)])
    lambda_handler._overlay_cache.clear()
    seen = {}

    def fake_send(message, history, **kw):
        seen.update(kw)
        return "Done.", [], []

    monkeypatch.setattr(chat_engine, "send_message", fake_send)
    client.post("/api/chat", headers=AUTH, json={"message": "q"})
    assert "honours" in seen["system_prompt"]


@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
def test_stored_replies_are_pii_scrubbed(client, monkeypatch, path):
    import chat_engine
    import conversation_store
    saved = []
    monkeypatch.setattr(conversation_store, "save_turn", lambda cid, owner, q, a, *rest: saved.append(a))

    async def fake_stream(message, history, **kw):
        yield {"type": "raw_complete", "text": "Reach jane@example.edu", "messages": [], "artifacts": []}

    monkeypatch.setattr(chat_engine, "send_message_streaming", fake_stream)
    monkeypatch.setattr(chat_engine, "send_message", lambda m, h, **kw: ("Reach jane@example.edu", [], []))
    client.post(path, headers=AUTH, json={"message": "q"})
    assert saved == ["Reach [EMAIL REDACTED]"]


ADMIN = "illuminate-admins"
PII_CONTRACT = {"measures": ["dataset.course_student_activity.v1:students"], "dimensions": ["person_email"],
                "filters": [{"dimension": "person_email", "op": "eq", "values": ["jane@example.edu"]}]}


@pytest.mark.parametrize("groups, expected", [((ADMIN,), True), ((), False)])
def test_stream_route_gives_the_tools_the_callers_identity_right(client, monkeypatch, groups, expected):
    import chat_engine

    seen = []

    async def fake_stream(message, history, **kw):
        seen.append(kw["tools"].allow_identity)
        yield {"type": "raw_complete", "text": "ok", "messages": [], "artifacts": []}

    monkeypatch.setattr(chat_engine, "send_message_streaming", fake_stream)
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user_of(TENANT_A, *groups))
    client.post("/api/chat/stream", headers=AUTH, json={"message": "q"})
    assert seen == [expected]


@pytest.fixture(autouse=True)
def _tenant_database(monkeypatch):
    import snowflake_client
    monkeypatch.setattr(snowflake_client, "tenant_database", lambda tenant: "DB_A")


def _admin_tools():
    return lambda_handler._engine_kwargs(TENANT_A, user_of(TENANT_A, ADMIN))["tools"]


def test_chat_identity_audit_logs_names_never_filter_values(caplog):
    from semantic_layer.contract import QueryContract

    tools = _admin_tools()
    tools.execute = lambda v, ctx: snowflake_client.Result(columns=["N"], rows=[{"N": 1}])
    with caplog.at_level("INFO", logger="API-PROXY"):
        tools.dispatch("query_semantic", PII_CONTRACT)
        lambda_handler._compile_for(QueryContract(**PII_CONTRACT), user_of(TENANT_A, ADMIN), TENANT_A)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("identity_query")]
    assert len(lines) == 2
    assert all("person_email" in line and "jane@example.edu" not in line for line in lines)


@pytest.mark.parametrize("projection, result, logged", [
    ("EMAIL", snowflake_client.Result(columns=["N"], rows=[{"N": 1}]), True),
    ("*", None, False),
    ("EMAIL", snowflake_client.Result(columns=[], rows=[], error="boom"), True),
])
def test_freehand_identity_audit_logs_only_after_the_guard_accepts_and_never_the_sql(caplog, projection, result, logged):
    tools = _admin_tools()
    tools.execute = lambda v, ctx: result or pytest.fail("the guard accepted a star projection")
    sql = f"SELECT {projection} FROM CDM_LMS.PERSON WHERE EMAIL = 'jane@example.edu'"
    with caplog.at_level("INFO", logger="API-PROXY"):
        tools.dispatch("execute_sql", {"sql": sql, "reason": "no governed metric covers this"}, called=("search_catalog",))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("identity_query")]
    assert bool(lines) is logged
    assert not any("jane@example.edu" in line or "EMAIL" in line for line in lines)
