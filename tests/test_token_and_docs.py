import importlib

import pytest
from fastapi.testclient import TestClient

import lambda_handler


@pytest.fixture
def decoding(monkeypatch):
    monkeypatch.setattr(lambda_handler, "USER_POOL_CLIENT_ID", "client-1")
    monkeypatch.setattr(lambda_handler, "_get_jwks", lambda: {"keys": [{"kid": "k1"}]})
    monkeypatch.setattr(lambda_handler.jwt, "get_unverified_header", lambda token: {"kid": "k1"})

    def use(claims):
        monkeypatch.setattr(lambda_handler.jwt, "decode", lambda *a, **k: claims)
    return use


def test_id_tokens_for_this_client_are_accepted(decoding):
    decoding({"token_use": "id", "aud": "client-1", "sub": "u1"})
    assert lambda_handler._validate_token("t")["sub"] == "u1"


@pytest.mark.parametrize("claims", [
    {"token_use": "access", "client_id": "client-1", "sub": "u1"},
    {"aud": "client-1", "sub": "u1"},
    {"token_use": "id", "aud": "other-client", "sub": "u1"},
])
def test_access_tokens_untyped_tokens_and_other_clients_are_rejected(decoding, claims):
    decoding(claims)
    assert lambda_handler._validate_token("t") is None


def test_api_docs_are_off_unless_enabled(monkeypatch):
    try:
        monkeypatch.setenv("API_DOCS", "off")
        importlib.reload(lambda_handler)
        client = TestClient(lambda_handler.app)
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        monkeypatch.setenv("API_DOCS", "on")
        importlib.reload(lambda_handler)
        assert TestClient(lambda_handler.app).get("/openapi.json").status_code == 200
    finally:
        monkeypatch.delenv("API_DOCS", raising=False)
        importlib.reload(lambda_handler)
