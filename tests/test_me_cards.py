import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

import lambda_handler
import user_items_store
from semantic_layer.contract import QueryContract
from tenancy import NOT_READY
from tests.tenant_fakes import TENANT_A, TENANT_B, TENANT_CORE, install_tenants, user_of

AUTH = {"Authorization": "Bearer test"}
BUILTIN = ["active-students", "student-engagement", "weekly-engagement",
           "courses-with-activity", "classic-courses", "average-grade"]
NEW = {"label": "Mine", "description": "d", "longDescription": "ld", "prompt": "p",
       "contract": {"metrics": ["metric.reportable_courses.v1"]}, "format": "number"}


@pytest.fixture
def as_user(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    install_tenants(monkeypatch, TENANT_A, TENANT_B, TENANT_CORE)
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="user-items-test",
            KeySchema=[{"AttributeName": "tenant_id", "KeyType": "HASH"}, {"AttributeName": "item_key", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "tenant_id", "AttributeType": "S"}, {"AttributeName": "item_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")
        monkeypatch.setattr(user_items_store, "_table", table)

        def login(user=None):
            monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user or user_of(TENANT_A))
            return TestClient(lambda_handler.app)

        yield login


def ids(r):
    return [c["id"] for c in r.json()["cards"]]


def test_first_user_gets_the_six_defaults(as_user):
    r = as_user().get("/api/v1/me/cards", headers=AUTH)
    assert r.status_code == 200
    assert ids(r) == BUILTIN and r.json()["version"] == 0
    assert all(c["isBuiltIn"] and c["enabled"] for c in r.json()["cards"])
    assert r.json()["cards"][0]["contract"] == {"metrics": ["metric.active_students.v1"]}


def test_default_contracts_validate():
    for card in lambda_handler._default_cards():
        QueryContract.model_validate(card["contract"])


def test_unauthenticated_is_401(as_user, monkeypatch):
    c = as_user()
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: None)
    assert c.get("/api/v1/me/cards").status_code == 401


def test_reorder_disable_hide_and_add(as_user):
    c = as_user()
    made = c.post("/api/v1/me/cards", headers=AUTH, json=NEW)
    assert made.status_code == 200
    cid = made.json()["id"]
    assert cid.startswith("c_") and len(cid) == 14 and made.json()["isBuiltIn"] is False
    got = c.get("/api/v1/me/cards", headers=AUTH)
    assert ids(got) == BUILTIN + [cid]
    version = got.json()["version"]

    put = c.put("/api/v1/me/cards/layout", headers=AUTH, json={
        "order": [cid, "average-grade"], "disabled": ["classic-courses"], "hidden": ["weekly-engagement"],
        "expected_version": version})
    assert put.status_code == 200
    assert ids(put) == [cid, "average-grade", "active-students", "student-engagement",
                        "courses-with-activity", "classic-courses"]
    enabled = {x["id"]: x["enabled"] for x in put.json()["cards"]}
    assert enabled["classic-courses"] is False and enabled["average-grade"] is True
    assert c.get("/api/v1/me/cards", headers=AUTH).json() == put.json()


def test_layout_conflict_is_409(as_user):
    c = as_user()
    body = {"order": [], "disabled": [], "hidden": [], "expected_version": 0}
    assert c.put("/api/v1/me/cards/layout", headers=AUTH, json=body).status_code == 200
    assert c.put("/api/v1/me/cards/layout", headers=AUTH, json=body).status_code == 409


def test_layout_rejects_unknown_ids(as_user):
    c = as_user()
    r = c.put("/api/v1/me/cards/layout", headers=AUTH, json={
        "order": ["nope"], "disabled": [], "hidden": [], "expected_version": 0})
    assert r.status_code == 400 and r.json()["detail"]["errors"]
    r = c.put("/api/v1/me/cards/layout", headers=AUTH, json={
        "order": [], "disabled": [], "hidden": ["c_000000000000"], "expected_version": 0})
    assert r.status_code == 400


def test_new_builtin_appears_and_hidden_stays_hidden(as_user, monkeypatch):
    c = as_user()
    c.put("/api/v1/me/cards/layout", headers=AUTH, json={
        "order": ["average-grade", "active-students"], "disabled": [], "hidden": ["classic-courses"],
        "expected_version": 0})
    extra = {"id": "brand-new", "label": "New", "description": "", "longDescription": "", "prompt": "p",
             "contract": {"metrics": ["metric.active_students.v1"]}, "format": "number"}
    defaults = lambda_handler._default_cards()
    monkeypatch.setattr(lambda_handler, "_default_cards", lambda: defaults + [extra])
    got = ids(c.get("/api/v1/me/cards", headers=AUTH))
    assert got == ["average-grade", "active-students", "student-engagement", "weekly-engagement",
                   "courses-with-activity", "brand-new"]


def test_invalid_contract_is_400(as_user):
    c = as_user()
    for bad in ({"metrics": []}, {"bogus": 1}, "text"):
        r = c.post("/api/v1/me/cards", headers=AUTH, json={**NEW, "contract": bad})
        assert r.status_code == 400 and r.json()["detail"]["errors"]
    assert ids(c.get("/api/v1/me/cards", headers=AUTH)) == BUILTIN


def test_field_validation_is_400(as_user):
    c = as_user()
    for patch in ({"format": "bogus"}, {"label": ""}, {"label": "x" * 201}, {"prompt": "x" * 4001},
                  {"description": "x" * 4001}, {"longDescription": "x" * 4001}, {"label": None}):
        assert c.post("/api/v1/me/cards", headers=AUTH, json={**NEW, **patch}).status_code == 400, patch
    missing = {k: v for k, v in NEW.items() if k != "prompt"}
    assert c.post("/api/v1/me/cards", headers=AUTH, json=missing).status_code == 400


def test_card_limit_is_100(as_user, monkeypatch):
    c = as_user()
    monkeypatch.setattr(user_items_store, "count", lambda *a, **k: 100)
    r = c.post("/api/v1/me/cards", headers=AUTH, json=NEW)
    assert r.status_code == 400 and "100" in r.json()["detail"]["errors"][0]


def test_update_own_card(as_user):
    c = as_user()
    cid = c.post("/api/v1/me/cards", headers=AUTH, json=NEW).json()["id"]
    r = c.put(f"/api/v1/me/cards/{cid}", headers=AUTH, json={**NEW, "label": "Renamed", "reportLink": "/reporting"})
    assert r.status_code == 200 and r.json()["label"] == "Renamed" and r.json()["id"] == cid
    cards = {x["id"]: x for x in c.get("/api/v1/me/cards", headers=AUTH).json()["cards"]}
    assert cards[cid]["label"] == "Renamed" and cards[cid]["reportLink"] == "/reporting"
    bad = c.put(f"/api/v1/me/cards/{cid}", headers=AUTH, json={**NEW, "contract": {"bogus": 1}})
    assert bad.status_code == 400


def test_update_builtin_400_and_unknown_404(as_user):
    c = as_user()
    assert c.put("/api/v1/me/cards/active-students", headers=AUTH, json=NEW).status_code == 400
    assert c.put("/api/v1/me/cards/c_000000000000", headers=AUTH, json=NEW).status_code == 404
    assert c.put("/api/v1/me/cards/bad%23id", headers=AUTH, json=NEW).status_code == 404


def test_delete_custom_removes_it_from_the_layout(as_user):
    c = as_user()
    cid = c.post("/api/v1/me/cards", headers=AUTH, json=NEW).json()["id"]
    assert c.delete(f"/api/v1/me/cards/{cid}", headers=AUTH).status_code == 200
    assert ids(c.get("/api/v1/me/cards", headers=AUTH)) == BUILTIN
    layout = user_items_store.get("tenant-a", "u1", "layout")
    assert cid not in layout["order"]
    assert c.delete(f"/api/v1/me/cards/{cid}", headers=AUTH).status_code == 404


def test_delete_builtin_hides_it_and_reset_restores_order(as_user):
    c = as_user()
    cid = c.post("/api/v1/me/cards", headers=AUTH, json=NEW).json()["id"]
    assert c.delete("/api/v1/me/cards/average-grade", headers=AUTH).status_code == 200
    got = c.get("/api/v1/me/cards", headers=AUTH)
    assert "average-grade" not in ids(got)
    v = got.json()["version"]
    c.put("/api/v1/me/cards/layout", headers=AUTH, json={
        "order": [cid, "classic-courses"], "disabled": ["active-students"], "hidden": ["average-grade"],
        "expected_version": v})
    r = c.post("/api/v1/me/cards/reset", headers=AUTH)
    assert r.status_code == 200
    assert ids(r) == BUILTIN + [cid]
    assert all(x["enabled"] for x in r.json()["cards"])


def test_not_ready_tenant_is_403(as_user):
    c = as_user({"sub": "u1", "custom:tenant_id": "ghost"})
    for method, path in (("GET", "/api/v1/me/cards"), ("POST", "/api/v1/me/cards/reset"),
                         ("DELETE", "/api/v1/me/cards/average-grade")):
        r = c.request(method, path, headers=AUTH)
        assert r.status_code == 403 and r.json() == {"detail": NOT_READY}


def test_layout_lists_are_deduplicated_and_capped(as_user):
    c = as_user()
    r = c.put("/api/v1/me/cards/layout", headers=AUTH, json={
        "order": ["average-grade", "active-students", "average-grade"], "disabled": ["active-students"] * 5,
        "hidden": [], "expected_version": 0})
    assert r.status_code == 200
    assert ids(r)[:2] == ["average-grade", "active-students"] and len(ids(r)) == 6
    assert user_items_store.get("tenant-a", "u1", "layout")["disabled"] == ["active-students"]
    r = c.put("/api/v1/me/cards/layout", headers=AUTH, json={
        "order": ["average-grade"] * 107, "disabled": [], "hidden": [], "expected_version": 1})
    assert r.status_code == 400 and "106" in r.json()["detail"]["errors"][0]


@pytest.mark.parametrize("link", ["https://evil.example", "//evil.example", "reporting", "javascript:alert(1)", "/\\evil", 5])
def test_report_link_must_be_app_relative(as_user, link):
    r = as_user().post("/api/v1/me/cards", headers=AUTH, json={**NEW, "reportLink": link})
    assert r.status_code == 400 and "reportLink" in r.json()["detail"]["errors"][0]


def _conflict_layout_writes(monkeypatch, times):
    real, left = user_items_store.put, [times]

    def put(tenant_id, sub, kind, id, body, **kw):
        if kind == "layout" and left[0] > 0:
            left[0] -= 1
            raise user_items_store.Conflict("layout")
        return real(tenant_id, sub, kind, id, body, **kw)

    monkeypatch.setattr(user_items_store, "put", put)


def test_create_retries_a_layout_conflict(as_user, monkeypatch):
    c = as_user()
    _conflict_layout_writes(monkeypatch, 1)
    r = c.post("/api/v1/me/cards", headers=AUTH, json=NEW)
    assert r.status_code == 200
    assert ids(c.get("/api/v1/me/cards", headers=AUTH)) == BUILTIN + [r.json()["id"]]


def test_create_that_keeps_conflicting_leaves_no_orphan(as_user, monkeypatch):
    c = as_user()
    _conflict_layout_writes(monkeypatch, 3)
    assert c.post("/api/v1/me/cards", headers=AUTH, json=NEW).status_code == 409
    assert user_items_store.list_kind("tenant-a", "u1", "card") == []


def test_delete_and_reset_retry_a_layout_conflict(as_user, monkeypatch):
    c = as_user()
    _conflict_layout_writes(monkeypatch, 1)
    assert c.delete("/api/v1/me/cards/average-grade", headers=AUTH).status_code == 200
    _conflict_layout_writes(monkeypatch, 2)
    r = c.post("/api/v1/me/cards/reset", headers=AUTH)
    assert r.status_code == 200 and ids(r) == BUILTIN


def test_cards_view_lists_hidden_and_disabled(as_user):
    c = as_user()
    first = c.get("/api/v1/me/cards", headers=AUTH).json()
    assert first["hidden"] == [] and first["disabled"] == []
    put = c.put("/api/v1/me/cards/layout", headers=AUTH, json={
        "order": [], "disabled": ["classic-courses", "active-students"],
        "hidden": ["average-grade", "student-engagement"], "expected_version": 0}).json()
    assert put["hidden"] == ["student-engagement", "average-grade"]
    assert put["disabled"] == ["active-students", "classic-courses"]
    assert c.get("/api/v1/me/cards", headers=AUTH).json() == put


def test_every_cards_view_route_returns_hidden(as_user):
    c = as_user()
    cid = c.post("/api/v1/me/cards", headers=AUTH, json=NEW).json()["id"]
    hidden = c.delete("/api/v1/me/cards/average-grade", headers=AUTH).json()
    assert hidden["hidden"] == ["average-grade"]
    assert c.delete(f"/api/v1/me/cards/{cid}", headers=AUTH).json()["hidden"] == ["average-grade"]
    reset = c.post("/api/v1/me/cards/reset", headers=AUTH).json()
    assert reset["hidden"] == [] and reset["disabled"] == []


def test_hidden_omits_ids_that_are_no_longer_built_in(as_user, monkeypatch):
    c = as_user()
    c.put("/api/v1/me/cards/layout", headers=AUTH, json={
        "order": [], "disabled": [], "hidden": ["classic-courses"], "expected_version": 0})
    defaults = [d for d in lambda_handler._default_cards() if d["id"] != "classic-courses"]
    monkeypatch.setattr(lambda_handler, "_default_cards", lambda: defaults)
    assert c.get("/api/v1/me/cards", headers=AUTH).json()["hidden"] == []
