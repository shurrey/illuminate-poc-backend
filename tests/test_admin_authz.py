import pytest
from fastapi.testclient import TestClient

import lambda_handler
import overlay_store

AUTH = {"Authorization": "Bearer test"}
ADMIN = {"sub": "a1", "custom:tenant_id": "t1", "cognito:groups": ["illuminate-admins"]}
MEMBER = {"sub": "u1", "custom:tenant_id": "t1"}

TARGET = "measure:dataset.student_grade.v1:average_grade_percentage"
ADMIN_ROUTES = [
    ("get", "/api/v1/admin/overlays", None),
    ("get", f"/api/v1/admin/overlay/{TARGET}", None),
    ("put", f"/api/v1/admin/overlay/{TARGET}", {"expr": "1", "expected_version": 0}),
    ("delete", f"/api/v1/admin/overlay/{TARGET}?expected_version=1", None),
    ("get", f"/api/v1/admin/overlay/{TARGET}/history", None),
    ("post", f"/api/v1/admin/overlay/{TARGET}/revert", {"version": 1, "expected_version": 1}),
]


def _never(*a, **k):
    raise AssertionError("the overlay store must not be touched")


@pytest.fixture
def client(monkeypatch):
    for name in ("list_overlays", "get_overlay", "history", "put_overlay", "revert", "delete_overlay"):
        monkeypatch.setattr(overlay_store, name, _never)
    return TestClient(lambda_handler.app)


def _as(monkeypatch, user):
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user if a else None)


@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES)
def test_admin_routes_reject_non_admins(client, monkeypatch, method, path, body):
    _as(monkeypatch, MEMBER)
    r = client.request(method, path, headers=AUTH, json=body)
    assert r.status_code == 403
    assert "illuminate-admins" in r.json()["detail"]


def test_admin_can_list_overlays(client, monkeypatch):
    monkeypatch.setattr(overlay_store, "list_overlays", lambda tid: [])
    _as(monkeypatch, ADMIN)
    r = client.get("/api/v1/admin/overlays", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["tenant_id"] == "t1"


def test_admin_without_tenant_is_still_rejected(client, monkeypatch):
    _as(monkeypatch, {"sub": "a1", "cognito:groups": ["illuminate-admins"]})
    assert client.get("/api/v1/admin/overlays", headers=AUTH).status_code == 403


@pytest.mark.parametrize("method,path", [
    ("get", "/api/v1/admin/overlays"),
    ("post", "/api/v1/semantic/compile"),
    ("get", "/api/v1/semantic/catalog"),
    ("get", "/api/conversations/abc"),
])
def test_missing_authorization_header_is_401(client, method, path):
    r = client.request(method, path, json={"metrics": ["metric.reportable_courses.v1"]})
    assert r.status_code == 401
