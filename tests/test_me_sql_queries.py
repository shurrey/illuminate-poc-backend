import pytest

import lambda_handler
import overlay_store
import snowflake_client
import user_items_store
from licenses import LEARN_REFUSAL
from semantic_layer.overlays import Overlay
from tests.test_me_cards import AUTH, as_user  # noqa: F401  (fixture)
from tests.tenant_fakes import TENANT_A, TENANT_CORE, user_of

PART = {"metrics": ["metric.reportable_courses.v1"], "dimensions": ["term_name"]}
OUTER = "SELECT term_name, reportable_courses FROM governed_1"
FREEHAND = "SELECT COUNT(*) AS n FROM CDM_LMS.COURSE"
EMAIL = "SELECT EMAIL FROM CDM_LMS.PERSON"
SQLQ = {"name": "My SQL", "description": "", "query_kind": "sql", "sql": FREEHAND, "governed_parts": {}}
WITH_PARTS = {**SQLQ, "description": "Pivot of a governed query", "sql": OUTER, "governed_parts": {"governed_1": PART}}


@pytest.fixture
def executed(monkeypatch):
    calls = []

    def fake_query_sql(sql, params=None, **k):
        calls.append(sql)
        return {"columns": ["TERM_NAME", "REPORTABLE_COURSES"], "rows": [{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3}]}

    monkeypatch.setattr(snowflake_client, "query_sql", fake_query_sql)
    return calls


def admin(**claims):
    return {**user_of(TENANT_A, "illuminate-admins"), **claims}


def save(c, body=SQLQ):
    return c.post("/api/v1/me/queries", headers=AUTH, json=body)


def run(c, query_id):
    return c.post(f"/api/v1/me/queries/{query_id}/run", headers=AUTH)


def test_save_and_run_sql_without_parts(as_user, executed):
    c = as_user(admin(name="Pat Lee"))
    made = save(c)
    assert made.status_code == 200
    body = made.json()
    assert body["query_kind"] == "sql" and body["sql"] == FREEHAND and body["owner_name"] == "Pat Lee"
    assert body["governance_hint"] == "ungoverned" and body["lastUsedAt"] is None

    r = run(c, body["id"])
    assert r.status_code == 200 and executed == [FREEHAND]
    out = r.json()
    assert out["rows"] == [{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3}] and out["sql"] == FREEHAND
    assert out["provenance"]["governance"] == "none" and out["provenance"]["reason"] == "Written by Pat Lee"
    listed = c.get("/api/v1/me/queries", headers=AUTH).json()["queries"][0]
    assert listed["lastUsedAt"] and listed["governance_hint"] == "ungoverned"


def test_owner_name_falls_back_to_the_email_claim(as_user):
    made = save(as_user(admin(email="pat@example.edu"))).json()
    assert made["owner_name"] == "pat@example.edu"


def test_save_and_run_sql_with_parts(as_user, executed):
    c = as_user()
    made = save(c, WITH_PARTS).json()
    assert made["governance_hint"] == "partial" and made["governed_parts"] == {"governed_1": PART}
    r = run(c, made["id"])
    assert r.status_code == 200
    out = r.json()
    assert out["executed_sql"] == executed[0] != OUTER
    assert out["provenance"]["governance"] == "full" and out["provenance"]["reason"] == "Pivot of a governed query"


def test_governed_query_runs_like_semantic_query(as_user, executed):
    c = as_user()
    made = c.post("/api/v1/me/queries", headers=AUTH, json={
        "name": "g", "prompt": "p", "description": "d", "contract": PART}).json()
    assert made["governance_hint"] == "governed"
    r = run(c, made["id"])
    assert r.status_code == 200
    out = r.json()
    assert out["rows"] and out["sql"] == executed[0] and out["provenance"]["metrics"] == PART["metrics"]
    assert c.get("/api/v1/me/queries", headers=AUTH).json()["queries"][0]["lastUsedAt"]


def test_an_admin_runs_email_sql_and_a_viewer_is_refused_at_save_and_run(as_user, executed):
    body = {**SQLQ, "sql": EMAIL}
    made = save(as_user(admin()), body)
    assert made.status_code == 200
    assert run(as_user(admin()), made.json()["id"]).status_code == 200

    viewer = as_user(user_of(TENANT_A, sub="u2"))
    refused = save(viewer, body)
    assert refused.status_code == 400 and refused.json()["detail"]["errors"]
    executed.clear()
    stored = user_items_store.get(TENANT_A.tenant_id, "u1", "query", made.json()["id"])
    user_items_store.put(TENANT_A.tenant_id, "u2", "query", "q_000000000009",
                         {**{k: v for k, v in stored.items() if k not in ("id", "version", "updated_at")},
                          "lastUsedAt": None})
    r = run(viewer, "q_000000000009")
    assert r.status_code == 400 and executed == []
    assert user_items_store.list_kind(TENANT_A.tenant_id, "u2", "query")[0]["lastUsedAt"] is None


def test_core_learn_is_refused_at_save(as_user):
    c = as_user(user_of(TENANT_CORE))
    r = save(c, {**SQLQ, "sql": "SELECT COUNT(*) AS n FROM LEARN.COURSE_MAIN"})
    assert r.status_code == 400 and r.json()["detail"]["errors"] == [LEARN_REFUSAL]
    assert user_items_store.list_kind(TENANT_CORE.tenant_id, "u1", "query") == []


def test_a_stored_learn_query_is_refused_on_run_for_a_core_user(as_user, executed):
    c = as_user(user_of(TENANT_CORE))
    user_items_store.put(TENANT_CORE.tenant_id, "u1", "query", "q_000000000001", {
        **SQLQ, "sql": "SELECT COUNT(*) AS n FROM LEARN.COURSE_MAIN", "createdAt": "2026-01-01T00:00:00Z",
        "lastUsedAt": None})
    r = run(c, "q_000000000001")
    assert r.status_code == 400 and r.json()["detail"] == LEARN_REFUSAL
    assert executed == []


def test_an_overlay_change_is_reflected_in_the_next_run(as_user, executed, monkeypatch):
    c = as_user()
    made = save(c, WITH_PARTS).json()
    before = run(c, made["id"]).json()
    assert before["provenance"]["overlays"] == [] and "COURSE_ENDED = 0" not in before["executed_sql"]
    overlay = Overlay(target="metric:metric.reportable_courses.v1", default_filters=["ongoing"], version=1)
    monkeypatch.setattr(overlay_store, "list_overlays", lambda tenant_id: [overlay])
    lambda_handler._overlay_cache.clear()
    after = run(c, made["id"]).json()
    assert len(after["provenance"]["overlays"]) == 1
    assert "COURSE_ENDED = 0" in after["executed_sql"] and executed[-1] == after["executed_sql"] != executed[0]


def test_a_tampered_stored_part_is_still_guarded(as_user, executed):
    c = as_user()
    made = save(c, WITH_PARTS).json()
    stored = user_items_store.get(TENANT_A.tenant_id, "u1", "query", made["id"])
    user_items_store.put(TENANT_A.tenant_id, "u1", "query", made["id"], {
        **{k: v for k, v in stored.items() if k not in ("id", "version", "updated_at")},
        "governed_parts": {"governed_1": {"limit": 99999}}}, expected_version=stored["version"])
    r = run(c, made["id"])
    assert r.status_code == 400 and "governed_1" in r.json()["detail"] and executed == []


def test_stored_sql_is_never_run_as_compiled(as_user, executed):
    c = as_user()
    made = save(c).json()
    stored = user_items_store.get(TENANT_A.tenant_id, "u1", "query", made["id"])
    user_items_store.put(TENANT_A.tenant_id, "u1", "query", made["id"], {
        **{k: v for k, v in stored.items() if k not in ("id", "version", "updated_at")},
        "sql": "DROP TABLE CDM_LMS.COURSE"}, expected_version=stored["version"])
    assert run(c, made["id"]).status_code == 400 and executed == []


def test_sql_length_limit(as_user):
    c = as_user()
    assert save(c, {**SQLQ, "sql": "x" * 20_001}).status_code == 400
    ok = save(c, {**SQLQ, "sql": "SELECT 1 AS n -- " + "x" * 100})
    assert ok.status_code == 200
    r = c.put(f"/api/v1/me/queries/{ok.json()['id']}", headers=AUTH, json={**SQLQ, "sql": "x" * 20_001})
    assert r.status_code == 400


@pytest.mark.parametrize("patch,fragment", [
    ({"sql": ""}, "sql is required"),
    ({"sql": 5}, "sql is required"),
    ({"name": " "}, "name is required"),
    ({"governed_parts": []}, "governed_parts must be an object"),
    ({"governed_parts": {"governed_1": "x"}, "sql": OUTER}, "governed_1"),
    ({"query_kind": "other"}, "query_kind must be"),
])
def test_invalid_sql_bodies_are_400(as_user, patch, fragment):
    r = save(as_user(), {**SQLQ, **patch})
    assert r.status_code == 400 and any(fragment in e for e in r.json()["detail"]["errors"])


def test_a_refused_statement_is_400_with_the_guard_message(as_user):
    r = save(as_user(), {**SQLQ, "sql": "DROP TABLE CDM_LMS.COURSE"})
    assert r.status_code == 400 and r.json()["detail"]["errors"]


def test_update_revalidates_as_the_saver_and_keeps_dates(as_user):
    c = as_user()
    made = save(c).json()
    bad = c.put(f"/api/v1/me/queries/{made['id']}", headers=AUTH, json={**SQLQ, "sql": EMAIL})
    assert bad.status_code == 400
    r = c.put(f"/api/v1/me/queries/{made['id']}", headers=AUTH, json={**WITH_PARTS, "name": "renamed"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "renamed" and body["createdAt"] == made["createdAt"] and body["governance_hint"] == "partial"


def test_run_of_an_unknown_query_is_404(as_user):
    c = as_user()
    assert run(c, "q_nope").status_code == 404
    assert run(c, "q_000000000000").status_code == 404


def test_a_blank_description_run_reason_is_the_owner_default_and_long_ones_are_capped(as_user, executed):
    c = as_user()
    made = save(c, {**SQLQ, "description": "d" * 600}).json()
    assert run(c, made["id"]).json()["provenance"]["reason"] == "d" * 500
    no_name = save(c).json()
    assert run(c, no_name["id"]).json()["provenance"]["reason"] == "Written in the query builder"


def test_import_accepts_sql_queries_validated_as_the_importing_user(as_user, executed):
    c = as_user()
    r = c.post("/api/v1/me/import", headers=AUTH, json={"queries": [
        {**SQLQ, "id": "local-sql"}, {**SQLQ, "id": "local-pii", "name": "email", "sql": EMAIL}]})
    body = r.json()
    assert body["queries"] == 1 and len(body["skipped"]) == 1
    assert body["skipped"][0]["name"] == "email" and body["skipped"][0]["reason"]
    stored = user_items_store.list_kind(TENANT_A.tenant_id, "u1", "query")
    assert [q["query_kind"] for q in stored] == ["sql"] and stored[0]["sql"] == FREEHAND


def _tamper(tenant_id, query_id, **fields):
    stored = user_items_store.get(tenant_id, "u1", "query", query_id)
    body = {k: v for k, v in stored.items() if k not in ("id", "version", "updated_at")}
    user_items_store.put(tenant_id, "u1", "query", query_id, {**body, **fields}, expected_version=stored["version"])


@pytest.mark.parametrize("field", ["description", "sql", "owner_name", "query_kind", "name", "governed_parts"])
@pytest.mark.parametrize("value", [5, ["x"], {"a": 1}])
def test_a_tampered_field_type_is_never_a_500(as_user, executed, field, value):
    c = as_user(admin(name="Pat"))
    made = save(c, WITH_PARTS).json()
    _tamper(TENANT_A.tenant_id, made["id"], **{field: value})
    assert c.get("/api/v1/me/queries", headers=AUTH).status_code == 200
    assert run(c, made["id"]).status_code in (200, 400)


def test_a_null_description_is_blank(as_user, executed):
    c = as_user(admin(name="Pat"))
    made = save(c, {**SQLQ, "description": None})
    assert made.status_code == 200
    assert run(c, made.json()["id"]).json()["provenance"]["reason"] == "Written by Pat"


JUNK_PART = {**PART, "sql": "DROP TABLE x", "junk": 1}


def test_unknown_keys_in_governed_parts_are_dropped_on_save_and_update(as_user, executed):
    c = as_user()
    made = save(c, {**WITH_PARTS, "governed_parts": {"governed_1": JUNK_PART}})
    assert made.status_code == 200 and made.json()["governed_parts"] == {"governed_1": PART}
    r = c.put(f"/api/v1/me/queries/{made.json()['id']}", headers=AUTH,
              json={**WITH_PARTS, "governed_parts": {"governed_1": JUNK_PART}})
    assert r.json()["governed_parts"] == {"governed_1": PART}
    stored = user_items_store.list_kind(TENANT_A.tenant_id, "u1", "query")[0]
    assert stored["governed_parts"] == {"governed_1": PART}


def test_unknown_keys_in_governed_parts_are_dropped_on_import(as_user, executed):
    c = as_user()
    r = c.post("/api/v1/me/import", headers=AUTH, json={"queries": [
        {**WITH_PARTS, "id": "l1", "governed_parts": {"governed_1": JUNK_PART}}]})
    assert r.json()["queries"] == 1
    assert user_items_store.list_kind(TENANT_A.tenant_id, "u1", "query")[0]["governed_parts"] == {"governed_1": PART}


def test_a_failed_last_used_write_still_returns_the_result(as_user, executed, monkeypatch):
    from fastapi import HTTPException

    c = as_user()
    made = save(c).json()

    def conflict(*a, **k):
        raise HTTPException(status_code=409, detail="changed")

    monkeypatch.setattr(lambda_handler, "_replace_query", conflict)
    assert run(c, made["id"]).status_code == 200


def test_a_saved_sql_querys_hint_is_its_validated_preview_and_never_governed(as_user):
    c = as_user()
    counted = save(c, {**WITH_PARTS, "sql": "SELECT COUNT(*) AS n FROM governed_1"}).json()
    passthrough = save(c, WITH_PARTS).json()
    assert counted["governance_hint"] == "ungoverned" and passthrough["governance_hint"] == "partial"
    listed = {q["id"]: q["governance_hint"] for q in c.get("/api/v1/me/queries", headers=AUTH).json()["queries"]}
    assert listed == {counted["id"]: "ungoverned", passthrough["id"]: "partial"}


@pytest.mark.parametrize("stored, hint", [
    ({}, "partial"),
    ({"governance_hint": "governed"}, "partial"),
    ({"governance_hint": "full"}, "partial"),
    ({"governance_hint": "ungoverned"}, "ungoverned"),
])
def test_an_sql_rows_listed_hint_is_clamped_to_partial(as_user, stored, hint):
    c = as_user()
    user_items_store.put(TENANT_A.tenant_id, "u1", "query", "q_000000000001",
                         {**WITH_PARTS, "createdAt": "2026-01-01T00:00:00Z", "lastUsedAt": None, **stored})
    [listed] = c.get("/api/v1/me/queries", headers=AUTH).json()["queries"]
    assert listed["governance_hint"] == hint
