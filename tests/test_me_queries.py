import pytest

import lambda_handler
import user_items_store
from tests.test_me_cards import AUTH, as_user  # noqa: F401  (fixture)

Q = {"name": "My query", "prompt": "p", "description": "d", "contract": {"metrics": ["metric.reportable_courses.v1"]}}


def test_create_returns_server_fields(as_user):
    r = as_user().post("/api/v1/me/queries", headers=AUTH, json=Q)
    assert r.status_code == 200
    body = r.json()
    assert body["id"].startswith("q_") and len(body["id"]) == 14
    assert body["query_kind"] == "governed" and "kind" not in body and body["lastUsedAt"] is None and body["createdAt"]
    assert body["contract"] == Q["contract"] and "version" not in body


def test_list_is_newest_first(as_user):
    c = as_user()
    first = c.post("/api/v1/me/queries", headers=AUTH, json={**Q, "name": "one"}).json()
    second = c.post("/api/v1/me/queries", headers=AUTH, json={**Q, "name": "two"}).json()
    got = c.get("/api/v1/me/queries", headers=AUTH).json()["queries"]
    assert [q["id"] for q in got] == [second["id"], first["id"]]


@pytest.mark.parametrize("patch,fragment", [
    ({"name": ""}, "name is required"),
    ({"name": "x" * 201}, "name must be at most 200"),
    ({"prompt": "x" * 4001}, "prompt must be at most 4000"),
    ({"description": 5}, "description is required"),
    ({"contract": {"bogus": 1}}, "contract"),
    ({"contract": "x"}, "contract must be an object"),
])
def test_invalid_body_is_400_with_errors(as_user, patch, fragment):
    r = as_user().post("/api/v1/me/queries", headers=AUTH, json={**Q, **patch})
    assert r.status_code == 400
    assert any(fragment in e for e in r.json()["detail"]["errors"])


def test_limit_of_500(as_user, monkeypatch):
    monkeypatch.setattr(lambda_handler, "MAX_QUERIES", 2)
    c = as_user()
    assert c.post("/api/v1/me/queries", headers=AUTH, json=Q).status_code == 200
    assert c.post("/api/v1/me/queries", headers=AUTH, json=Q).status_code == 200
    r = c.post("/api/v1/me/queries", headers=AUTH, json=Q)
    assert r.status_code == 400 and "at most 2" in r.json()["detail"]["errors"][0]


def test_update_keeps_dates_and_changes_fields(as_user):
    c = as_user()
    made = c.post("/api/v1/me/queries", headers=AUTH, json=Q).json()
    r = c.put(f"/api/v1/me/queries/{made['id']}", headers=AUTH, json={**Q, "name": "renamed", "prompt": "p2"})
    assert r.status_code == 200
    assert r.json()["name"] == "renamed" and r.json()["prompt"] == "p2"
    assert r.json()["createdAt"] == made["createdAt"] and r.json()["query_kind"] == "governed"


def test_used_sets_last_used_at(as_user):
    c = as_user()
    made = c.post("/api/v1/me/queries", headers=AUTH, json=Q).json()
    r = c.post(f"/api/v1/me/queries/{made['id']}/used", headers=AUTH)
    assert r.status_code == 200 and r.json()["lastUsedAt"]
    assert c.get("/api/v1/me/queries", headers=AUTH).json()["queries"][0]["lastUsedAt"] == r.json()["lastUsedAt"]


def test_delete_then_404(as_user):
    c = as_user()
    made = c.post("/api/v1/me/queries", headers=AUTH, json=Q).json()
    assert c.delete(f"/api/v1/me/queries/{made['id']}", headers=AUTH).status_code == 200
    assert c.get("/api/v1/me/queries", headers=AUTH).json()["queries"] == []
    assert c.delete(f"/api/v1/me/queries/{made['id']}", headers=AUTH).status_code == 404


@pytest.mark.parametrize("method,suffix", [("put", ""), ("post", "/used"), ("delete", "")])
def test_unknown_or_malformed_id_is_404(as_user, method, suffix):
    c = as_user()
    for qid in ("q_000000000000", "not-an-id", "q_%23x"):
        kw = {"json": Q} if method == "put" else {}
        assert getattr(c, method)(f"/api/v1/me/queries/{qid}{suffix}", headers=AUTH, **kw).status_code == 404


def test_used_retries_after_a_concurrent_write(as_user, monkeypatch):
    c = as_user()
    made = c.post("/api/v1/me/queries", headers=AUTH, json=Q).json()
    real, calls = user_items_store.put, []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise user_items_store.Conflict("x")
        return real(*a, **kw)

    monkeypatch.setattr(user_items_store, "put", flaky)
    assert c.post(f"/api/v1/me/queries/{made['id']}/used", headers=AUTH).status_code == 200


BIG_CONTRACT = {"metrics": ["metric.reportable_courses.v1"],
                "filters": [{"dimension": "d", "op": "in", "values": ["v" * 17000]}]}


def test_oversized_contract_is_400(as_user):
    c = as_user()
    r = c.post("/api/v1/me/queries", headers=AUTH, json={**Q, "contract": BIG_CONTRACT})
    assert r.status_code == 400
    assert any("contract must be at most 16 KB" in e for e in r.json()["detail"]["errors"])
    made = c.post("/api/v1/me/queries", headers=AUTH, json=Q).json()
    assert c.put(f"/api/v1/me/queries/{made['id']}", headers=AUTH, json={**Q, "contract": BIG_CONTRACT}).status_code == 400


def test_rows_saved_with_the_old_kind_field_read_as_query_kind(as_user):
    from tests.tenant_fakes import TENANT_A

    c = as_user()
    user_items_store.put(TENANT_A.tenant_id, "u1", "query", "q_000000000001",
                         {**Q, "kind": "governed", "createdAt": "2026-01-01T00:00:00Z", "lastUsedAt": None})
    got = c.get("/api/v1/me/queries", headers=AUTH).json()["queries"][0]
    assert got["query_kind"] == "governed" and "kind" not in got
    used = c.post("/api/v1/me/queries/q_000000000001/used", headers=AUTH).json()
    assert used["query_kind"] == "governed" and "kind" not in used
    stored = user_items_store.get(TENANT_A.tenant_id, "u1", "query", "q_000000000001")
    assert stored["query_kind"] == "governed" and "kind" not in stored


def test_oversized_body_is_400(as_user, monkeypatch):
    monkeypatch.setattr(lambda_handler, "MAX_BODY_BYTES", 2000)
    r = as_user().post("/api/v1/me/queries", headers=AUTH, json={**Q, "description": "\u00e9" * 1000})
    assert r.status_code == 400 and any("too large" in e for e in r.json()["detail"]["errors"])
