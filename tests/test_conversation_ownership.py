import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

import conversation_store
import lambda_handler
from tests.tenant_fakes import TENANT_A, install_tenants, user_of

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
            AttributeDefinitions=[{"AttributeName": "context_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(conversation_store, "_table", t)
        yield t


def test_history_is_private_to_its_owner(table):
    conversation_store.save_turn("c1", "alice", "hi", "hello")
    assert [m["content"] for m in conversation_store.load_history("c1", "alice")] == ["hi", "hello"]
    assert conversation_store.load_history("c1", "bob") == []


def test_another_user_cannot_append_to_or_overwrite_a_conversation(table):
    conversation_store.save_turn("c1", "alice", "hi", "hello")
    conversation_store.save_turn("c1", "bob", "mine now", "ok")
    assert [m["content"] for m in conversation_store.load_history("c1", "alice")] == ["hi", "hello"]


def test_only_the_owner_can_clear(table):
    conversation_store.save_turn("c1", "alice", "hi", "hello")
    assert conversation_store.clear_history("c1", "bob") is False
    assert conversation_store.load_history("c1", "alice") != []
    assert conversation_store.clear_history("c1", "alice") is True
    assert conversation_store.load_history("c1", "alice") == []


def test_legacy_items_without_an_owner_are_not_readable(table):
    table.put_item(Item={"context_id": "old", "messages": '[{"role": "user", "content": "x"}]'})
    assert conversation_store.load_history("old", "alice") == []


def test_an_item_with_no_owner_cannot_be_written_by_anyone(table):
    table.put_item(Item={"context_id": "old", "messages": "[]"})
    conversation_store.save_turn("old", "alice", "mine now", "ok")
    conversation_store.save_turn("old", "bob", "no, mine", "ok")
    assert "owner_sub" not in table.get_item(Key={"context_id": "old"})["Item"]
    assert table.get_item(Key={"context_id": "old"})["Item"]["messages"] == "[]"
    assert conversation_store.load_history("old", "alice") == conversation_store.load_history("old", "bob") == []


OWNER_ALICE = f"{TENANT_A.tenant_id}#alice"
OWNER_BOB = f"{TENANT_A.tenant_id}#bob"


@pytest.fixture
def client(table, monkeypatch):
    install_tenants(monkeypatch, TENANT_A)
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user_of(TENANT_A, sub="bob") if a else None)
    return TestClient(lambda_handler.app)


def test_reading_someone_elses_conversation_is_404(client):
    conversation_store.save_turn("c1", OWNER_ALICE, "hi", "hello")
    assert client.get("/api/conversations/c1", headers=AUTH).status_code == 404


def test_deleting_someone_elses_conversation_is_404_and_keeps_it(client):
    conversation_store.save_turn("c1", OWNER_ALICE, "hi", "hello")
    assert client.delete("/api/conversations/c1", headers=AUTH).status_code == 404
    assert conversation_store.load_history("c1", OWNER_ALICE) != []


def test_owner_can_read_their_conversation(client):
    conversation_store.save_turn("c2", OWNER_BOB, "q", "a")
    r = client.get("/api/conversations/c2", headers=AUTH)
    assert r.status_code == 200
    assert [m["content"] for m in r.json()["messages"]] == ["q", "a"]
