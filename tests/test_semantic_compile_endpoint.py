import pytest
from fastapi.testclient import TestClient

import lambda_handler

AUTH = {"Authorization": "Bearer test"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_DATABASE", "TESTDB")
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: {"sub": "u1"})
    return TestClient(lambda_handler.app)


def test_compiles_a_metric(client):
    r = client.post("/api/v1/semantic/compile", headers=AUTH,
                    json={"metrics": ["metric.reportable_courses.v1"], "dimensions": ["term_name"]})
    assert r.status_code == 200
    body = r.json()
    assert "TESTDB.CDM_LMS.COURSE" in body["sql"]
    assert body["provenance"] == {
        "governed": True,
        "datasets": ["dataset.course_filters.v1"],
        "metrics": ["metric.reportable_courses.v1"],
        "measures": ["dataset.course_filters.v1:courses"],
        "overlays": [],
    }


def test_unknown_metric_is_a_400(client):
    r = client.post("/api/v1/semantic/compile", headers=AUTH, json={"metrics": ["metric.nope.v1"]})
    assert r.status_code == 400
    assert "unknown metric" in r.json()["detail"]


def test_malformed_contract_is_a_422(client):
    r = client.post("/api/v1/semantic/compile", headers=AUTH, json={"metrics": ["metric.x.v1"], "limit": 5000})
    assert r.status_code == 422


def test_requires_a_valid_token(client, monkeypatch):
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: None)
    r = client.post("/api/v1/semantic/compile", headers=AUTH, json={"metrics": ["metric.reportable_courses.v1"]})
    assert r.status_code == 401
