import json
import logging

import pytest
from fastapi.testclient import TestClient

import chat_engine
import lambda_handler
from tests.tenant_fakes import TENANT_A, TENANT_B, install_tenants, user_of

AUTH = {"Authorization": "Bearer test"}
SECRET = "Snowflake said: object DB_ALPHA.SECRET_SCHEMA.T does not exist for role R_ALPHA"
GENERIC = "Something went wrong; try again."


@pytest.fixture
def world(monkeypatch):
    import conversation_store

    install_tenants(monkeypatch, TENANT_A, TENANT_B)
    monkeypatch.setattr(conversation_store, "load_history", lambda cid, owner: [])
    monkeypatch.setattr(conversation_store, "save_turn", lambda *a, **k: None)
    monkeypatch.setattr(lambda_handler, "_request_owners", set())
    monkeypatch.setattr(lambda_handler, "_cancelled_requests", set())

    def login(user=None):
        monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: (user or user_of(TENANT_A)) if a else None)
        return TestClient(lambda_handler.app)

    return login


def _events(body: str) -> list[dict]:
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


def _raise(*a, **k):
    raise RuntimeError(SECRET)


def test_a_chat_exception_is_not_echoed_but_is_logged(world, monkeypatch, caplog):
    monkeypatch.setattr(chat_engine, "send_message", _raise)
    with caplog.at_level(logging.ERROR):
        r = world().post("/api/chat", headers=AUTH, json={"message": "q"})
    assert (r.status_code, r.json()) == (502, {"detail": GENERIC})
    assert SECRET in caplog.text


def test_a_streaming_engine_exception_is_not_echoed_but_is_logged(world, monkeypatch, caplog):
    async def failing(message, history, **kw):
        yield {"type": "status", "message": "working"}
        raise RuntimeError(SECRET)

    monkeypatch.setattr(chat_engine, "send_message_streaming", failing)
    with caplog.at_level(logging.ERROR):
        r = world().post("/api/chat/stream", headers=AUTH, json={"message": "q"})
    assert r.status_code == 200 and SECRET not in r.text
    assert _events(r.text)[-1] == {"type": "error", "message": GENERIC}
    assert SECRET in caplog.text


def test_a_relay_exception_is_not_echoed_but_is_logged(world, monkeypatch, caplog):
    async def failing(message_text, owner, context_id=None, **kw):
        yield {"type": "status", "message": "working"}
        raise RuntimeError(SECRET)

    monkeypatch.setattr(lambda_handler, "send_message_streaming", failing)
    with caplog.at_level(logging.ERROR):
        r = world().post("/api/chat/stream", headers=AUTH, json={"message": "q"})
    assert SECRET not in r.text and _events(r.text)[-1] == {"type": "error", "message": GENERIC}
    assert SECRET in caplog.text


# --- cancel keyed by (owner, request id) ----------------------------------------------------------------------------

def test_cancel_by_another_owner_with_the_same_request_id_does_nothing(world):
    alice, bob = user_of(TENANT_A, sub="alice"), user_of(TENANT_A, sub="bob")
    lambda_handler._request_owners.add(("tenant-a#alice", "r1"))
    r = world(bob).post("/api/chat/cancel/r1", headers=AUTH)
    assert r.status_code == 404 and lambda_handler._cancelled_requests == set()
    assert world(alice).post("/api/chat/cancel/r1", headers=AUTH).status_code == 200
    assert lambda_handler._cancelled_requests == {("tenant-a#alice", "r1")}


def test_the_same_sub_in_another_tenant_cannot_cancel(world):
    lambda_handler._request_owners.add(("tenant-a#u1", "r1"))
    r = world(user_of(TENANT_B)).post("/api/chat/cancel/r1", headers=AUTH)
    assert r.status_code == 404 and lambda_handler._cancelled_requests == set()


def test_a_second_stream_with_the_same_request_id_does_not_take_over_the_first(world, monkeypatch):
    """Bob streams r1 while Alice's r1 is in progress; Bob's cancel stops only Bob's, and his stream ending
    leaves Alice's registration in place."""
    lambda_handler._request_owners.add(("tenant-a#alice", "r1"))
    seen = []

    async def fake_stream(message_text, owner, context_id=None, **kw):
        seen.append(set(lambda_handler._request_owners))
        lambda_handler._cancelled_requests.add(("tenant-a#alice", "r1"))
        yield {"type": "status", "message": "working"}

    monkeypatch.setattr(lambda_handler, "send_message_streaming", fake_stream)
    r = world(user_of(TENANT_A, sub="bob")).post("/api/chat/stream", headers=AUTH,
                                                  json={"message": "hi", "request_id": "r1"})
    assert seen == [{("tenant-a#alice", "r1"), ("tenant-a#bob", "r1")}]
    assert [e["type"] for e in _events(r.text)] == ["status"]
    assert lambda_handler._request_owners == {("tenant-a#alice", "r1")}
    assert lambda_handler._cancelled_requests == {("tenant-a#alice", "r1")}
