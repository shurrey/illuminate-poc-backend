import pytest

import lambda_handler
import overlay_store
import user_items_store as store
from semantic_layer.catalog import default_catalog
from semantic_layer.catalog_view import public_catalog
from semantic_layer.overlays import Overlay
from tests.test_directory import seed
from tests.test_me_cards import AUTH, NEW, as_user  # noqa: F401
from tests.test_me_sql_queries import executed  # noqa: F401
from tests.test_shared_with_me import ADA, ANN, OWNER, FILTER_ONLY, make, share
from tests.tenant_fakes import TENANT_A, TENANT_B, TENANT_CORE, user_of

ADMIN = "illuminate-admins"
METRIC = "metric.reportable_courses.v1"
MEASURE = "dataset.student_grade.v1:average_grade_percentage"
CARD_NOT_FOUND = {"detail": "Card not found"}


def _get(client, path, **params):
    return client.get(path, headers=AUTH, params=params)


# --- search ---------------------------------------------------------------------------------------------------

def test_search_returns_the_chat_tools_matches_under_results(as_user):
    from semantic_layer.search import search_catalog

    r = _get(as_user(), "/api/v1/semantic/search", q="reportable courses")
    assert r.status_code == 200
    assert r.json() == {"results": search_catalog("reportable courses", default_catalog())}
    assert r.json()["results"][0]["id"] == METRIC


@pytest.mark.parametrize("q", ["", "   ", None])
def test_search_needs_a_question(as_user, q):
    r = _get(as_user(), "/api/v1/semantic/search", **({} if q is None else {"q": q}))
    assert r.status_code == 400


def test_search_question_is_at_most_200_characters(as_user):
    c = as_user()
    assert _get(c, "/api/v1/semantic/search", q="a" * 200).status_code == 200
    assert _get(c, "/api/v1/semantic/search", q="a" * 201).status_code == 400


def test_search_limit_defaults_to_8_and_is_capped_at_25(as_user):
    c = as_user()
    q = "course student grade activity term enrollment"
    assert len(_get(c, "/api/v1/semantic/search", q=q).json()["results"]) == 8
    assert len(_get(c, "/api/v1/semantic/search", q=q, limit=3).json()["results"]) == 3
    assert len(_get(c, "/api/v1/semantic/search", q=q, limit=500).json()["results"]) == 25
    assert _get(c, "/api/v1/semantic/search", q=q, limit=0).status_code == 400


def test_search_never_offers_a_pii_dimension(as_user):
    r = _get(as_user(user_of(TENANT_A, ADMIN)), "/api/v1/semantic/search", q="email", limit=25)
    assert r.status_code == 200 and "person_email" not in r.text


def _tenant_marked_catalog(monkeypatch, mark):
    """Tenant A's catalog has the reportable-courses metric described with mark; every other tenant's is canonical."""
    real = lambda_handler._tenant_state
    base = default_catalog()
    metric = base.metrics[METRIC].model_copy(update={"description": f"{mark} reportable courses"})
    marked = base.model_copy(update={"metrics": {**base.metrics, METRIC: metric}})
    monkeypatch.setattr(lambda_handler, "_tenant_state",
                        lambda tid: (marked, [], {}) if tid == TENANT_A.tenant_id else real(tid))


def test_search_and_entity_use_the_callers_tenant_catalog(as_user, monkeypatch):
    _tenant_marked_catalog(monkeypatch, "ALPHA-ONLY")
    a = as_user(user_of(TENANT_A))
    assert "ALPHA-ONLY" in _get(a, "/api/v1/semantic/search", q="reportable courses").text
    assert "ALPHA-ONLY" in _get(a, f"/api/v1/semantic/entity/{METRIC}").text
    b = as_user(user_of(TENANT_B))
    for r in (_get(b, "/api/v1/semantic/search", q="reportable courses"), _get(b, f"/api/v1/semantic/entity/{METRIC}")):
        assert r.status_code == 200 and "ALPHA-ONLY" not in r.text


def test_an_overlay_in_one_tenant_is_not_seen_by_another(as_user, monkeypatch):
    overlays = {TENANT_A.tenant_id: [
        Overlay(target=f"measure:{MEASURE}", expr="ROUND(GRADE_PERCENTAGE, 0)", version=1),
        Overlay(target="filter:dataset.student_grade.v1:alpha_honours", sql="GRADE_PERCENTAGE >= 90",
                description="ALPHA-ONLY honours", version=1),
    ]}
    monkeypatch.setattr(overlay_store, "list_overlays", lambda tid: overlays.get(tid, []))
    lambda_handler._overlay_cache.clear()

    a = as_user(user_of(TENANT_A))
    assert _get(a, f"/api/v1/semantic/entity/{MEASURE}").json()["expr"] == "ROUND(GRADE_PERCENTAGE, 0)"
    assert "ALPHA-ONLY" in _get(a, "/api/v1/semantic/entity/dataset.student_grade.v1").text
    b = as_user(user_of(TENANT_B))
    measure = _get(b, f"/api/v1/semantic/entity/{MEASURE}")
    dataset = _get(b, "/api/v1/semantic/entity/dataset.student_grade.v1")
    assert measure.json()["expr"] != "ROUND(GRADE_PERCENTAGE, 0)"
    assert "ALPHA-ONLY" not in dataset.text and "alpha_honours" not in dataset.text


# --- entity ---------------------------------------------------------------------------------------------------

def _public():
    return public_catalog(default_catalog(), allow_identity=False)


def test_entity_describes_a_dataset_as_the_catalog_does(as_user):
    ds = _public()["datasets"][0]
    r = _get(as_user(), f"/api/v1/semantic/entity/{ds['id']}")
    assert r.status_code == 200 and r.json() == {"kind": "dataset", **ds}


def test_entity_describes_a_metric_with_its_dataset_and_dimensions(as_user):
    r = _get(as_user(), f"/api/v1/semantic/entity/{METRIC}")
    body = r.json()
    metric = next(m for m in _public()["metrics"] if m["id"] == METRIC)
    ds = next(d for d in _public()["datasets"] if d["id"] == metric["measure"].split(":")[0])
    assert r.status_code == 200 and body["kind"] == "metric"
    assert {k: body[k] for k in metric} == metric
    assert body["dataset"] == {"id": ds["id"], "display_name": ds["display_name"], "grain": ds["grain"]}
    assert body["dimensions"] == ds["dimensions"] and body["joins"] == ds["joins"]


def test_entity_describes_a_measure_and_a_dimension(as_user):
    c = as_user()
    measure = _get(c, f"/api/v1/semantic/entity/{MEASURE}").json()
    assert measure["kind"] == "measure" and measure["id"] == MEASURE and measure["agg"]
    assert measure["dataset"]["id"] == "dataset.student_grade.v1" and measure["dimensions"]
    ds = next(d for d in _public()["datasets"] if d["id"] == "dataset.student_grade.v1")
    dim = ds["dimensions"][0]
    got = _get(c, f"/api/v1/semantic/entity/{ds['id']}:{dim['name']}").json()
    assert got == {"kind": "dimension", "id": f"{ds['id']}:{dim['name']}", **dim,
                   "dataset": {"id": ds["id"], "display_name": ds["display_name"], "grain": ds["grain"]}}


def test_a_pii_dimension_is_not_selectable_for_a_viewer_and_is_for_an_admin(as_user):
    path = "/api/v1/semantic/entity/dataset.course_student_activity.v1:person_email"
    assert _get(as_user(), path).json()["selectable"] is False
    assert _get(as_user(user_of(TENANT_A, ADMIN)), path).json()["selectable"] is True


@pytest.mark.parametrize("entity_id", [
    "metric.no_such_metric.v1", "dataset.no_such.v1", "dataset.student_grade.v1:no_such", "DATASET.X.V1",
    "dataset.student_grade.v1:", "anything", "metric.reportable_courses.v1:x", "a" * 300,
])
def test_an_unknown_or_malformed_entity_is_a_404(as_user, entity_id):
    r = _get(as_user(), f"/api/v1/semantic/entity/{entity_id}")
    assert (r.status_code, r.json()) == (404, {"detail": "Entity not found"})


def test_an_internal_dataset_is_a_404(as_user, monkeypatch):
    base = default_catalog()
    ds_id = next(iter(base.datasets))
    internal = base.model_copy(update={"datasets": {
        **base.datasets, ds_id: base.datasets[ds_id].model_copy(update={"visibility": "internal"})}})
    monkeypatch.setattr(lambda_handler, "_tenant_state", lambda tid: (internal, [], {}))
    r = _get(as_user(), f"/api/v1/semantic/entity/{ds_id}")
    assert r.status_code == 404
    for m in internal.metrics.values():
        if m.dataset_id == ds_id:
            assert _get(as_user(), f"/api/v1/semantic/entity/{m.id}").status_code == 404


def test_a_core_tenant_sees_nothing_of_learn(as_user):
    c = as_user(user_of(TENANT_CORE, ADMIN))
    texts = [_get(c, "/api/v1/semantic/search", q="learn table course user", limit=25).text]
    view = public_catalog(default_catalog(), allow_identity=True)
    for entity_id in [d["id"] for d in view["datasets"]] + [m["id"] for m in view["metrics"]]:
        r = _get(c, f"/api/v1/semantic/entity/{entity_id}")
        assert r.status_code == 200
        texts.append(r.text)
    assert all("LEARN." not in t.upper() and '"LEARN"' not in t.upper() for t in texts)


@pytest.mark.parametrize("path", ["/api/v1/semantic/search?q=courses", f"/api/v1/semantic/entity/{METRIC}"])
def test_search_and_entity_need_a_token(as_user, monkeypatch, path):
    c = as_user()
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: None)
    assert c.get(path).status_code == 401


# --- card run -------------------------------------------------------------------------------------------------

@pytest.fixture
def ran(monkeypatch):
    """The contracts _run_contract was asked to run, with the caller's sub."""
    seen = []

    async def fake(contract, user, tenant):
        seen.append((contract, user["sub"], tenant.tenant_id))
        return {"columns": ["n"], "rows": [{"n": 1}], "sql": "SELECT 1"}

    monkeypatch.setattr(lambda_handler, "_run_contract", fake)
    return seen


def _run(client, card_id):
    return client.post(f"/api/v1/me/cards/{card_id}/run", headers=AUTH)


def test_a_built_in_card_runs_its_contract_as_the_caller(as_user, ran):
    builtin = lambda_handler._default_cards()[0]
    r = _run(as_user(), builtin["id"])
    assert r.status_code == 200 and r.json()["rows"] == [{"n": 1}]
    assert ran == [(builtin["contract"], "u1", TENANT_A.tenant_id)]


def test_a_hidden_built_in_card_still_runs(as_user, ran):
    c = as_user()
    assert c.delete("/api/v1/me/cards/average-grade", headers=AUTH).status_code == 200
    assert _run(c, "average-grade").status_code == 200 and len(ran) == 1


def test_an_own_card_runs_its_contract_as_the_caller(as_user, ran):
    c = as_user()
    cid = make(c, "cards", NEW)
    assert _run(c, cid).status_code == 200
    assert ran == [(NEW["contract"], "u1", TENANT_A.tenant_id)]


def test_an_own_card_runs_for_real_through_the_warehouse(as_user, executed):
    c = as_user()
    r = _run(c, make(c, "cards", NEW))
    assert r.status_code == 200 and r.json()["rows"] and len(executed) == 1


@pytest.mark.parametrize("card_id", ["c_000000000000", "c_bad", "no-such-builtin", "sh_0000000000000000", "x"])
def test_an_unknown_card_is_a_404(as_user, ran, card_id):
    r = _run(as_user(), card_id)
    assert (r.status_code, r.json()) == (404, CARD_NOT_FOUND) and ran == []


def test_someone_elses_card_is_the_same_404(as_user, ran):
    seed(as_user, OWNER, ANN)
    cid = make(as_user(OWNER), "cards", NEW)
    r = _run(as_user(ANN), cid)
    assert (r.status_code, r.json()) == (404, CARD_NOT_FOUND) and ran == []


def test_another_tenants_card_with_the_same_sub_is_the_same_404(as_user, ran):
    cid = make(as_user(user_of(TENANT_A)), "cards", NEW)
    r = _run(as_user(user_of(TENANT_B)), cid)
    assert (r.status_code, r.json()) == (404, CARD_NOT_FOUND) and ran == []


def _pin(client, sid):
    r = client.post("/api/v1/me/cards/pin-shared", headers=AUTH, json={"share_id": sid})
    assert r.status_code == 200, r.text


def test_a_pinned_shared_card_runs_through_the_shared_run(as_user, ran):
    seed(as_user, OWNER, ANN)
    sid = share(as_user(OWNER), "cards", make(as_user(OWNER), "cards", NEW), ["ann"])
    _pin(as_user(ANN), sid)
    r = _run(as_user(ANN), sid)
    assert r.status_code == 200 and ran == [(NEW["contract"], "ann", TENANT_A.tenant_id)]


def test_a_shared_card_that_is_not_pinned_is_a_404(as_user, ran):
    seed(as_user, OWNER, ANN)
    sid = share(as_user(OWNER), "cards", make(as_user(OWNER), "cards", NEW), ["ann"])
    r = _run(as_user(ANN), sid)
    assert (r.status_code, r.json()) == (404, CARD_NOT_FOUND) and ran == []


def test_a_pinned_card_that_was_unshared_is_a_404(as_user, ran):
    seed(as_user, OWNER, ANN)
    o = as_user(OWNER)
    cid = make(o, "cards", NEW)
    sid = share(o, "cards", cid, ["ann"])
    _pin(as_user(ANN), sid)
    assert as_user(OWNER).delete(f"/api/v1/me/cards/{cid}/share", headers=AUTH).status_code == 200
    r = _run(as_user(ANN), sid)
    assert (r.status_code, r.json()) == (404, CARD_NOT_FOUND) and ran == []
    assert sid in store.get(TENANT_A.tenant_id, "ann", "layout")["order"]


def test_a_viewers_pinned_card_run_is_masked_and_an_admins_is_not(as_user, executed):
    seed(as_user, OWNER, ANN, ADA)
    o = as_user(OWNER)
    sid = share(o, "cards", make(o, "cards", {**NEW, "contract": FILTER_ONLY}), ["ann", "ada"])
    for who in (ANN, ADA):
        _pin(as_user(who), sid)
    viewer = _run(as_user(ANN), sid)
    assert viewer.status_code == 200, viewer.text
    assert "jane@example.edu" not in viewer.text and "…" in viewer.json()["sql"]
    assert "jane@example.edu" in _run(as_user(ADA), sid).text


def test_card_run_needs_a_token(as_user, monkeypatch, ran):
    c = as_user()
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: None)
    assert c.post("/api/v1/me/cards/active-students/run").status_code == 401 and ran == []


# --- governed marking (MCP sessions) ------------------------------------------------------------------------------

from tests.test_mcp_audience import world  # noqa: E402,F401
from tests.test_mcp_sessions import CONTRACT, H, item, w  # noqa: E402,F401


def test_a_card_run_is_a_governed_call(w, monkeypatch):
    monkeypatch.setattr(lambda_handler, "_card_contract", lambda t, s, i: CONTRACT)
    assert w.client.post("/api/v1/me/cards/c_000000000000/run", headers=H()).status_code == 200
    assert item(w)["governed"] is True


def test_a_pinned_shared_card_run_is_a_governed_call(w, monkeypatch):
    monkeypatch.setattr(lambda_handler, "_is_pinned", lambda t, s, i: True)
    monkeypatch.setattr(lambda_handler, "_shared_item",
                        lambda t, s, i: ({"item_kind": "card"}, {"id": "c_1", "contract": CONTRACT}, "Owner"))
    assert w.client.post("/api/v1/me/cards/sh_0000000000000000/run", headers=H()).status_code == 200
    assert item(w)["governed"] is True


def test_a_failed_card_run_does_not_mark_the_session(w, monkeypatch):
    def missing(t, s, i):
        raise lambda_handler._card_not_found()

    monkeypatch.setattr(lambda_handler, "_card_contract", missing)
    assert w.client.post("/api/v1/me/cards/c_000000000000/run", headers=H()).status_code == 404
    assert item(w)["governed"] is False
