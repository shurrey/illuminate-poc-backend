import pytest
from fastapi.testclient import TestClient

import lambda_handler
import snowflake_client
from licenses import LEARN_REFUSAL
from tests.tenant_fakes import TENANT_A, TENANT_CORE, install_tenants, user_of

AUTH = {"Authorization": "Bearer test"}
PART = {"metrics": ["metric.reportable_courses.v1"], "dimensions": ["term_name"]}
OUTER = "SELECT term_name, reportable_courses FROM governed_1"
FREEHAND = "SELECT COUNT(*) AS n FROM CDM_LMS.COURSE"


@pytest.fixture
def executed(monkeypatch):
    calls = []

    def fake_query_sql(sql, params=None, **k):
        calls.append(sql)
        return {"columns": ["TERM_NAME", "REPORTABLE_COURSES"], "rows": [{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3}]}

    monkeypatch.setattr(snowflake_client, "query_sql", fake_query_sql)
    return calls


@pytest.fixture
def as_user(monkeypatch):
    def install(tenant=TENANT_A, *groups):
        install_tenants(monkeypatch, TENANT_A, TENANT_CORE, database="TESTDB")
        monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user_of(tenant, *groups))
        return TestClient(lambda_handler.app)

    return install


def _run(client, **body):
    return client.post("/api/v1/sql/run", headers=AUTH, json=body)


def test_freehand_run_is_labelled_ungoverned_with_the_default_reason(as_user, executed):
    r = _run(as_user(), sql=FREEHAND)
    assert r.status_code == 200
    body = r.json()
    assert body["sql"] == FREEHAND and executed == [FREEHAND]
    assert body["rows"] == [{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3}]
    assert body["truncated"] is False and "executed_sql" not in body
    assert body["provenance"]["governance"] == "none"
    assert body["provenance"]["reason"] == "Written in the query builder"


def test_run_with_parts_splices_labels_and_reports_the_executed_sql(as_user, executed):
    r = _run(as_user(), sql=OUTER, governed={"governed_1": PART}, reason="Pivot of a governed query")
    assert r.status_code == 200
    body = r.json()
    assert body["sql"] == OUTER and body["executed_sql"] == executed[0] != OUTER
    assert body["governed_parts"]["governed_1"]["metrics"] == PART["metrics"]
    prov = body["provenance"]
    assert prov["governance"] == "full" and prov["governed"] is True
    assert prov["metrics"] == PART["metrics"] and prov["reason"] == "Pivot of a governed query"
    assert prov["columns"]["TERM_NAME"]["governed"] is True


def test_a_cut_result_is_never_labelled_fully_governed(as_user, monkeypatch):
    monkeypatch.setattr(snowflake_client, "execute", lambda v, ctx: snowflake_client.Result(
        columns=["term_name", "reportable_courses"], rows=[{"term_name": "Fall", "reportable_courses": 3}], truncated=True))
    body = _run(as_user(), sql=OUTER, governed={"governed_1": PART}).json()
    assert body["truncated"] is True
    assert body["provenance"]["governance"] == "partial" and body["provenance"]["governed"] is False


def test_a_viewer_is_refused_pii_and_nothing_runs(as_user, executed):
    r = _run(as_user(), sql="SELECT EMAIL FROM CDM_LMS.PERSON")
    assert r.status_code == 400 and r.json()["detail"]
    assert executed == []


def test_an_author_may_select_pii(as_user, executed):
    r = _run(as_user(TENANT_A, "illuminate-authors"), sql="SELECT EMAIL FROM CDM_LMS.PERSON")
    assert r.status_code == 200 and len(executed) == 1


def test_core_tenants_cannot_reach_learn(as_user, executed):
    r = _run(as_user(TENANT_CORE), sql="SELECT COUNT(*) AS n FROM LEARN.COURSE_MAIN")
    assert r.status_code == 400 and r.json() == {"detail": LEARN_REFUSAL}
    assert executed == []


def test_invalid_parts_are_a_400_naming_the_part(as_user, executed):
    r = _run(as_user(), sql=OUTER, governed={"governed_1": {"limit": 99999}})
    assert r.status_code == 400 and "governed_1" in r.json()["detail"]
    assert executed == []


def test_a_warehouse_error_is_a_502_without_the_warehouse_message(as_user, monkeypatch):
    def boom(sql, params=None, **k):
        raise RuntimeError("Object 'SECRET_DB' does not exist")

    monkeypatch.setattr(snowflake_client, "query_sql", boom)
    r = _run(as_user(), sql=FREEHAND)
    assert r.status_code == 502
    assert r.json() == {"detail": "The warehouse could not run this query."}


@pytest.mark.parametrize("body", [
    {"sql": "x" * 20_001},
    {"sql": FREEHAND, "reason": "r" * 501},
    {"sql": FREEHAND, "governed": {"governed_1": "not an object"}},
    {},
])
def test_bodies_are_validated(as_user, executed, body):
    assert _run(as_user(), **body).status_code == 422
    assert executed == []


def test_requires_a_valid_token(as_user, executed, monkeypatch):
    client = as_user()
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: None)
    assert _run(client, sql=FREEHAND).status_code == 401
    assert client.post("/api/v1/sql/validate", headers=AUTH, json={"sql": FREEHAND}).status_code == 401
    assert client.get("/api/v1/schema/describe?schema=CDM_LMS&table=COURSE", headers=AUTH).status_code == 401
    assert executed == []


def test_validate_accepts_and_refuses_without_a_warehouse_call(as_user, executed, monkeypatch):
    monkeypatch.setattr(snowflake_client, "execute", lambda *a: pytest.fail("reached the warehouse"))
    client = as_user()
    ok = client.post("/api/v1/sql/validate", headers=AUTH, json={"sql": FREEHAND})
    assert ok.status_code == 200 and ok.json() == {"ok": True, "governance_preview": "none"}
    spliced = client.post("/api/v1/sql/validate", headers=AUTH, json={"sql": OUTER, "governed": {"governed_1": PART}})
    assert spliced.json() == {"ok": True, "governance_preview": "full"}
    bad = client.post("/api/v1/sql/validate", headers=AUTH, json={"sql": "SELECT EMAIL FROM CDM_LMS.PERSON"})
    assert bad.status_code == 200 and bad.json()["ok"] is False and bad.json()["error"]
    bad_part = client.post("/api/v1/sql/validate", headers=AUTH,
                           json={"sql": OUTER, "governed": {"governed_1": {"limit": 0}}})
    assert bad_part.json()["ok"] is False and "governed_1" in bad_part.json()["error"]
    assert executed == []


COLUMNS = [{"name": "ID", "type": "NUMBER", "pii": False, "description": ""}]


def _describe(client, schema, table):
    return client.get("/api/v1/schema/describe", headers=AUTH, params={"schema": schema, "table": table})


def test_describe_returns_the_columns(as_user, monkeypatch):
    monkeypatch.setattr("semantic_layer.sql_service.describe_table", lambda s, t: COLUMNS)
    monkeypatch.setattr("semantic_layer.sql_service.describe.__defaults__", (lambda s, t: COLUMNS,))
    r = _describe(as_user(), "cdm_lms", "course")
    assert r.status_code == 200 and r.json() == {"table": "CDM_LMS.COURSE", "columns": COLUMNS}


def test_describe_is_404_for_an_unknown_table(as_user, monkeypatch):
    monkeypatch.setattr("semantic_layer.sql_service.describe.__defaults__", (lambda s, t: None,))
    r = _describe(as_user(), "CDM_LMS", "NOPE")
    assert r.status_code == 404 and "CDM_LMS.NOPE" in r.json()["detail"]


def test_describe_learn_is_premium_only(as_user):
    core = _describe(as_user(TENANT_CORE), "LEARN", "COURSE_MAIN")
    assert core.status_code == 403 and core.json() == {"detail": LEARN_REFUSAL}
    premium = _describe(as_user(), "LEARN", "COURSE_MAIN")
    assert premium.status_code == 200 and premium.json()["table"] == "LEARN.COURSE_MAIN"


def test_describe_rejects_unsafe_identifiers(as_user):
    assert _describe(as_user(), "CDM_LMS;DROP", "COURSE").status_code == 400


@pytest.mark.parametrize("reason", ["", "   "])
def test_a_blank_reason_falls_back_to_the_default(as_user, executed, reason):
    body = _run(as_user(), sql=FREEHAND, reason=reason).json()
    assert body["provenance"]["reason"] == "Written in the query builder"


@pytest.mark.parametrize("sql, preview", [
    ("SELECT COUNT(*) AS n FROM governed_1", "none"),
    ("SELECT term_name, 1 AS x FROM governed_1", "partial"),
    ("SELECT term_name, reportable_courses FROM governed_1 WHERE reportable_courses > 1", "full"),
    ("SELECT g.term_name, g.reportable_courses FROM governed_1 g JOIN CDM_LMS.COURSE c ON c.ID = 1", "partial"),
])
def test_validate_previews_the_label_a_run_would_get(as_user, executed, sql, preview):
    r = as_user().post("/api/v1/sql/validate", headers=AUTH, json={"sql": sql, "governed": {"governed_1": PART}})
    assert r.json() == {"ok": True, "governance_preview": preview}
    assert executed == []


def test_a_too_deeply_nested_spliced_statement_is_a_refusal_not_a_500(as_user, executed):
    sql = "SELECT term_name FROM " + "(SELECT term_name FROM " * 150 + "governed_1" + ") AS s" * 150
    c = as_user()
    body = {"sql": sql, "governed": {"governed_1": PART}}
    assert c.post("/api/v1/sql/validate", headers=AUTH, json=body).json() == {
        "ok": False, "error": "The query is too complex to check."}
    r = c.post("/api/v1/sql/run", headers=AUTH, json=body)
    assert r.status_code == 400 and r.json()["detail"] == "The query is too complex to check."
    assert executed == []
