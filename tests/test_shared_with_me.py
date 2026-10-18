import pytest

import lambda_handler
from botocore.exceptions import ClientError

import user_items_store as store
from semantic_layer.redact import redact_contract, redact_sql
from tests.test_directory import person, seed
from tests.test_me_cards import AUTH, NEW, as_user  # noqa: F401
from tests.test_me_queries import Q
from tests.test_me_sql_queries import EMAIL, FREEHAND, SQLQ, executed  # noqa: F401
from tests.tenant_fakes import TENANT_A, TENANT_B

T = TENANT_A.tenant_id
ADMIN = "illuminate-admins"
OWNER = {**person("owner", "Olive Owner", "o@a.edu"), "cognito:groups": [ADMIN]}
ANN = person("ann", "Ann Lee", "ann@a.edu")
BOB = person("bob", "Bob Ames", "bob@a.edu")
ADA = {**person("ada", "Ada Admin", "ada@a.edu"), "cognito:groups": [ADMIN]}
PII_CONTRACT = {"measures": ["dataset.course_student_activity.v1:students"], "dimensions": ["person_email"],
                "filters": [{"dimension": "person_email", "op": "eq", "values": ["jane@example.edu"]}]}
LITERAL_SQL = "SELECT COUNT(*) AS n FROM CDM_LMS.PERSON WHERE EMAIL = 'jane@example.edu'"
PARTS_SQL = "SELECT students FROM governed_1 WHERE person_email = 'jane@example.edu'"
NOT_FOUND = {"detail": "Shared item not found"}


@pytest.fixture
def world(as_user):
    seed(as_user, OWNER, ANN, BOB, ADA)
    return as_user


def make(client, plural, body):
    r = client.post(f"/api/v1/me/{plural}", headers=AUTH, json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def share(client, plural, item_id, recipients=(), tenant=False):
    r = client.put(f"/api/v1/me/{plural}/{item_id}/share", headers=AUTH,
                   json={"recipients": list(recipients), "tenant": tenant})
    assert r.status_code == 200, r.text
    return r.json()["share_id"]


def shared(client):
    r = client.get("/api/v1/shared", headers=AUTH)
    assert r.status_code == 200
    return r.json()["items"]


def view(client, sid):
    return client.get(f"/api/v1/shared/{sid}", headers=AUTH)


def run(client, sid):
    return client.post(f"/api/v1/shared/{sid}/run", headers=AUTH)


def copy(client, sid):
    return client.post(f"/api/v1/shared/{sid}/copy", headers=AUTH)


def test_a_named_recipient_sees_the_share_and_others_do_not(world):
    o = world(OWNER)
    qid = make(o, "queries", {**Q, "name": "Weekly", "description": "Run on Mondays"})
    sid = share(o, "queries", qid, ["ann"])

    items = shared(world(ANN))
    assert items == [{"share_id": sid, "item_kind": "query", "name": "Weekly", "owners_note": "Run on Mondays",
                      "owner": {"name": "Olive Owner"}, "query_kind": "governed", "governance_hint": "governed",
                      "shared_at": items[0]["shared_at"]}]
    assert shared(world(BOB)) == []


def test_a_tenant_wide_share_lists_for_everyone_but_the_owner(world):
    o = world(OWNER)
    cid = make(o, "cards", NEW)
    sid = share(o, "cards", cid, tenant=True)
    for who in (ANN, BOB):
        assert [(i["share_id"], i["item_kind"], i["label"], i["format"]) for i in shared(world(who))] == [
            (sid, "card", "Mine", "number")]
    assert shared(world(OWNER)) == []


def test_the_listing_never_exposes_owner_sub_or_item_ids(world):
    o = world(OWNER)
    qid = make(o, "queries", Q)
    share(o, "queries", qid, ["ann"])
    text = world(ANN).get("/api/v1/shared", headers=AUTH).text
    assert qid not in text and '"owner"' in text and "owner_sub" not in text and '"sub"' not in text


def test_owner_name_falls_back_to_email_then_a_colleague(world):
    o = world(OWNER)
    sid = share(o, "queries", make(o, "queries", Q), ["ann"])
    store.put(T, "owner", "directory", None, {"sub": "owner", "name": "", "email": "olive@a.edu"},
              expected_version=store.get(T, "owner", "directory")["version"])
    assert shared(world(ANN))[0]["owner"] == {"name": "olive"}
    store.delete(T, "owner", "directory")
    assert shared(world(ANN))[0]["owner"] == {"name": "A colleague"}
    assert view(world(ANN), sid).json()["owner"] == {"name": "A colleague"}


def test_a_share_whose_item_is_gone_is_skipped_and_404(world, executed):
    o = world(OWNER)
    qid = make(o, "queries", Q)
    sid = share(o, "queries", qid, ["ann"])
    store.delete(T, "owner", "query", qid)  # orphan: the share outlives its item
    a = world(ANN)
    assert shared(a) == []
    for r in (view(a, sid), run(a, sid), copy(a, sid)):
        assert r.status_code == 404 and r.json() == NOT_FOUND
    assert executed == []


def test_a_viewer_sees_redacted_literals_and_an_admin_the_stored_text(world):
    o = world(OWNER)
    sql_id = make(o, "queries", {**SQLQ, "name": "lit", "sql": PARTS_SQL,
                                 "governed_parts": {"governed_1": PII_CONTRACT}})
    gov_id = make(o, "queries", {**Q, "contract": PII_CONTRACT, "prompt": "emails for jane@example.edu"})
    card_id = make(o, "cards", {**NEW, "contract": PII_CONTRACT})
    sids = [share(o, p, i, ["ann", "ada"]) for p, i in (("queries", sql_id), ("queries", gov_id), ("cards", card_id))]

    viewer = [view(world(ANN), s) for s in sids]
    assert all(r.status_code == 200 for r in viewer)
    assert all("jane@example.edu" not in r.text for r in viewer)
    assert viewer[0].json()["sql"] == "SELECT students FROM governed_1 WHERE person_email = '…'"
    assert viewer[0].json()["governed_parts"]["governed_1"]["filters"][0]["values"] == ["…"]
    assert viewer[1].json()["contract"]["filters"][0]["values"] == ["…"] and "prompt" not in viewer[1].json()
    assert viewer[2].json()["contract"]["filters"][0]["values"] == ["…"]
    assert all(r.json()["redacted"] is True for r in viewer)

    admin = [view(world(ADA), s).json() for s in sids]
    assert admin[0]["sql"] == PARTS_SQL and admin[0]["governed_parts"]["governed_1"] == PII_CONTRACT
    assert admin[1]["contract"] == PII_CONTRACT and admin[1]["prompt"] == "emails for jane@example.edu"
    assert admin[2]["contract"] == PII_CONTRACT
    assert all(a["redacted"] is False for a in admin)
    for body in admin:
        assert not {"owner_sub", "item_id", "id", "owner_name", "lastUsedAt", "version"} & set(body)


def test_unparseable_sql_is_withheld_from_a_viewer(world):
    o = world(OWNER)
    qid = make(o, "queries", SQLQ)
    sid = share(o, "queries", qid, ["ann"])
    stored = store.get(T, "owner", "query", qid)
    body = {k: v for k, v in stored.items() if k not in ("id", "version", "updated_at")}
    store.put(T, "owner", "query", qid, {**body, "sql": "SELECT 'unterminated"}, expected_version=stored["version"])
    got = view(world(ANN), sid).json()
    assert got["sql"] is None and got["note"]


def test_a_run_uses_the_runners_role(world, executed):
    o = world(OWNER)
    email_sid = share(o, "queries", make(o, "queries", {**SQLQ, "sql": EMAIL}), ["ann", "ada"])
    pii_sid = share(o, "cards", make(o, "cards", {**NEW, "contract": PII_CONTRACT}), ["ann", "ada"])

    for sid in (email_sid, pii_sid):
        refused = run(world(ANN), sid)
        assert refused.status_code == 400 and "jane@example.edu" not in refused.text
    assert executed == []
    assert run(world(ADA), email_sid).status_code == 200
    assert run(world(ADA), pii_sid).status_code == 200 and len(executed) == 2


FILTER_ONLY = {"measures": PII_CONTRACT["measures"], "filters": PII_CONTRACT["filters"]}


@pytest.mark.parametrize("plural, body", [
    ("cards", {**NEW, "contract": FILTER_ONLY}),
    ("queries", {**SQLQ, "sql": LITERAL_SQL}),
    ("queries", {**SQLQ, "sql": "SELECT students FROM governed_1 WHERE students > 5",
                 "governed_parts": {"governed_1": FILTER_ONLY}}),
])
def test_a_viewers_shared_run_masks_values_and_an_admins_does_not(world, executed, plural, body):
    o = world(OWNER)
    sid = share(o, plural, make(o, plural, body), ["ann", "ada"])
    viewer = run(world(ANN), sid)
    assert viewer.status_code == 200, viewer.text
    assert "jane@example.edu" not in viewer.text and "…" in viewer.json()["sql"]
    for part in (viewer.json().get("governed_parts") or {}).values():
        assert part == redact_contract(part)
    assert "jane@example.edu" in run(world(ADA), sid).text


def test_a_shared_sql_runs_reason_is_the_owners_note_or_their_name(world, executed):
    o = world(OWNER)
    noted = share(o, "queries", make(o, "queries", {**SQLQ, "description": "For the dean"}), ["ann"])
    bare = share(o, "queries", make(o, "queries", SQLQ), ["ann"])
    assert run(world(ANN), noted).json()["provenance"]["reason"] == "Owner's note: For the dean"
    assert run(world(ANN), bare).json()["provenance"]["reason"] == "Shared by Olive Owner"


def test_the_owners_edit_is_reflected_in_the_next_view_and_run(world, executed):
    o = world(OWNER)
    qid = make(o, "queries", SQLQ)
    sid = share(o, "queries", qid, ["ann"])
    assert run(world(ANN), sid).status_code == 200 and executed == [FREEHAND]
    edited = "SELECT COUNT(*) AS m FROM CDM_LMS.COURSE"
    put = world(OWNER).put(f"/api/v1/me/queries/{qid}", headers=AUTH, json={**SQLQ, "name": "v2", "sql": edited})
    assert put.status_code == 200
    assert view(world(ANN), sid).json()["name"] == "v2"
    assert run(world(ANN), sid).status_code == 200 and executed[-1] == edited


def test_unsharing_is_a_404_on_the_next_run(world, executed):
    o = world(OWNER)
    qid = make(o, "queries", SQLQ)
    sid = share(o, "queries", qid, ["ann", "bob"])
    assert run(world(ANN), sid).status_code == 200
    share(world(OWNER), "queries", qid, ["bob"])
    assert run(world(ANN), sid).json() == NOT_FOUND
    assert world(OWNER).delete(f"/api/v1/me/queries/{qid}/share", headers=AUTH).status_code == 200
    for r in (run(world(BOB), sid), view(world(BOB), sid), copy(world(BOB), sid)):
        assert r.status_code == 404 and r.json() == NOT_FOUND


def test_a_shared_run_never_writes_the_owners_row(world, executed):
    o = world(OWNER)
    qid = make(o, "queries", SQLQ)
    sid = share(o, "queries", qid, ["ann"])
    before = store.get(T, "owner", "query", qid)
    assert run(world(ANN), sid).status_code == 200
    after = store.get(T, "owner", "query", qid)
    assert after == before and after["lastUsedAt"] is None
    assert store.list_kind(T, "ann", "query") == []


def test_the_owner_still_records_last_used_on_their_own_run(world, executed):
    o = world(OWNER)
    qid = make(o, "queries", SQLQ)
    assert o.post(f"/api/v1/me/queries/{qid}/run", headers=AUTH).status_code == 200
    assert store.get(T, "owner", "query", qid)["lastUsedAt"]


def test_copy_saves_into_the_recipients_profile_unredacted(world):
    o = world(OWNER)
    qid = make(o, "queries", {**SQLQ, "name": "lit", "description": "note", "sql": LITERAL_SQL})
    cid = make(o, "cards", {**NEW, "contract": PII_CONTRACT})
    q_sid, c_sid = share(o, "queries", qid, ["ada"]), share(o, "cards", cid, ["ada"])
    a = world(ADA)

    got = copy(a, q_sid)
    assert got.status_code == 200 and got.json()["id"] != qid
    mine = store.list_kind(T, "ada", "query")
    assert len(mine) == 1 and mine[0]["sql"] == LITERAL_SQL and mine[0]["owner_name"] == "Ada Admin"
    assert mine[0]["name"] == "lit" and mine[0]["description"] == "note" and mine[0]["lastUsedAt"] is None

    card = copy(a, c_sid)
    assert card.status_code == 200 and card.json()["id"] != cid
    assert store.list_kind(T, "ada", "card")[0]["contract"] == PII_CONTRACT
    assert card.json()["id"] in [c["id"] for c in a.get("/api/v1/me/cards", headers=AUTH).json()["cards"]]
    assert store.get(T, "owner", "query", qid)["version"] == 1 and store.get(T, "owner", "card", cid)["version"] == 1


def test_a_viewer_copying_an_email_query_is_refused_with_the_reason(world):
    o = world(OWNER)
    sid = share(o, "queries", make(o, "queries", {**SQLQ, "sql": EMAIL}), ["ann", "ada"])
    refused = copy(world(ANN), sid)
    assert refused.status_code == 400 and refused.json()["detail"]["errors"]
    assert store.list_kind(T, "ann", "query") == []
    assert copy(world(ADA), sid).status_code == 200


HIDDEN = "This item contains values hidden for your role, so it can't be copied."


@pytest.mark.parametrize("plural,body", [
    ("queries", {**SQLQ, "sql": "SELECT COUNT(*) AS n FROM CDM_LMS.COURSE WHERE COURSE_NAME = 'Bio'"}),
    ("queries", {**SQLQ, "sql": "SELECT COUNT(*) AS n FROM CDM_LMS.COURSE WHERE ID = 12345"}),
    ("queries", {**SQLQ, "sql": "SELECT COUNT(*) AS n -- for jane@x.edu\nFROM CDM_LMS.COURSE"}),
    ("queries", {**SQLQ, "sql": "SELECT students FROM governed_1",
                 "governed_parts": {"governed_1": {**PII_CONTRACT, "dimensions": []}}}),
    ("queries", {**Q, "contract": PII_CONTRACT, "prompt": ""}),
    ("queries", {**Q, "prompt": "emails for jane@example.edu"}),
    ("cards", {**NEW, "contract": PII_CONTRACT, "prompt": ""}),
    ("cards", {**NEW, "prompt": "p"}),
], ids=["sql-string", "sql-number", "sql-comment", "governed-part", "filter-value", "prompt", "card-filter",
        "card-prompt"])
def test_a_viewer_cannot_copy_an_item_with_hidden_values_but_an_admin_can(world, plural, body):
    o = world(OWNER)
    sid = share(o, plural, make(o, plural, body), ["ann", "ada"])
    kind = "query" if plural == "queries" else "card"
    refused = copy(world(ANN), sid)
    assert refused.status_code == 400 and refused.json()["detail"]["errors"] == [HIDDEN]
    assert store.list_kind(T, "ann", kind) == []
    assert copy(world(ADA), sid).status_code == 200 and len(store.list_kind(T, "ada", kind)) == 1


def test_a_viewer_can_copy_an_item_with_nothing_to_hide(world):
    o = world(OWNER)
    q_sid = share(o, "queries", make(o, "queries", {**SQLQ, "sql": "SELECT COUNT(*) AS n FROM CDM_LMS.COURSE LIMIT 10"}),
                  ["ann"])
    g_sid = share(o, "queries", make(o, "queries", {**Q, "prompt": ""}), ["ann"])
    c_sid = share(o, "cards", make(o, "cards", {**NEW, "prompt": ""}), ["ann"])
    a = world(ANN)
    assert [copy(a, s).status_code for s in (q_sid, g_sid, c_sid)] == [200, 200, 200]
    assert len(store.list_kind(T, "ann", "query")) == 2 and len(store.list_kind(T, "ann", "card")) == 1


@pytest.mark.parametrize("sid", ["sh_0000000000000000", "nope", "sh_XYZ", "sh_00000000000000000"])
def test_unknown_and_malformed_share_ids_are_the_same_404(world, sid):
    a = world(ANN)
    for r in (view(a, sid), run(a, sid), copy(a, sid)):
        assert r.status_code == 404 and r.json() == NOT_FOUND


def test_the_owner_and_a_non_member_get_the_same_404(world):
    o = world(OWNER)
    sid = share(o, "queries", make(o, "queries", Q), ["ann"])
    for who in (OWNER, BOB):
        c = world(who)
        for r in (view(c, sid), run(c, sid), copy(c, sid)):
            assert r.status_code == 404 and r.json() == NOT_FOUND


def test_another_tenants_caller_with_the_same_sub_gets_404(world):
    o = world(OWNER)
    sid = share(o, "queries", make(o, "queries", Q), tenant=True)
    twin = person("ann", "Ann Lee", "ann@b.edu", TENANT_B)
    seed(world, twin)
    c = world(twin)
    assert shared(c) == []
    for r in (view(c, sid), run(c, sid), copy(c, sid)):
        assert r.status_code == 404 and r.json() == NOT_FOUND


def test_listing_paginates(world, monkeypatch):
    o = world(OWNER)
    sids = {share(o, "queries", make(o, "queries", Q), ["ann"]) for _ in range(3)}
    real = store._get_table().query
    monkeypatch.setattr(store._get_table(), "query", lambda **kw: real(**{**kw, "Limit": 1}))
    assert {i["share_id"] for i in shared(world(ANN))} == sids


def _cancelled(*codes):
    return ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "x"},
                        "CancellationReasons": [{"Code": c} for c in codes]}, "TransactWriteItems")


def test_delete_share_retries_a_transaction_conflict_once(world, monkeypatch):
    store.put_share(T, "owner", "query", "q_a", {"recipients": [], "tenant": True})
    client = store._get_table().meta.client
    real, calls = client.transact_write_items, []

    def flaky(**kw):
        calls.append(kw)
        if len(calls) == 1:
            raise _cancelled("TransactionConflict", "None")
        return real(**kw)

    monkeypatch.setattr(client, "transact_write_items", flaky)
    assert store.delete_share(T, "owner", "query", "q_a") is True and len(calls) == 2
    assert store.get_share(T, "owner", "query", "q_a") is None


def test_delete_share_reraises_a_persistent_failure(world, monkeypatch):
    store.put_share(T, "owner", "query", "q_a", {"recipients": [], "tenant": True})
    client = store._get_table().meta.client

    def always(**kw):
        raise _cancelled("TransactionConflict", "None")

    monkeypatch.setattr(client, "transact_write_items", always)
    with pytest.raises(ClientError):
        store.delete_share(T, "owner", "query", "q_a")


def test_delete_share_treats_a_failed_condition_as_nothing_removed(world, monkeypatch):
    store.put_share(T, "owner", "query", "q_a", {"recipients": [], "tenant": True})
    client = store._get_table().meta.client

    def gone(**kw):
        raise _cancelled("ConditionalCheckFailed", "None")

    monkeypatch.setattr(client, "transact_write_items", gone)
    assert store.delete_share(T, "owner", "query", "q_a") is False


@pytest.mark.parametrize("sql,expected", [
    ("SELECT 'a''b', $$x$$, N'n' FROM t WHERE e = 'j@x.edu' AND n = 1",
     "SELECT '…', '…', '…' FROM t WHERE e = '…' AND n = '…'"),
    ("SELECT a FROM t WHERE student_id = 12345 AND g > 1.5 LIMIT 10 OFFSET 20",
     "SELECT a FROM t WHERE student_id = '…' AND g > '…' LIMIT 10 OFFSET 20"),
    ("SELECT TOP 5 a FROM t SAMPLE (10) FETCH FIRST 3 ROWS ONLY",
     "SELECT TOP 5 a FROM t SAMPLE (10) FETCH FIRST 3 ROWS ONLY"),
    ("SELECT a FROM t TABLESAMPLE BERNOULLI (20 ROWS) SEED (7)",
     "SELECT a FROM t TABLESAMPLE BERNOULLI (20 ROWS) SEED (7)"),
    ("SELECT seed(12345), repeatable (678) FROM t", "SELECT seed('…'), repeatable ('…') FROM t"),
    ("SELECT a FROM t SAMPLE (10) QUALIFY seed(5) > 1", "SELECT a FROM t SAMPLE (10) QUALIFY seed('…') > '…'"),
    ("SELECT a FROM t SAMPLE (10) UNION ALL SELECT seed(5) FROM u",
     "SELECT a FROM t SAMPLE (10) UNION ALL SELECT seed('…') FROM u"),
    ("SELECT a FROM t TABLESAMPLE BERNOULLI (10) REPEATABLE (7)",
     "SELECT a FROM t TABLESAMPLE BERNOULLI (10) REPEATABLE (7)"),
    ("SELECT a FROM t SAMPLE (10) SEED (7) WHERE b = 1", "SELECT a FROM t SAMPLE (10) SEED (7) WHERE b = '…'"),
    ("SELECT a -- for jane@x.edu\nFROM t /* jane */ WHERE b = 1",
     "SELECT a\nFROM t WHERE b = '…'"),
    ("SELECT a FROM t -- trailing jane", "SELECT a FROM t"),
    ('SELECT "Col" FROM t', 'SELECT "Col" FROM t'),
    ("SELECT 'x\nFROM t", None),
    ("SELECT FROM WHERE (", None),
])
def test_redact_sql(sql, expected):
    assert redact_sql(sql) == expected


def test_redact_contract_replaces_every_filter_value_and_keeps_the_rest():
    contract = {"metrics": ["m"], "filters": [{"dimension": "d", "op": "in", "values": ["a", 2, True]},
                                              {"dimension": "e", "op": "is_null"}], "limit": 5}
    assert redact_contract(contract) == {"metrics": ["m"], "limit": 5, "filters": [
        {"dimension": "d", "op": "in", "values": ["…", "…", "…"]}, {"dimension": "e", "op": "is_null"}]}
    assert contract["filters"][0]["values"] == ["a", 2, True]
    assert redact_contract("tampered") == {}
    assert redact_contract({"metrics": ["m"], "filters": "x"}) == {"metrics": ["m"], "filters": []}


def test_delete_share_never_removes_a_newer_share(world, monkeypatch):
    newer = store.put_share(T, "owner", "query", "q_a", {"recipients": [], "tenant": True})
    stale = {**newer, "share_id": "sh_" + "0" * 16}
    real = store.get_share
    monkeypatch.setattr(store, "get_share", lambda *a: stale)
    assert store.delete_share(T, "owner", "query", "q_a") is False
    monkeypatch.setattr(store, "get_share", real)
    assert store.get_share(T, "owner", "query", "q_a")["share_id"] == newer["share_id"]
    assert store.get_share_pointer(T, newer["share_id"]) is not None


def test_a_governed_item_without_a_contract_hides_nothing_so_a_viewer_can_copy_it(world):
    for kind, item in (("card", {"label": "x"}), ("query", {"query_kind": "governed", "name": "n"})):
        assert lambda_handler._hides_values(kind, item) is False
    assert lambda_handler._hides_values("card", {"contract": "tampered"}) is True


# --- pinned shared cards on the home page ---

PIN = "/api/v1/me/cards/pin-shared"


def pin(client, sid):
    return client.post(PIN, headers=AUTH, json={"share_id": sid})


def home(client):
    r = client.get("/api/v1/me/cards", headers=AUTH)
    assert r.status_code == 200
    return r.json()


def shared_cards(client):
    return [c for c in home(client)["cards"] if c.get("shared") is True]


def test_pin_view_and_run_a_shared_card(world, monkeypatch):
    o = world(OWNER)
    cid = make(o, "cards", NEW)
    sid = share(o, "cards", cid, ["ann"])
    a = world(ANN)
    r = pin(a, sid)
    assert r.status_code == 200
    assert shared_cards(a) == [{"label": "Mine", "description": "d", "longDescription": "ld", "format": "number",
                                "contract": NEW["contract"], "id": sid, "shared": True,
                                "owner": {"name": "Olive Owner"}, "isBuiltIn": False, "enabled": True}]
    assert cid not in a.get("/api/v1/me/cards", headers=AUTH).text
    assert [c["id"] for c in r.json()["cards"]][-1] == sid
    assert store.list_kind(T, "ann", "card") == []

    seen = []

    async def fake(contract, user, tenant):
        seen.append((contract, user["sub"]))
        return {"ok": True}

    monkeypatch.setattr(lambda_handler, "_run_contract", fake)
    assert run(a, sid).status_code == 200 and seen == [(NEW["contract"], "ann")]


def test_pinning_is_idempotent_and_survives_a_layout_put_and_reset(world):
    o = world(OWNER)
    sid = share(o, "cards", make(o, "cards", NEW), ["ann"])
    a = world(ANN)
    pin(a, sid)
    first = pin(a, sid).json()
    assert [c["id"] for c in first["cards"]].count(sid) == 1
    order = [c["id"] for c in first["cards"]]
    put = a.put("/api/v1/me/cards/layout", headers=AUTH,
                json={"order": order[::-1], "disabled": [sid], "hidden": [], "expected_version": first["version"]})
    assert put.status_code == 200
    assert [c["id"] for c in put.json()["cards"]][0] == sid and put.json()["disabled"] == [sid]
    reset = a.post("/api/v1/me/cards/reset", headers=AUTH)
    assert sid in [c["id"] for c in reset.json()["cards"]] and reset.json()["disabled"] == [sid]


def test_remove_a_pinned_card_through_delete(world):
    o = world(OWNER)
    sid = share(o, "cards", make(o, "cards", NEW), ["ann"])
    a = world(ANN)
    pin(a, sid)
    r = a.delete(f"/api/v1/me/cards/{sid}", headers=AUTH)
    assert r.status_code == 200 and sid not in [c["id"] for c in r.json()["cards"]]
    assert a.delete(f"/api/v1/me/cards/{sid}", headers=AUTH).status_code == 404
    assert view(a, sid).status_code == 200


def test_unsharing_or_deleting_removes_the_card_without_rewriting_the_layout(world):
    o = world(OWNER)
    cid = make(o, "cards", NEW)
    sid = share(o, "cards", cid, ["ann"])
    pin(world(ANN), sid)
    before = store.get(T, "ann", "layout")
    assert world(OWNER).delete(f"/api/v1/me/cards/{cid}/share", headers=AUTH).status_code == 200
    a = world(ANN)
    assert shared_cards(a) == [] and sid not in a.get("/api/v1/me/cards", headers=AUTH).text
    assert store.get(T, "ann", "layout") == before
    sid2 = share(world(OWNER), "cards", cid, ["ann"])
    assert sid2 != sid and shared_cards(world(ANN)) == []


def test_the_owners_edit_is_reflected_and_a_removed_member_loses_the_card(world):
    o = world(OWNER)
    cid = make(o, "cards", NEW)
    sid = share(o, "cards", cid, ["ann", "bob"])
    pin(world(ANN), sid)
    o = world(OWNER)
    assert o.put(f"/api/v1/me/cards/{cid}", headers=AUTH, json={**NEW, "label": "Renamed"}).status_code == 200
    assert [c["label"] for c in shared_cards(world(ANN))] == ["Renamed"]
    share(world(OWNER), "cards", cid, ["bob"])
    assert shared_cards(world(ANN)) == []


def test_a_non_member_a_query_share_and_junk_cannot_be_pinned(world):
    o = world(OWNER)
    sid = share(o, "cards", make(o, "cards", NEW), ["ann"])
    qsid = share(o, "queries", make(o, "queries", Q), ["bob"])
    for who, target in ((BOB, sid), (BOB, qsid), (OWNER, sid), (ANN, "sh_0000000000000000"), (ANN, "nope")):
        r = pin(world(who), target)
        assert r.status_code == 404 and r.json() == NOT_FOUND
    assert pin(world(ANN), 7).status_code == 404
    for who, target in ((BOB, sid), (ANN, qsid), (BOB, "sh_0000000000000000")):
        c = world(who)
        r = c.put("/api/v1/me/cards/layout", headers=AUTH,
                  json={"order": [target], "disabled": [], "hidden": [], "expected_version": 0})
        assert r.status_code == 400
    assert store.get(T, "bob", "layout") is None


def test_a_layout_put_naming_a_pin_unshared_since_load_drops_it(world):
    o = world(OWNER)
    cid = make(o, "cards", NEW)
    sid = share(o, "cards", cid, ["ann"])
    a = world(ANN)
    loaded = pin(a, sid).json()
    assert world(OWNER).delete(f"/api/v1/me/cards/{cid}/share", headers=AUTH).status_code == 200
    a = world(ANN)
    order = [c["id"] for c in loaded["cards"]]
    r = a.put("/api/v1/me/cards/layout", headers=AUTH,
              json={"order": order[::-1], "disabled": [sid], "hidden": [], "expected_version": loaded["version"]})
    assert r.status_code == 200, r.text
    assert sid not in store.get(T, "ann", "layout")["order"] and r.json()["disabled"] == []
    assert [c["id"] for c in r.json()["cards"]] == [i for i in order[::-1] if i != sid]


def test_a_viewer_sees_a_pinned_card_redacted_and_an_admin_the_stored_one(world):
    o = world(OWNER)
    sid = share(o, "cards", make(o, "cards", {**NEW, "contract": PII_CONTRACT}), ["ann", "ada"])
    for who in (ANN, ADA):
        c = world(who)
        pin(c, sid)
    viewer, admin = shared_cards(world(ANN))[0], shared_cards(world(ADA))[0]
    assert "prompt" not in viewer and viewer["contract"]["filters"][0]["values"] == ["…"]
    assert "jane@example.edu" not in str(viewer)
    assert admin["prompt"] == "p" and admin["contract"] == PII_CONTRACT


def test_the_owners_own_card_listing_is_unchanged_by_recipients_pinning(world):
    o = world(OWNER)
    cid = make(o, "cards", NEW)
    sid = share(o, "cards", cid, ["ann"])
    before = home(o)
    pin(world(ANN), sid)
    assert home(world(OWNER)) == before
    assert [c["id"] for c in before["cards"] if c.get("shared") is True] == []


def test_at_most_fifty_shared_cards_can_be_pinned(world):
    o = world(OWNER)
    sids = [share(o, "cards", make(o, "cards", {**NEW, "label": f"c{i}"}), ["ann"]) for i in range(51)]
    a = world(ANN)
    assert all(pin(a, s).status_code == 200 for s in sids[:50])
    r = pin(a, sids[50])
    assert r.status_code == 400 and "50" in str(r.json())
    assert pin(a, sids[0]).status_code == 200
    layout = home(a)
    over = a.put("/api/v1/me/cards/layout", headers=AUTH, json={
        "order": [c["id"] for c in layout["cards"]] + [sids[50]], "disabled": [], "hidden": [],
        "expected_version": layout["version"]})
    assert over.status_code == 400


def test_delete_removes_a_pin_that_is_only_disabled_and_pin_prunes_dead_pins(world):
    o = world(OWNER)
    c1, c2 = make(o, "cards", NEW), make(o, "cards", {**NEW, "label": "two"})
    s1, s2 = share(o, "cards", c1, ["ann"]), share(world(OWNER), "cards", c2, ["ann"])
    a = world(ANN)
    pin(a, s1)
    pin(a, s2)
    layout = store.get(T, "ann", "layout")
    store.put(T, "ann", "layout", None, {"order": [i for i in layout["order"] if i != s1], "disabled": [s1],
                                         "hidden": []}, expected_version=layout["version"])
    assert a.delete(f"/api/v1/me/cards/{s1}", headers=AUTH).status_code == 200
    assert s1 not in store.get(T, "ann", "layout")["disabled"]

    pin(a, s1)
    world(OWNER).delete(f"/api/v1/me/cards/{c2}/share", headers=AUTH)
    pin(world(ANN), s1)
    assert s2 not in store.get(T, "ann", "layout")["order"]
