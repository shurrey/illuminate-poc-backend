import hashlib

import user_items_store
from tests.test_me_cards import AUTH, BUILTIN, NEW, as_user  # noqa: F401
from tests.tenant_fakes import TENANT_A

SUB = "u1"
CONTRACT = {"metrics": ["metric.reportable_courses.v1"]}


def card(cid, **kw):
    return {**NEW, "id": cid, "label": f"card {cid}", "isBuiltIn": False, "enabled": True, **kw}


def query(qid, **kw):
    return {"id": qid, "name": f"q {qid}", "prompt": "p", "description": "d", "contract": CONTRACT,
            "createdAt": "2026-01-01T00:00:00.000Z", "lastUsedAt": None, **kw}


def expected_id(prefix, kind, source):
    return prefix + hashlib.sha256(f"{SUB}:{kind}:{source}".encode()).hexdigest()[:12]


def cards_of(c):
    return c.get("/api/v1/me/cards", headers=AUTH).json()["cards"]


def run(c, **payload):
    return c.post("/api/v1/me/import", headers=AUTH, json=payload)


def test_imports_cards_queries_and_prefs(as_user):
    c = as_user()
    r = run(c,
            cards=[{"id": "average-grade", "isBuiltIn": True, "enabled": False},
                   card("local-1", enabled=False), card("local-2"),
                   {"id": "built-in-unknown", "isBuiltIn": True}],
            queries=[query("a", createdAt="2026-02-01T00:00:00Z", lastUsedAt="2026-03-01T00:00:00Z"), query("b")],
            prefs={"favorites": ["x"], "recentReports": [], "readNotifications": ["n"], "visibleWidgets": ["ai", "zzz"]})
    assert r.status_code == 200
    assert r.json() == {"status": "imported", "cards": 2, "queries": 2, "skipped": []}

    cards = cards_of(c)
    ids = [x["id"] for x in cards]
    c1, c2 = expected_id("c_", "card", "local-1"), expected_id("c_", "card", "local-2")
    assert ids[:3] == ["average-grade", c1, c2] and set(ids) == set(BUILTIN) | {c1, c2}
    enabled = {x["id"]: x["enabled"] for x in cards}
    assert enabled["average-grade"] is False and enabled[c1] is False and enabled[c2] is True
    assert [x["isBuiltIn"] for x in cards].count(False) == 2

    queries = {q["id"]: q for q in c.get("/api/v1/me/queries", headers=AUTH).json()["queries"]}
    qa = queries[expected_id("q_", "query", "a")]
    assert qa["createdAt"] == "2026-02-01T00:00:00Z" and qa["lastUsedAt"] == "2026-03-01T00:00:00Z"
    assert qa["query_kind"] == "governed" and "kind" not in qa

    prefs = c.get("/api/v1/me/prefs", headers=AUTH).json()
    assert prefs["favorites"] == ["x"] and prefs["visibleWidgets"] == ["ai"] and prefs["version"] == 1


def test_invalid_dates_fall_back(as_user):
    c = as_user()
    run(c, queries=[query("a", createdAt="yesterday", lastUsedAt="nope")])
    q = c.get("/api/v1/me/queries", headers=AUTH).json()["queries"][0]
    assert q["createdAt"] != "yesterday" and q["lastUsedAt"] is None


def test_invalid_items_are_skipped_with_reasons(as_user):
    c = as_user()
    r = run(c,
            cards=[card("ok"), card("bad", contract={"bogus": 1}), card("long", label="x" * 201), {"id": 3}],
            queries=[query("ok"), query("bad", contract="x"), query("long", name="y" * 201), "junk"])
    body = r.json()
    assert body["cards"] == 1 and body["queries"] == 1
    assert {(s["kind"], s["name"][:3]) for s in body["skipped"]} == {
        ("card", "car"), ("card", "xxx"), ("card", ""), ("query", "q b"), ("query", "yyy"), ("query", "")}
    assert all(s["reason"] for s in body["skipped"])


def test_limits_skip_the_overflow(as_user, monkeypatch):
    import lambda_handler
    monkeypatch.setattr(lambda_handler, "MAX_CUSTOM_CARDS", 1)
    monkeypatch.setattr(lambda_handler, "MAX_QUERIES", 1)
    c = as_user()
    body = run(c, cards=[card("a"), card("b")], queries=[query("a"), query("b")]).json()
    assert body["cards"] == 1 and body["queries"] == 1
    assert sorted(s["kind"] for s in body["skipped"]) == ["card", "query"]
    assert all("at most 1" in s["reason"] for s in body["skipped"])


def test_existing_prefs_are_kept(as_user):
    c = as_user()
    c.put("/api/v1/me/prefs", headers=AUTH, json={"favorites": ["mine"], "recentReports": [], "readNotifications": [],
                                                 "visibleWidgets": ["ai"], "expected_version": 0})
    run(c, prefs={"favorites": ["theirs"], "recentReports": [], "readNotifications": [], "visibleWidgets": []})
    assert c.get("/api/v1/me/prefs", headers=AUTH).json()["favorites"] == ["mine"]


def test_second_import_is_a_no_op(as_user):
    c = as_user()
    assert run(c, cards=[card("a")], queries=[query("a")]).json()["status"] == "imported"
    again = run(c, cards=[card("zz")], queries=[query("zz")])
    assert again.json() == {"status": "already_imported"}
    assert len(cards_of(c)) == 7
    assert len(c.get("/api/v1/me/queries", headers=AUTH).json()["queries"]) == 1


def test_lost_marker_race_returns_already_imported_without_duplicates(as_user, monkeypatch):
    c = as_user()
    assert run(c, cards=[card("a")], queries=[query("a")]).json()["status"] == "imported"
    user_items_store.delete(TENANT_A.tenant_id, SUB, "imported")
    # The competing call finished everything, including the marker, between our start check and our marker write.
    real_get = user_items_store.get
    state = {"first": True}

    def get(tenant, sub, kind, id=None):
        if kind == "imported" and state["first"]:
            state["first"] = False
            return None
        return real_get(tenant, sub, kind, id)

    real_put = user_items_store.put

    def put(tenant, sub, kind, id, body, **kw):
        if kind == "imported":
            real_put(tenant, sub, kind, id, body)
        return real_put(tenant, sub, kind, id, body, **kw)

    monkeypatch.setattr(user_items_store, "get", get)
    monkeypatch.setattr(user_items_store, "put", put)
    r = run(c, cards=[card("a")], queries=[query("a")])
    assert r.json() == {"status": "already_imported"}
    assert len(cards_of(c)) == 7
    assert len(user_items_store.list_kind(TENANT_A.tenant_id, SUB, "query")) == 1
    assert [i["id"] for i in user_items_store.list_kind(TENANT_A.tenant_id, SUB, "card")] == [expected_id("c_", "card", "a")]


def test_replay_after_partial_failure_counts_collisions_as_present(as_user):
    c = as_user()
    run(c, cards=[card("a")], queries=[query("a")])
    user_items_store.delete(TENANT_A.tenant_id, SUB, "imported")
    r = run(c, cards=[card("a"), card("b")], queries=[query("a"), query("b")])
    assert r.json() == {"status": "imported", "cards": 1, "queries": 1, "skipped": []}
    assert len(cards_of(c)) == 8


def test_bad_shapes_are_400(as_user):
    c = as_user()
    assert run(c, cards="x").status_code == 400
    assert run(c, queries={}).status_code == 400


BIG_CONTRACT = {"metrics": ["metric.reportable_courses.v1"],
                "filters": [{"dimension": "d", "op": "in", "values": ["v" * 17000]}]}


def test_oversized_items_are_skipped(as_user):
    body = run(as_user(), cards=[card("big", contract=BIG_CONTRACT), card("ok")],
               queries=[query("big", contract=BIG_CONTRACT), query("ok")]).json()
    assert body["status"] == "imported" and body["cards"] == 1 and body["queries"] == 1
    assert sorted(s["kind"] for s in body["skipped"]) == ["card", "query"]
    assert all("16 KB" in s["reason"] for s in body["skipped"])


def _failing_put(monkeypatch, fails):
    from botocore.exceptions import ClientError

    real_put = user_items_store.put

    def put(tenant, sub, kind, id, body, **kw):
        if fails(kind, body):
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}}, "PutItem")
        return real_put(tenant, sub, kind, id, body, **kw)

    monkeypatch.setattr(user_items_store, "put", put)


def test_a_store_error_on_one_item_is_partial_and_writes_no_marker(as_user, monkeypatch, caplog):
    c = as_user()
    _failing_put(monkeypatch, lambda kind, body: body.get("name") == "q bad" or body.get("label") == "card bad")
    r = run(c, cards=[card("bad"), card("ok")], queries=[query("bad"), query("ok")],
            prefs={"favorites": ["x"], "recentReports": [], "readNotifications": [], "visibleWidgets": ["ai"]})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "partial" and body["cards"] == 1 and body["queries"] == 1
    assert {(s["kind"], s["name"], s["reason"]) for s in body["skipped"]} == {
        ("card", "card bad", "could not be saved"), ("query", "q bad", "could not be saved")}
    assert user_items_store.get(TENANT_A.tenant_id, SUB, "imported") is None
    assert c.get("/api/v1/me/prefs", headers=AUTH).json()["favorites"] == ["x"]
    assert "q bad" not in caplog.text and "card bad" not in caplog.text


def test_a_retry_after_partial_imports_the_rest_and_writes_the_marker(as_user, monkeypatch):
    c = as_user()
    payload = {"cards": [card("bad"), card("ok")], "queries": [query("bad"), query("ok")]}
    real_put = user_items_store.put
    _failing_put(monkeypatch, lambda kind, body: body.get("name") == "q bad" or body.get("label") == "card bad")
    assert run(c, **payload).json()["status"] == "partial"
    monkeypatch.setattr(user_items_store, "put", real_put)
    r = run(c, **payload).json()
    assert r == {"status": "imported", "cards": 1, "queries": 1, "skipped": []}
    assert user_items_store.get(TENANT_A.tenant_id, SUB, "imported") is not None
    assert len(user_items_store.list_kind(TENANT_A.tenant_id, SUB, "card")) == 2
    assert len(user_items_store.list_kind(TENANT_A.tenant_id, SUB, "query")) == 2


def test_validation_skips_alone_still_write_the_marker(as_user):
    body = run(as_user(), queries=[query("bad", contract="x"), query("ok")]).json()
    assert body["status"] == "imported" and len(body["skipped"]) == 1
    assert user_items_store.get(TENANT_A.tenant_id, SUB, "imported") is not None


def test_a_value_error_on_one_item_is_skipped_without_blocking_the_marker(as_user, monkeypatch):
    # A ValueError comes from key validation, so a retry would fail the same way.
    real_put = user_items_store.put

    def put(tenant, sub, kind, id, body, **kw):
        if body.get("name") == "q bad":
            raise ValueError("bad id")
        return real_put(tenant, sub, kind, id, body, **kw)

    monkeypatch.setattr(user_items_store, "put", put)
    body = run(as_user(), queries=[query("bad"), query("ok")]).json()
    assert body["status"] == "imported" and body["queries"] == 1
    assert [s["name"] for s in body["skipped"]] == ["q bad"]


def test_failed_layout_and_prefs_writes_are_partial(as_user, monkeypatch):
    _failing_put(monkeypatch, lambda kind, body: kind in ("layout", "prefs"))
    body = run(as_user(), cards=[card("a")],
               prefs={"favorites": ["x"], "recentReports": [], "readNotifications": [], "visibleWidgets": ["ai"]}).json()
    assert body["status"] == "partial" and body["cards"] == 1
    assert sorted(s["kind"] for s in body["skipped"]) == ["layout", "prefs"]
    assert user_items_store.get(TENANT_A.tenant_id, SUB, "imported") is None


def test_a_failed_marker_write_is_a_server_error(as_user, monkeypatch):
    import pytest
    from botocore.exceptions import ClientError

    _failing_put(monkeypatch, lambda kind, body: kind == "imported")
    with pytest.raises(ClientError):
        run(as_user(), queries=[query("a")])


def test_oversized_prefs_are_skipped(as_user):
    ids = [f"{i:03d}" + "\U0001F600" * 190 for i in range(200)]
    c = as_user()
    body = run(c, prefs={"favorites": ids, "recentReports": [], "readNotifications": ids, "visibleWidgets": ["ai"]}).json()
    assert body["status"] == "imported"
    assert [(s["kind"], "too large" in s["reason"]) for s in body["skipped"]] == [("prefs", True)]
    assert c.get("/api/v1/me/prefs", headers=AUTH).json()["version"] == 0
