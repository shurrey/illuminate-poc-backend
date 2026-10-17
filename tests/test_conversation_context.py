import json

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

import conversation_store
import lambda_handler
from tests.tenant_fakes import TENANT_A, install_tenants, user_of

AUTH = {"Authorization": "Bearer test"}
SQL_ARTIFACT = {"id": "s1", "type": "sql", "title": "Courses by term", "data": "SELECT 1",
                "query": {"metrics": ["metric.reportable_courses.v1"], "dimensions": ["term_name"]},
                "provenance": {"governed": True}}


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        t = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="conversations-test",
            KeySchema=[{"AttributeName": "context_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "context_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(conversation_store, "_table", t)
        yield t


@pytest.fixture
def engine(monkeypatch):
    import chat_engine
    seen = []

    async def fake_stream(message, history, **kw):
        seen.append((message, history))
        yield {"type": "raw_complete", "text": f"answer {len(seen)}", "messages": [], "artifacts": [SQL_ARTIFACT]}

    monkeypatch.setattr(chat_engine, "send_message_streaming", fake_stream)
    return seen


@pytest.fixture
def client(table, monkeypatch):
    install_tenants(monkeypatch, TENANT_A)
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user_of(TENANT_A, sub="bob") if a else None)
    return TestClient(lambda_handler.app)


def _complete(r):
    return [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")][-1]["data"]


def test_turns_keep_the_queries_behind_each_answer(table):
    conversation_store.save_turn("c1", "tenant-a#bob", "q", "a", queries=[{"title": "t", "query": {"metrics": ["m"]}}])
    history = conversation_store.load_history("c1", "tenant-a#bob")
    assert history[1]["queries"] == [{"title": "t", "query": {"metrics": ["m"]}}]


def test_follow_up_questions_see_the_previous_query_contract_in_the_new_user_turn(client, engine):
    client.post("/api/chat/stream", headers=AUTH, json={"message": "courses by term", "context_id": "c9"})
    client.post("/api/chat/stream", headers=AUTH, json={"message": "now by month", "context_id": "c9"})
    message, history = engine[1]
    assert history[1]["content"][0]["text"] == "answer 1"
    assert message.endswith("now by month")
    assert message.startswith("<previous_queries>") and '"metric.reportable_courses.v1"' in message


def test_earlier_queries_are_replayed_on_the_user_turn_that_followed_them(client, engine):
    for q in ("one", "two", "three"):
        client.post("/api/chat/stream", headers=AUTH, json={"message": q, "context_id": "c8"})
    _, history = engine[2]
    assert history[0]["content"][0]["text"] == "one"
    assert history[2]["content"][0]["text"].startswith("<previous_queries>")
    assert all("previous_queries" not in m["content"][0]["text"] for m in history if m["role"] == "assistant")


def test_a_blank_saved_answer_does_not_send_blank_text_to_the_model(client, engine):
    conversation_store.save_turn("c7", "tenant-a#bob", "q", "")
    client.post("/api/chat/stream", headers=AUTH, json={"message": "again", "context_id": "c7"})
    _, history = engine[0]
    assert all(block["text"].strip() for m in history for block in m["content"])


def test_a_new_conversation_gets_a_server_generated_id(client, engine):
    data = _complete(client.post("/api/chat/stream", headers=AUTH, json={"message": "hi"}))
    assert data["contextId"] and conversation_store.owns(data["contextId"], "tenant-a#bob")


def test_someone_elses_conversation_id_is_replaced(client, engine):
    conversation_store.save_turn("alice-ctx", "alice", "secret q", "secret a")
    data = _complete(client.post("/api/chat/stream", headers=AUTH, json={"message": "hi", "context_id": "alice-ctx"}))
    assert data["contextId"] != "alice-ctx"
    assert engine[0] == ("hi", [])
    assert [m["content"] for m in conversation_store.load_history("alice-ctx", "alice")] == ["secret q", "secret a"]


def test_history_is_trimmed_to_whole_turns_starting_with_the_user(table, monkeypatch):
    monkeypatch.setattr(conversation_store, "_MAX_MESSAGES", 3)
    conversation_store.save_turn("c5", "tenant-a#bob", "q1", "a1")
    conversation_store.save_turn("c5", "tenant-a#bob", "q2", "a2")
    history = conversation_store.load_history("c5", "tenant-a#bob")
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[0]["content"] == "q2"


def test_conversation_storage_runs_off_the_event_loop(client, monkeypatch):
    import threading
    import chat_engine

    threads = []
    monkeypatch.setattr(conversation_store, "load_history", lambda cid, owner: threads.append(threading.get_ident()) or [])
    monkeypatch.setattr(conversation_store, "save_turn", lambda *a, **k: threads.append(threading.get_ident()))

    async def fake_stream(message, history, **kw):
        threads.append(("loop", threading.get_ident()))
        yield {"type": "raw_complete", "text": "Done.", "messages": [], "artifacts": []}

    monkeypatch.setattr(chat_engine, "send_message_streaming", fake_stream)
    client.post("/api/chat/stream", headers=AUTH, json={"message": "q"})
    loop_thread = next(t[1] for t in threads if isinstance(t, tuple))
    assert all(t != loop_thread for t in threads if not isinstance(t, tuple))


PARTS = {"governed_1": {"metrics": ["metric.reportable_courses.v1"], "dimensions": ["term_name"]}}
OUTER_SQL = "SELECT term_name, reportable_courses FROM governed_1"


def _sql_artifact(governance):
    return {"id": "s2", "type": "sql", "title": "Courses", "data": OUTER_SQL, "sql": OUTER_SQL,
            "executed_sql": "WITH governed_1 AS (SELECT 1) SELECT term_name FROM governed_1",
            "governed_parts": PARTS, "provenance": {"governed": governance == "full", "governance": governance}}


def test_a_fully_governed_sql_answer_keeps_its_outer_sql_and_parts_not_a_query_contract():
    assert lambda_handler._queries_from([_sql_artifact("full")]) == [
        {"title": "Courses", "governed": True, "governance": "full", "sql": OUTER_SQL, "governed_parts": PARTS}]


def test_an_ungoverned_sql_answer_is_stored_with_no_parts():
    artifact = {"id": "s3", "type": "sql", "title": "Raw", "data": "SELECT 1", "sql": "SELECT 1",
                "provenance": {"governed": False, "governance": "none"}}
    assert lambda_handler._queries_from([artifact]) == [
        {"title": "Raw", "governed": False, "governance": "none", "sql": "SELECT 1", "governed_parts": {}}]


def test_a_truthy_non_boolean_governed_flag_is_not_a_governed_query():
    artifact = {**SQL_ARTIFACT, "provenance": {"governed": "yes"}}
    assert lambda_handler._queries_from([artifact])[0]["governed"] is False


def test_the_stored_conversation_keeps_the_governed_parts_for_the_follow_up(client, monkeypatch):
    import chat_engine
    seen = []

    async def fake_stream(message, history, **kw):
        seen.append(message)
        yield {"type": "raw_complete", "text": "answer", "messages": [], "artifacts": [_sql_artifact("partial")]}

    monkeypatch.setattr(chat_engine, "send_message_streaming", fake_stream)
    client.post("/api/chat/stream", headers=AUTH, json={"message": "mixed", "context_id": "c6"})
    stored = conversation_store.load_history("c6", "tenant-a#bob")[1]["queries"]
    assert stored == [{"title": "Courses", "governed": False, "governance": "partial", "sql": OUTER_SQL,
                       "governed_parts": PARTS}]
    client.post("/api/chat/stream", headers=AUTH, json={"message": "again", "context_id": "c6"})
    assert '"governed_parts": {"governed_1"' in seen[1] and "executed_sql" not in seen[1]
