import logging

import pytest

import lambda_handler
import user_items_store as store
from tests.test_me_cards import AUTH, as_user  # noqa: F401
from tests.tenant_fakes import TENANT_A, TENANT_B, user_of


def person(sub, name, email, tenant=TENANT_A):
    return {**user_of(tenant, sub=sub), "name": name, "email": email}


def seed(as_user, *people):
    for p in people:
        as_user(p).get("/api/v1/me", headers=AUTH)


def search(as_user, caller, q=None):
    r = as_user(caller).get("/api/v1/directory", headers=AUTH, params={"q": q} if q is not None else {})
    assert r.status_code == 200
    return r.json()["users"]


ME = person("me", "Me Myself", "me@a.edu")


def test_me_upserts_the_callers_entry(as_user):
    seed(as_user, ME)
    entry = store.get(TENANT_A.tenant_id, "me", "directory")
    assert (entry["sub"], entry["name"], entry["email"]) == ("me", "Me Myself", "me@a.edu")


def test_me_writes_only_when_the_entry_differs(as_user, monkeypatch):
    seed(as_user, ME)
    writes = []
    monkeypatch.setattr(store, "put", lambda *a, **k: writes.append(a))
    seed(as_user, ME)
    assert writes == []
    seed(as_user, {**ME, "name": "New Name"})
    assert len(writes) == 1


def test_name_falls_back_to_the_email_local_part(as_user):
    seed(as_user, {**ME, "name": None})
    assert store.get(TENANT_A.tenant_id, "me", "directory")["name"] == "me"


def test_a_store_failure_does_not_break_me(as_user, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(store, "get", boom)
    with caplog.at_level(logging.WARNING):
        r = as_user(ME).get("/api/v1/me", headers=AUTH)
    assert r.status_code == 200 and "directory" in caplog.text.lower()


def test_search_matches_name_or_email_case_insensitively_sorted_by_name(as_user):
    seed(as_user, person("u2", "Zed Ames", "zed@a.edu"), person("u3", "ann lee", "al@a.edu"),
         person("u4", "Bob", "ANN.b@a.edu"), ME)
    assert [u["sub"] for u in search(as_user, ME, "ANN")] == ["u3", "u4"]
    assert [u["sub"] for u in search(as_user, ME)] == ["u3", "u4", "u2"]
    assert set(search(as_user, ME, "zed")[0]) == {"sub", "name", "email"}


def test_search_excludes_the_caller_and_caps_at_25(as_user):
    seed(as_user, ME, *[person(f"u{i:02d}", f"User {i:02d}", f"u{i}@a.edu") for i in range(30)])
    users = search(as_user, ME, "")
    assert len(users) == 25 and "me" not in {u["sub"] for u in users}
    assert [u["name"] for u in users] == sorted(u["name"] for u in users)


def test_directory_is_tenant_scoped(as_user):
    seed(as_user, ME, person("b1", "Beta Person", "b@b.edu", TENANT_B))
    assert search(as_user, ME) == []


def test_list_directory_paginates_and_ignores_other_kinds(as_user):
    for i in range(120):
        store.put(TENANT_A.tenant_id, f"p{i}", "directory", None, {"sub": f"p{i}", "name": "n" * 3000, "email": "e"})
    store.put(TENANT_A.tenant_id, "p0", "prefs", None, {"x": 1})
    assert len(store.list_directory(TENANT_A.tenant_id)) == 120


def test_directory_key_is_not_under_a_user_prefix(as_user):
    store.put("t1", "u1", "directory", None, {"sub": "u1", "name": "n", "email": "e"})
    keys = [i["item_key"] for i in store._get_table().scan()["Items"]]
    assert keys == ["directory#u1"]
    with pytest.raises(ValueError):
        store.put("t1", "u#1", "directory", None, {})
    with pytest.raises(ValueError):
        store.put("t1", "u1", "directory", "x", {})
