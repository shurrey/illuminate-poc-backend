import pytest
from fastapi.testclient import TestClient

import lambda_handler

AUTH = {"Authorization": "Bearer test"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: {"sub": "u1"})
    return TestClient(lambda_handler.app)


def test_returns_the_catalog_with_an_etag(client):
    r = client.get("/api/v1/semantic/catalog", headers=AUTH)
    assert r.status_code == 200
    assert "dataset.course_filters.v1" in [d["id"] for d in r.json()["datasets"]]
    assert r.headers["etag"].startswith('"')


def test_matching_etag_is_304(client):
    etag = client.get("/api/v1/semantic/catalog", headers=AUTH).headers["etag"]
    r = client.get("/api/v1/semantic/catalog", headers=AUTH | {"If-None-Match": etag})
    assert r.status_code == 304
    assert r.content == b""


def test_requires_a_valid_token(client, monkeypatch):
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: None)
    assert client.get("/api/v1/semantic/catalog", headers=AUTH).status_code == 401
