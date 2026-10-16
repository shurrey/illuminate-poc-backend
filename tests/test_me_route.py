"""Test GET /api/v1/me endpoint."""
import pytest
from fastapi.testclient import TestClient

import lambda_handler
from tenancy import NOT_READY
from tests.tenant_fakes import TENANT_A, TENANT_B, TENANT_CORE, install_tenants, user_of

AUTH = {"Authorization": "Bearer test"}


@pytest.fixture
def as_user(monkeypatch):
    install_tenants(monkeypatch, TENANT_A, TENANT_B, TENANT_CORE)
    lambda_handler._terms_cache.clear()
    lambda_handler._overlay_cache.clear()

    def login(user):
        monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user)
        return TestClient(lambda_handler.app)

    yield login
    lambda_handler._terms_cache.clear()
    lambda_handler._overlay_cache.clear()


def test_me_returns_tenant_name_and_license_for_tenant_a(as_user):
    r = as_user(user_of(TENANT_A)).get("/api/v1/me", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["tenant"]["name"] == "Alpha University"
    assert body["tenant"]["license"] == "premium"
    assert "tenant_id" not in body
    assert "tenant_id" not in body["tenant"]


def test_me_returns_tenant_name_and_license_for_tenant_b(as_user):
    r = as_user(user_of(TENANT_B)).get("/api/v1/me", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["tenant"]["name"] == "Beta College"
    assert body["tenant"]["license"] == "premium"
    assert "tenant_id" not in body
    assert "tenant_id" not in body["tenant"]


def test_me_returns_core_license(as_user):
    r = as_user(user_of(TENANT_CORE)).get("/api/v1/me", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["tenant"]["name"] == "Core College"
    assert body["tenant"]["license"] == "core"


def test_me_returns_admin_role(as_user):
    r = as_user(user_of(TENANT_A, "illuminate-admins")).get("/api/v1/me", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["is_admin"] is True
    assert "admin" in body["roles"]


def test_me_returns_author_role(as_user):
    r = as_user(user_of(TENANT_A, "illuminate-authors")).get("/api/v1/me", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["is_admin"] is False
    assert "author" in body["roles"]


def test_me_returns_developer_role(as_user):
    r = as_user(user_of(TENANT_A, "illuminate-developers")).get("/api/v1/me", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["is_admin"] is False
    assert "developer" in body["roles"]


def test_me_returns_viewer_role_when_no_groups(as_user):
    r = as_user(user_of(TENANT_A)).get("/api/v1/me", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["is_admin"] is False
    assert "viewer" in body["roles"]


def test_me_returns_multiple_roles(as_user):
    r = as_user(user_of(TENANT_A, "illuminate-admins", "illuminate-authors")).get("/api/v1/me", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["is_admin"] is True
    assert set(body["roles"]) == {"admin", "author"}


def test_me_without_token_is_401(monkeypatch):
    install_tenants(monkeypatch, TENANT_A, TENANT_B, TENANT_CORE)
    lambda_handler._terms_cache.clear()
    lambda_handler._overlay_cache.clear()

    # Patch to return None when authorization is missing
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: None if not a else user_of(TENANT_A))
    client = TestClient(lambda_handler.app)
    r = client.get("/api/v1/me")
    assert r.status_code == 401

    lambda_handler._terms_cache.clear()
    lambda_handler._overlay_cache.clear()


def test_me_not_ready_tenant_is_403(as_user):
    user = {"sub": "u1", "custom:tenant_id": "not-registered"}
    r = as_user(user).get("/api/v1/me", headers=AUTH)
    assert r.status_code == 403
    assert r.json() == {"detail": NOT_READY}
