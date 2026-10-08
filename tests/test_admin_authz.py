import pytest
from fastapi.testclient import TestClient

import lambda_handler
import tenant_store
from semantic_layer.models import Glossary, Tenant

AUTH = {"Authorization": "Bearer test"}
ADMIN = {"sub": "a1", "custom:tenant_id": "t1", "cognito:groups": ["illuminate-admins"]}
MEMBER = {"sub": "u1", "custom:tenant_id": "t1"}

ADMIN_ROUTES = [
    ("get", "/api/v1/admin/metrics", None),
    ("get", "/api/v1/admin/overlay/metric.student_count.v1", None),
    ("put", "/api/v1/admin/overlay/metric.student_count.v1",
     {"measure_sql": "SELECT 1", "diff_description": "x"}),
    ("delete", "/api/v1/admin/overlay/metric.student_count.v1", None),
]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(tenant_store, "load_tenant",
                        lambda tid: Tenant(id=tid, display_name=tid, overlays={}, glossary=Glossary(synonyms={})))
    monkeypatch.setattr(tenant_store, "get_overlay", lambda tid, mid: None)
    monkeypatch.setattr(tenant_store, "put_overlay", lambda *a, **k: None)
    monkeypatch.setattr(tenant_store, "delete_overlay", lambda tid, mid: None)
    return TestClient(lambda_handler.app)


def _as(monkeypatch, user):
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user if a else None)


@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES)
def test_admin_routes_reject_non_admins(client, monkeypatch, method, path, body):
    _as(monkeypatch, MEMBER)
    r = client.request(method, path, headers=AUTH, json=body)
    assert r.status_code == 403
    assert "illuminate-admins" in r.json()["detail"]


def test_admin_can_list_metrics(client, monkeypatch):
    _as(monkeypatch, ADMIN)
    r = client.get("/api/v1/admin/metrics", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["tenant_id"] == "t1"


def test_admin_without_tenant_is_still_rejected(client, monkeypatch):
    _as(monkeypatch, {"sub": "a1", "cognito:groups": ["illuminate-admins"]})
    assert client.get("/api/v1/admin/metrics", headers=AUTH).status_code == 403


@pytest.mark.parametrize("method,path", [
    ("get", "/api/v1/admin/metrics"),
    ("post", "/api/v1/semantic/compile"),
    ("get", "/api/v1/semantic/catalog"),
    ("get", "/api/conversations/abc"),
])
def test_missing_authorization_header_is_401(client, method, path):
    r = client.request(method, path, json={"metrics": ["metric.reportable_courses.v1"]})
    assert r.status_code == 401
