import pytest
from fastapi.testclient import TestClient

import lambda_handler
import snowflake_client
from tests.tenant_fakes import TENANT_A, install_tenants, user_of

AUTH = {"Authorization": "Bearer test"}
MEMBER = user_of(TENANT_A)
ADMIN = user_of(TENANT_A, "illuminate-admins", sub="a1")


@pytest.fixture
def executed(monkeypatch):
    calls = []
    monkeypatch.setattr(snowflake_client, "query_sql",
                        lambda sql, params=None, **k: calls.append(sql) or {"columns": ["N"], "rows": [{"N": 1}]})
    monkeypatch.setattr(snowflake_client, "query_preview",
                        lambda schema, table, limit=20, **k: calls.append(f"{schema}.{table}") or {"columns": [], "rows": []})
    return calls


def _client(monkeypatch, user):
    install_tenants(monkeypatch, TENANT_A)
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user if a else None)
    return TestClient(lambda_handler.app)


def test_table_preview_is_admin_only(monkeypatch, executed):
    r = _client(monkeypatch, MEMBER).get("/api/v1/dictionary/preview?schema=CDM_LMS&table=PERSON", headers=AUTH)
    assert r.status_code == 403
    assert executed == []


def test_admins_can_preview_tables(monkeypatch, executed):
    r = _client(monkeypatch, ADMIN).get("/api/v1/dictionary/preview?schema=CDM_LMS&table=PERSON", headers=AUTH)
    assert r.status_code == 200
    assert executed == ["CDM_LMS.PERSON"]
