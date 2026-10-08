import pytest
from fastapi.testclient import TestClient

import lambda_handler
import snowflake_client

AUTH = {"Authorization": "Bearer test"}
CONTRACT = {"metrics": ["metric.reportable_courses.v1"], "dimensions": ["term_name"]}


@pytest.fixture
def executed(monkeypatch):
    calls = []

    def fake_query_sql(sql, params=None):
        calls.append(sql)
        return {"columns": ["TERM_NAME", "REPORTABLE_COURSES"], "rows": [{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3}]}

    monkeypatch.setattr(snowflake_client, "query_sql", fake_query_sql)
    return calls


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_DATABASE", "TESTDB")
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: {"sub": "u1"})
    return TestClient(lambda_handler.app)


def test_runs_the_compiled_sql_through_the_guard(client, executed):
    r = client.post("/api/v1/semantic/query", headers=AUTH, json=CONTRACT)
    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == [{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3}]
    assert body["columns"] == ["TERM_NAME", "REPORTABLE_COURSES"]
    assert executed == [body["sql"]]
    assert body["provenance"]["metrics"] == ["metric.reportable_courses.v1"]


def test_compile_errors_are_400_and_nothing_runs(client, executed):
    r = client.post("/api/v1/semantic/query", headers=AUTH, json={"metrics": ["metric.nope.v1"]})
    assert r.status_code == 400
    assert executed == []


def test_execution_errors_are_502_with_the_sql(client, monkeypatch):
    def boom(sql, params=None):
        raise RuntimeError("warehouse suspended")

    monkeypatch.setattr(snowflake_client, "query_sql", boom)
    r = client.post("/api/v1/semantic/query", headers=AUTH, json=CONTRACT)
    assert r.status_code == 502
    assert "warehouse suspended" in r.json()["detail"]["error"]
    assert "DS_COURSE_FILTERS_V1" in r.json()["detail"]["sql"]


def test_requires_a_valid_token(client, executed, monkeypatch):
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: None)
    r = client.post("/api/v1/semantic/query", headers=AUTH, json=CONTRACT)
    assert r.status_code == 401
    assert executed == []
