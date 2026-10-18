import re

import pytest

import lambda_handler
import user_items_store as store
from tests.test_directory import person, seed
from tests.test_me_cards import AUTH, NEW, as_user  # noqa: F401
from tests.test_me_queries import Q
from tests.tenant_fakes import TENANT_A, TENANT_B

T = TENANT_A.tenant_id
OWNER = person("owner", "Olive Owner", "o@a.edu")
ANN = person("ann", "Ann Lee", "ann@a.edu")
BOB = person("bob", "Bob Ames", "bob@a.edu")
KINDS = [("queries", "query", Q), ("cards", "card", NEW)]


def make(client, plural, body):
    r = client.post(f"/api/v1/me/{plural}", headers=AUTH, json=body)
    assert r.status_code == 200
    return r.json()["id"]


def share(client, plural, item_id, recipients=(), tenant=False, method="put"):
    url = f"/api/v1/me/{plural}/{item_id}/share"
    if method == "delete":
        return client.delete(url, headers=AUTH)
    return client.put(url, headers=AUTH, json={"recipients": list(recipients), "tenant": tenant})


@pytest.fixture
def world(as_user):
    seed(as_user, OWNER, ANN, BOB, person("far", "Far Away", "f@b.edu", TENANT_B))
    return as_user


@pytest.mark.parametrize("plural,kind,body", KINDS)
def test_share_reshare_and_unshare(world, plural, kind, body):
    c = world(OWNER)
    item = make(c, plural, body)
    first = share(c, plural, item, ["ann"])
    assert first.status_code == 200
    got = first.json()
    assert re.fullmatch(r"sh_[0-9a-f]{16}", got["share_id"])
    assert got["recipients"] == [{"sub": "ann", "name": "Ann Lee"}] and got["tenant"] is False

    again = share(c, plural, item, ["ann", "bob"], tenant=True)
    assert again.json()["share_id"] == got["share_id"]
    assert [r["sub"] for r in again.json()["recipients"]] == ["ann", "bob"] and again.json()["tenant"] is True
    assert c.get(f"/api/v1/me/{plural}/{item}/share", headers=AUTH).json() == again.json()
    stored = store.get_share(T, "owner", kind, item)
    assert stored["share_id"] == got["share_id"] and stored["shared_at"] <= stored["updated_at"]
    pointer = store.get_share_pointer(T, got["share_id"])
    assert {k: pointer[k] for k in ("owner_sub", "item_kind", "item_id")} == {
        "owner_sub": "owner", "item_kind": kind, "item_id": item}

    gone = share(c, plural, item, method="put")
    assert gone.status_code == 200 and gone.json()["recipients"] == [] and gone.json()["tenant"] is False
    assert store.get_share(T, "owner", kind, item) is None
    assert store.get_share_pointer(T, got["share_id"]) is None


@pytest.mark.parametrize("plural,kind,body", KINDS)
def test_delete_unshares_and_get_without_share_is_empty(world, plural, kind, body):
    c = world(OWNER)
    item = make(c, plural, body)
    assert c.get(f"/api/v1/me/{plural}/{item}/share", headers=AUTH).json() == {
        "share_id": None, "recipients": [], "tenant": False}
    sid = share(c, plural, item, ["ann"]).json()["share_id"]
    assert c.delete(f"/api/v1/me/{plural}/{item}/share", headers=AUTH).status_code == 200
    assert store.get_share(T, "owner", kind, item) is None and store.get_share_pointer(T, sid) is None
    assert c.delete(f"/api/v1/me/{plural}/{item}/share", headers=AUTH).status_code == 200


@pytest.mark.parametrize("plural,kind,body", KINDS)
def test_reshare_after_unshare_gets_a_new_id(world, plural, kind, body):
    c = world(OWNER)
    item = make(c, plural, body)
    first = share(c, plural, item, ["ann"]).json()["share_id"]
    share(c, plural, item, method="delete")
    assert share(c, plural, item, ["ann"]).json()["share_id"] != first


@pytest.mark.parametrize("plural,kind,body", KINDS)
@pytest.mark.parametrize("recipients,fragment", [
    (["far"], "directory"), (["owner"], "yourself"), (["nobody"], "directory"),
    (["ann", "ann"], "twice"), ([5], "directory"), ("ann", "list"),
    ([f"u{i}" for i in range(201)], "200"),
])
def test_bad_recipients_are_400(world, plural, kind, body, recipients, fragment):
    c = world(OWNER)
    item = make(c, plural, body)
    r = c.put(f"/api/v1/me/{plural}/{item}/share", headers=AUTH, json={"recipients": recipients, "tenant": False})
    assert r.status_code == 400 and fragment in str(r.json()["detail"])
    assert store.get_share(T, "owner", kind, item) is None


@pytest.mark.parametrize("payload", [{}, {"recipients": []}, {"recipients": [], "tenant": "yes"}])
def test_malformed_body_is_400(world, payload):
    c = world(OWNER)
    item = make(c, "queries", Q)
    assert c.put(f"/api/v1/me/queries/{item}/share", headers=AUTH, json=payload).status_code == 400


def test_built_in_card_is_refused_and_unknown_ids_404(world):
    c = world(OWNER)
    assert share(c, "cards", "average-grade", ["ann"]).status_code == 400
    assert share(c, "cards", "c_000000000000", ["ann"]).status_code == 404
    assert share(c, "queries", "q_000000000000", ["ann"]).status_code == 404
    assert share(c, "queries", "nope", ["ann"]).status_code == 404
    assert c.get("/api/v1/me/queries/q_000000000000/share", headers=AUTH).status_code == 404


def test_another_users_item_is_404(world):
    item = make(world(OWNER), "queries", Q)
    assert share(world(ANN), "queries", item, ["bob"]).status_code == 404
    assert world(ANN).get(f"/api/v1/me/queries/{item}/share", headers=AUTH).status_code == 404
    assert store.get_share(T, "ann", "query", item) is None


@pytest.mark.parametrize("plural,kind,body", KINDS)
def test_deleting_the_item_deletes_share_and_pointer(world, plural, kind, body):
    c = world(OWNER)
    item = make(c, plural, body)
    sid = share(c, plural, item, ["ann"]).json()["share_id"]
    assert c.delete(f"/api/v1/me/{plural}/{item}", headers=AUTH).status_code == 200
    assert store.get_share(T, "owner", kind, item) is None and store.get_share_pointer(T, sid) is None


def test_lists_carry_shared_summary(world):
    c = world(OWNER)
    shared, plain = make(c, "queries", Q), make(c, "queries", Q)
    share(c, "queries", shared, ["ann", "bob"], tenant=True)
    by_id = {q["id"]: q for q in c.get("/api/v1/me/queries", headers=AUTH).json()["queries"]}
    assert by_id[shared]["shared"] == {"tenant": True, "count": 2}
    assert by_id[plain]["shared"] == {"tenant": False, "count": 0}

    card = make(c, "cards", NEW)
    share(c, "cards", card, ["ann"])
    cards = {x["id"]: x for x in c.get("/api/v1/me/cards", headers=AUTH).json()["cards"]}
    assert cards[card]["shared"] == {"tenant": False, "count": 1}
    assert "shared" not in cards["average-grade"]


def test_share_ids_and_keys_are_built_by_the_store(world):
    store.put_share(T, "owner", "query", "q_a", {"recipients": [], "tenant": True})
    keys = sorted(i["item_key"] for i in store._get_table().scan()["Items"] if i["item_key"].startswith("share"))
    sid = store.get_share(T, "owner", "query", "q_a")["share_id"]
    assert keys == [f"share#owner#query#q_a", f"shareid#{sid}"]
    for bad in (lambda: store.get_share(T, "o#x", "query", "q_a"),
                lambda: store.get_share(T, "owner", "prefs", "q_a"),
                lambda: store.get_share(T, "owner", "query", "a#b"),
                lambda: store.put_share(T, "owner", "layout", "x", {})):
        with pytest.raises(ValueError):
            bad()
    for bad_id in ("sh_xyz", "sh_" + "A" * 16, "x#y", "sh_" + "a" * 17):
        assert store.get_share_pointer(T, bad_id) is None


def test_put_share_conflicts_on_a_stale_version(world):
    first = store.put_share(T, "owner", "card", "c_a", {"recipients": [], "tenant": True})
    store.update_share(T, "owner", "card", "c_a", {"recipients": ["x"]}, first["version"])
    with pytest.raises(store.Conflict):
        store.update_share(T, "owner", "card", "c_a", {"recipients": ["y"]}, first["version"])
    with pytest.raises(store.Conflict):
        store.put_share(T, "owner", "card", "c_a", {"recipients": []})


def test_pair_is_written_in_one_transaction(world, monkeypatch):
    calls = []
    real = store._get_table().meta.client.transact_write_items
    monkeypatch.setattr(store._get_table().meta.client, "transact_write_items",
                        lambda **kw: (calls.append(kw), real(**kw))[1])
    store.put_share(T, "owner", "query", "q_a", {"recipients": [], "tenant": True})
    assert len(calls) == 1 and len(calls[0]["TransactItems"]) == 2


def test_directory_search_strips_and_caps_q_at_100_characters(world, as_user):
    seed(as_user, person("long", "a" * 100, "l@a.edu"))
    c = world(OWNER)

    def found(q):
        return [u["sub"] for u in c.get("/api/v1/directory", headers=AUTH, params={"q": q}).json()["users"]]

    assert found("  ann  ") == ["ann"]
    assert found("a" * 100 + "b") == ["long"]
