import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

import conversation_store
import lambda_handler

AUTH = {"Authorization": "Bearer test"}


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


@pytest.fixture
def clock(monkeypatch):
    now = [1000]
    monkeypatch.setattr(conversation_store.time, "time", lambda: now[0])
    return now


def test_lists_only_the_owners_conversations_newest_first(table, clock):
    conversation_store.save_turn("c1", "alice", "How many courses?", "5,183")
    clock[0] += 10
    conversation_store.save_turn("c2", "bob", "Bob's question", "answer")
    conversation_store.save_turn("c3", "alice", "Average grade by term", "57")
    assert [(c["context_id"], c["title"]) for c in conversation_store.list_conversations("alice")] == [
        ("c3", "Average grade by term"), ("c1", "How many courses?")]


def test_the_title_is_the_first_question_and_survives_later_turns(table, clock):
    conversation_store.save_turn("c1", "alice", "How many courses?" + "x" * 100, "5,183")
    conversation_store.save_turn("c1", "alice", "Now by term", "one row")
    [conv] = conversation_store.list_conversations("alice")
    assert conv["title"] == ("How many courses?" + "x" * 100)[:80]
    assert conv["updated_at"] == 1000


def test_conversations_route_lists_the_callers_conversations(table, monkeypatch):
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: {"sub": "alice"} if a else None)
    conversation_store.save_turn("c1", "alice", "How many courses?", "5,183")
    client = TestClient(lambda_handler.app)
    assert client.get("/api/conversations").status_code == 401
    body = client.get("/api/conversations", headers=AUTH).json()
    assert [c["context_id"] for c in body["conversations"]] == ["c1"]
