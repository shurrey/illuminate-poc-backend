import pytest
from fastapi.testclient import TestClient

import lambda_handler
import snowflake_client

AUTH = {"Authorization": "Bearer test"}
MEMBER = {"sub": "u1", "custom:tenant_id": "t1"}
ADMIN = {"sub": "a1", "custom:tenant_id": "t1", "cognito:groups": ["illuminate-admins"]}


@pytest.fixture
def executed(monkeypatch):
    calls = []
    monkeypatch.setattr(snowflake_client, "query_sql",
                        lambda sql, params=None: calls.append(sql) or {"columns": ["N"], "rows": [{"N": 1}]})
    monkeypatch.setattr(snowflake_client, "query_preview",
                        lambda schema, table, limit=20: calls.append(f"{schema}.{table}") or {"columns": [], "rows": []})
    return calls


def _client(monkeypatch, user):
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user if a else None)
    return TestClient(lambda_handler.app)


def test_dashboard_query_blocks_raw_pii(monkeypatch, executed):
    r = _client(monkeypatch, MEMBER).post("/api/v1/dashboard/query", headers=AUTH,
                                          json={"sql": "SELECT EMAIL FROM CDM_LMS.PERSON"})
    assert "personally identifiable" in r.json()["error"]
    assert executed == []


def test_dashboard_query_blocks_non_cdm_schemas(monkeypatch, executed):
    r = _client(monkeypatch, MEMBER).post("/api/v1/dashboard/query", headers=AUTH,
                                          json={"sql": "SELECT * FROM OTHER.SECRETS"})
    assert "not in the allowed list" in r.json()["error"]
    assert executed == []


def test_dashboard_query_still_runs_aggregate_sql(monkeypatch, executed):
    r = _client(monkeypatch, MEMBER).post("/api/v1/dashboard/query", headers=AUTH,
                                          json={"sql": "SELECT COUNT(DISTINCT ID) AS N FROM CDM_LMS.PERSON"})
    assert r.json() == {"columns": ["N"], "rows": [{"N": 1}]}
    assert len(executed) == 1


def test_table_preview_is_admin_only(monkeypatch, executed):
    r = _client(monkeypatch, MEMBER).get("/api/v1/dictionary/preview?schema=CDM_LMS&table=PERSON", headers=AUTH)
    assert r.status_code == 403
    assert executed == []


def test_admins_can_preview_tables(monkeypatch, executed):
    r = _client(monkeypatch, ADMIN).get("/api/v1/dictionary/preview?schema=CDM_LMS&table=PERSON", headers=AUTH)
    assert r.status_code == 200
    assert executed == ["CDM_LMS.PERSON"]
