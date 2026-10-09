from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import lambda_handler
import snowflake_client
from semantic_layer.reports import load_reports

AUTH = {"Authorization": "Bearer test"}
REPORTS = load_reports(Path(__file__).parent / "fixtures" / "reports")
TERMS = [{"term_name": "Q4: 2026", "term_start": "2026-04-01", "term_end": "2026-06-30"}]


def user(*groups):
    return {"sub": "u1", "cognito:groups": list(groups)}


@pytest.fixture
def ran(monkeypatch):
    sqls = []

    def fake(sql, params=None):
        sqls.append(sql)
        return {"columns": ["STUDENTS"], "rows": [{"STUDENTS": 7}]}

    monkeypatch.setenv("SNOWFLAKE_DATABASE", "TESTDB")
    monkeypatch.setattr(snowflake_client, "query_sql", fake)
    monkeypatch.setattr(lambda_handler, "_reports", lambda: REPORTS)
    monkeypatch.setattr(lambda_handler, "_current_terms", lambda: TERMS)
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user() if a else None)
    return sqls


@pytest.fixture
def client(ran):
    return TestClient(lambda_handler.app)


def as_role(monkeypatch, *groups):
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user(*groups))


def run(client, visual, query="main", values=None):
    return client.post("/api/v1/reports/report.sample.v1/run", headers=AUTH,
                       json={"visual": visual, "query": query, "values": values or {}})


def test_report_routes_need_a_token(client):
    assert client.get("/api/v1/reports").status_code == 401
    assert client.get("/api/v1/reports/report.sample.v1").status_code == 401


def test_lists_reports(client):
    assert client.get("/api/v1/reports", headers=AUTH).json() == {"reports": [
        {"id": "report.sample.v1", "title": "Sample report", "area": "leading",
         "description": "A fixture exercising filters, a KPI, a line and a text visual."}]}


def test_a_report_comes_with_its_resolved_defaults(client):
    body = client.get("/api/v1/reports/report.sample.v1", headers=AUTH).json()
    assert body["report"]["pages"][0]["visuals"][0]["id"] == "students"
    assert body["defaults"]["term"] == ["Q4: 2026"]
    assert client.get("/api/v1/reports/report.nope.v1", headers=AUTH).status_code == 404


def test_run_merges_values_and_returns_rows_with_their_contract(client, ran):
    body = run(client, "students", "current", {"term": ["Q4: 2026"]}).json()
    assert body["rows"] == [{"students": 7}] and body["ignored_filters"] == []
    assert body["contract"]["filters"] == [{"dimension": "dataset.courses.v1:term_name", "op": "in", "values": ["Q4: 2026"]}]
    assert body["provenance"]["governed"] is True and "TERM_NAME IN ('Q4: 2026')" in ran[-1]


def test_run_reports_the_filters_it_had_to_ignore(client):
    assert run(client, "sessions_by_day", values={"term": ["Q4: 2026"]}).json()["ignored_filters"] == ["term"]


def test_unknown_visuals_queries_and_text_visuals_are_404(client):
    assert run(client, "nope").status_code == 404
    assert run(client, "students", "nope").status_code == 404
    assert run(client, "about").status_code == 404


def test_viewers_get_unavailable_for_an_identity_only_query(client, ran):
    assert run(client, "roster").json() == {"unavailable": "Requires Author access"}
    assert ran == []


def test_viewers_get_mixed_tables_without_their_identity_columns(client, ran):
    body = run(client, "roster_by_term").json()
    assert body["contract"]["dimensions"] == ["term_name"] and "order_by" not in body["contract"]
    assert "PERSON_EMAIL" not in ran[-1][ran[-1].rindex("\nSELECT"):]


def test_authors_get_identity(client, ran, monkeypatch):
    as_role(monkeypatch, "illuminate-authors")
    assert run(client, "roster").json()["contract"]["dimensions"] == ["person_email"]
    assert "PERSON_EMAIL" in ran[-1][ran[-1].rindex("\nSELECT"):]


def test_a_malformed_value_is_a_400(client):
    resp = run(client, "students", "current", {"dates": ["2026-01-01"]})
    assert resp.status_code == 400 and "dates" in resp.json()["detail"]
