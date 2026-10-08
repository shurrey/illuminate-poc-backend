import pytest
from fastapi.testclient import TestClient

import lambda_handler

AUTH = {"Authorization": "Bearer test"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: {"sub": "bob"} if a else None)
    monkeypatch.setattr(lambda_handler, "_request_owners", {})
    monkeypatch.setattr(lambda_handler, "_cancelled_requests", set())
    return TestClient(lambda_handler.app)


def test_cancel_requires_a_token(client):
    lambda_handler._request_owners["r1"] = "bob"
    assert client.post("/api/chat/cancel/r1").status_code == 401
    assert lambda_handler._cancelled_requests == set()


def test_cannot_cancel_someone_elses_request(client):
    lambda_handler._request_owners["r1"] = "alice"
    assert client.post("/api/chat/cancel/r1", headers=AUTH).status_code == 404
    assert lambda_handler._cancelled_requests == set()


def test_unknown_request_is_404(client):
    assert client.post("/api/chat/cancel/nope", headers=AUTH).status_code == 404


def test_owner_can_cancel(client):
    lambda_handler._request_owners["r1"] = "bob"
    r = client.post("/api/chat/cancel/r1", headers=AUTH)
    assert r.status_code == 200 and r.json()["success"] is True
    assert "r1" in lambda_handler._cancelled_requests


def test_streaming_registers_and_releases_the_request_owner(client, monkeypatch):
    seen = []

    async def fake_stream(message_text, owner, context_id=None, tenant_id=None):
        seen.append(dict(lambda_handler._request_owners))
        yield {"type": "status", "message": "working"}

    monkeypatch.setattr(lambda_handler, "send_message_streaming", fake_stream)
    r = client.post("/api/chat/stream", headers=AUTH, json={"message": "hi", "request_id": "r9"})
    assert r.status_code == 200
    assert seen == [{"r9": "bob"}]
    assert lambda_handler._request_owners == {}
