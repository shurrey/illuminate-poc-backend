import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

import lambda_handler
import overlay_store

AUTH = {"Authorization": "Bearer test"}
ADMIN = {"sub": "a1", "custom:tenant_id": "t1", "cognito:groups": ["illuminate-admins"]}
TARGET = "measure:dataset.student_grade.v1:average_grade_percentage"
URL = f"/api/v1/admin/overlay/{TARGET}"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("SNOWFLAKE_DATABASE", "TESTDB")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        t = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="overlays-test",
            KeySchema=[{"AttributeName": "tenant_id", "KeyType": "HASH"}, {"AttributeName": "metric_id", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "tenant_id", "AttributeType": "S"}, {"AttributeName": "metric_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(overlay_store, "_table", t)
        lambda_handler._overlay_cache.clear()
        _as(monkeypatch, ADMIN)
        yield TestClient(lambda_handler.app)


def _as(monkeypatch, user):
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user if a else None)


def _put(client, expected_version=0, **fields):
    body = {"expr": "ROUND(GRADE_PERCENTAGE, 0)", "description": "rounded", "expected_version": expected_version} | fields
    return client.put(URL, headers=AUTH, json=body)


def test_a_valid_overlay_is_saved_and_listed(client):
    r = _put(client)
    assert r.status_code == 200 and r.json()["overlay"]["version"] == 1
    assert client.get(URL, headers=AUTH).json()["overlay"]["expr"] == "ROUND(GRADE_PERCENTAGE, 0)"
    assert [o["target"] for o in client.get("/api/v1/admin/overlays", headers=AUTH).json()["overlays"]] == [TARGET]


def test_an_invalid_overlay_is_a_400_with_reasons_and_nothing_is_saved(client):
    r = _put(client, expr="(SELECT MAX(EMAIL) FROM CDM_LMS.PERSON)")
    assert r.status_code == 400 and r.json()["detail"]["errors"]
    assert client.get(URL, headers=AUTH).json()["overlay"] is None


def test_a_stale_expected_version_is_a_409(client):
    _put(client)
    assert _put(client, expected_version=0, expr="FLOOR(GRADE_PERCENTAGE)").status_code == 409


def test_history_and_revert(client):
    _put(client)
    _put(client, expected_version=1, expr="FLOOR(GRADE_PERCENTAGE)")
    assert [h["version"] for h in client.get(f"{URL}/history", headers=AUTH).json()["history"]] == [2, 1]
    r = client.post(f"{URL}/revert", headers=AUTH, json={"version": 1, "expected_version": 2})
    assert r.status_code == 200 and r.json()["overlay"]["expr"] == "ROUND(GRADE_PERCENTAGE, 0)"
    assert client.post(f"{URL}/revert", headers=AUTH, json={"version": 9, "expected_version": 3}).status_code == 404


def test_compile_applies_only_the_callers_tenant_overlays_and_names_them_in_provenance(client, monkeypatch):
    _put(client)
    contract = {"metrics": ["metric.average_grade.v1"]}
    mine = client.post("/api/v1/semantic/compile", headers=AUTH, json=contract).json()
    assert "ROUND(GRADE_PERCENTAGE, 0)" in mine["sql"]
    assert mine["provenance"]["overlays"] == [f"{TARGET}@v1"]
    _as(monkeypatch, {"sub": "u2", "custom:tenant_id": "t2"})
    theirs = client.post("/api/v1/semantic/compile", headers=AUTH, json=contract).json()
    assert "ROUND(GRADE_PERCENTAGE, 0)" not in theirs["sql"] and theirs["provenance"]["overlays"] == []


def test_the_catalog_shows_tenant_only_filters_to_that_tenant(client):
    target = "filter:dataset.student_grade.v1:honours"
    r = client.put(f"/api/v1/admin/overlay/{target}", headers=AUTH,
                   json={"sql": "GRADE_PERCENTAGE >= 90", "description": "Honours", "expected_version": 0})
    assert r.status_code == 200, r.text
    ds = next(d for d in client.get("/api/v1/semantic/catalog", headers=AUTH).json()["datasets"] if d["id"] == "dataset.student_grade.v1")
    assert "honours" in [f["name"] for f in ds["filters"]]


def test_deleting_a_filter_that_a_metric_overlay_uses_is_refused(client):
    client.put("/api/v1/admin/overlay/filter:dataset.student_grade.v1:honours", headers=AUTH,
               json={"sql": "GRADE_PERCENTAGE >= 90", "expected_version": 0})
    r = client.put("/api/v1/admin/overlay/metric:metric.average_grade.v1", headers=AUTH,
                   json={"default_filters": ["honours"], "expected_version": 0})
    assert r.status_code == 200, r.text
    r = client.delete("/api/v1/admin/overlay/filter:dataset.student_grade.v1:honours?expected_version=1", headers=AUTH)
    assert r.status_code == 400
    assert client.delete("/api/v1/admin/overlay/metric:metric.average_grade.v1?expected_version=1", headers=AUTH).status_code == 200


def test_a_malformed_target_is_a_400(client):
    assert client.get("/api/v1/admin/overlay/widget:x", headers=AUTH).status_code == 400


def test_get_returns_the_canonical_definition_beside_the_overlay(client):
    body = client.get(URL, headers=AUTH).json()
    assert body["canonical"] == {"expr": "GRADE_PERCENTAGE"}
    assert client.get("/api/v1/admin/overlay/filter:dataset.student_grade.v1:honours", headers=AUTH).json()["canonical"] is None
    metric = client.get("/api/v1/admin/overlay/metric:metric.courses.v1", headers=AUTH).json()
    assert metric["canonical"] == {"default_filters": ["top_level", "live"]}


def _stale(table, target, **fields):
    table.put_item(Item={"tenant_id": "t1", "metric_id": target, "target": target, "version": 1, "description": "", **fields})


def test_an_overlay_that_no_longer_validates_is_skipped_reported_and_does_not_block_other_edits(client):
    table = overlay_store._get_table()
    _stale(table, TARGET, expr="REMOVED_COLUMN")
    lambda_handler._overlay_cache.clear()
    compiled = client.post("/api/v1/semantic/compile", headers=AUTH, json={"metrics": ["metric.average_grade.v1"]}).json()
    assert "REMOVED_COLUMN" not in compiled["sql"] and compiled["provenance"]["overlays"] == []
    listed = client.get("/api/v1/admin/overlays", headers=AUTH).json()["overlays"]
    assert listed[0]["status"] == "skipped" and listed[0]["problems"]
    other = "filter:dataset.student_grade.v1:honours"
    r = client.put(f"/api/v1/admin/overlay/{other}", headers=AUTH, json={"sql": "GRADE_PERCENTAGE >= 90", "expected_version": 0})
    assert r.status_code == 200, r.text


def test_deleting_an_unrelated_overlay_is_not_blocked_by_a_stale_metric_overlay(client):
    table = overlay_store._get_table()
    _stale(table, "metric:metric.average_grade.v1", default_filters=["gone"])
    _put(client)
    assert client.delete(f"{URL}?expected_version=1", headers=AUTH).status_code == 200


def test_delete_needs_the_current_version_and_a_missing_overlay_is_a_404(client):
    _put(client)
    assert client.delete(f"{URL}?expected_version=0", headers=AUTH).status_code == 409
    assert client.delete(f"{URL}?expected_version=1", headers=AUTH).status_code == 200
    assert client.delete(f"{URL}?expected_version=1", headers=AUTH).status_code == 404


def test_provenance_names_only_the_overlays_the_query_used(client):
    _put(client)
    client.put("/api/v1/admin/overlay/filter:dataset.student_grade.v1:honours", headers=AUTH,
               json={"sql": "GRADE_PERCENTAGE >= 90", "expected_version": 0})
    compiled = client.post("/api/v1/semantic/compile", headers=AUTH, json={"metrics": ["metric.average_grade.v1"]}).json()
    assert compiled["provenance"]["overlays"] == [f"{TARGET}@v1"]


def test_validation_errors_carry_no_terminal_colour_codes(client):
    r = _put(client, expr="GRADE_PERCENTAGE +* 2")
    assert r.status_code == 400 and not any("\x1b" in e for e in r.json()["detail"]["errors"])


def test_an_unreachable_overlay_store_falls_back_to_the_canonical_definitions(client, monkeypatch):
    def down(tid):
        raise RuntimeError("DynamoDB unavailable")

    monkeypatch.setattr(overlay_store, "list_overlays", down)
    lambda_handler._overlay_cache.clear()
    r = client.post("/api/v1/semantic/compile", headers=AUTH, json={"metrics": ["metric.average_grade.v1"]})
    assert r.status_code == 200 and r.json()["provenance"]["overlays"] == []
