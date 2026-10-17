import boto3
import pytest
from moto import mock_aws

import user_items_store as store


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        t = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="user-items-test",
            KeySchema=[{"AttributeName": "tenant_id", "KeyType": "HASH"}, {"AttributeName": "item_key", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "tenant_id", "AttributeType": "S"}, {"AttributeName": "item_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(store, "_table", t)
        yield t


@pytest.mark.parametrize("kind", ["card", "query"])
def test_keyed_kinds_round_trip(table, kind):
    saved = store.put("t1", "u1", kind, "c1", {"name": "Mine", "limit": 5, "ratio": 0.5})
    assert saved["id"] == "c1" and saved["version"] == 1 and saved["updated_at"]
    got = store.get("t1", "u1", kind, "c1")
    assert got == saved
    assert got["limit"] == 5 and type(got["limit"]) is int and type(got["ratio"]) is float
    assert store.get("t1", "u1", kind, "other") is None
    assert [i["id"] for i in store.list_kind("t1", "u1", kind)] == ["c1"]
    assert store.count("t1", "u1", kind) == 1
    assert store.delete("t1", "u1", kind, "c1") is True
    assert store.delete("t1", "u1", kind, "c1") is False
    assert store.get("t1", "u1", kind, "c1") is None


@pytest.mark.parametrize("kind", ["layout", "prefs", "imported"])
def test_singleton_kinds_round_trip(table, kind):
    saved = store.put("t1", "u1", kind, None, {"widgets": ["a", "b"], "nested": {"n": 3}})
    assert "id" not in saved and saved["version"] == 1
    got = store.get("t1", "u1", kind)
    assert got == saved and type(got["nested"]["n"]) is int
    assert [i["version"] for i in store.list_kind("t1", "u1", kind)] == [1]
    assert store.count("t1", "u1", kind) == 1
    assert store.delete("t1", "u1", kind) is True
    assert store.get("t1", "u1", kind) is None


def test_kinds_do_not_see_each_other(table):
    store.put("t1", "u1", "card", "x", {})
    store.put("t1", "u1", "query", "x", {})
    store.put("t1", "u1", "prefs", None, {})
    assert store.count("t1", "u1", "card") == 1
    assert store.count("t1", "u1", "query") == 1
    assert store.count("t1", "u1", "layout") == 0


def test_the_same_sub_in_two_tenants_is_isolated(table):
    store.put("t1", "u1", "card", "c1", {"name": "t1 card"})
    store.put("t2", "u1", "card", "c1", {"name": "t2 card"})
    assert store.get("t1", "u1", "card", "c1")["name"] == "t1 card"
    assert [i["name"] for i in store.list_kind("t2", "u1", "card")] == ["t2 card"]
    store.delete("t1", "u1", "card", "c1")
    assert store.get("t2", "u1", "card", "c1")["name"] == "t2 card"


def test_a_sub_prefix_never_matches_a_longer_sub(table):
    store.put("t1", "u10", "card", "c1", {})
    store.put("t1", "u1", "card", "c2", {})
    assert [i["id"] for i in store.list_kind("t1", "u1", "card")] == ["c2"]
    assert store.count("t1", "u1", "card") == 1


def test_a_singleton_kind_does_not_match_a_longer_kind_name(table):
    store.put("t1", "u1", "prefs", None, {})
    assert store.list_kind("t1", "u1", "layout") == []


@pytest.mark.parametrize("bad_id", ["a#b", "", "a b", "x" * 65, "a/b"])
def test_bad_ids_are_refused(table, bad_id):
    with pytest.raises(ValueError):
        store.put("t1", "u1", "card", bad_id, {})
    with pytest.raises(ValueError):
        store.get("t1", "u1", "card", bad_id)


def test_a_sub_with_hash_is_refused(table):
    with pytest.raises(ValueError):
        store.get("t1", "u#1", "card", "c1")
    with pytest.raises(ValueError):
        store.list_kind("t1", "", "card")


def test_unknown_kind_and_id_misuse_are_refused(table):
    with pytest.raises(ValueError):
        store.put("t1", "u1", "bogus", "c1", {})
    with pytest.raises(ValueError):
        store.put("t1", "u1", "card", None, {})
    with pytest.raises(ValueError):
        store.put("t1", "u1", "layout", "c1", {})


def test_create_only_put_conflicts_when_the_item_exists(table):
    store.put("t1", "u1", "card", "c1", {"name": "a"})
    with pytest.raises(store.Conflict):
        store.put("t1", "u1", "card", "c1", {"name": "b"})
    assert store.get("t1", "u1", "card", "c1")["name"] == "a"


def test_update_bumps_the_version_and_a_stale_version_conflicts(table):
    store.put("t1", "u1", "layout", None, {"w": 1})
    second = store.put("t1", "u1", "layout", None, {"w": 2}, expected_version=1)
    assert second["version"] == 2
    with pytest.raises(store.Conflict):
        store.put("t1", "u1", "layout", None, {"w": 3}, expected_version=1)
    assert store.get("t1", "u1", "layout")["w"] == 2


def test_an_update_of_a_missing_item_conflicts(table):
    with pytest.raises(store.Conflict):
        store.put("t1", "u1", "layout", None, {}, expected_version=1)


def test_list_and_count_paginate(table):
    big = "x" * 100_000
    for n in range(15):
        store.put("t1", "u1", "card", f"c{n:02d}", {"pad": big})
    assert len(store.list_kind("t1", "u1", "card")) == 15
    assert store.count("t1", "u1", "card") == 15


def test_the_pagination_fixture_really_spans_pages(table):
    big = "x" * 100_000
    for n in range(15):
        store.put("t1", "u1", "card", f"c{n:02d}", {"pad": big})
    first = table.query(KeyConditionExpression=boto3.dynamodb.conditions.Key("tenant_id").eq("t1"), ConsistentRead=True)
    assert "LastEvaluatedKey" in first


@pytest.mark.parametrize("tenant", ["", None])
@pytest.mark.parametrize("call", [
    lambda t: store.get(t, "u1", "card", "c1"),
    lambda t: store.put(t, "u1", "card", "c1", {}),
    lambda t: store.delete(t, "u1", "prefs"),
    lambda t: store.list_kind(t, "u1", "query"),
    lambda t: store.count(t, "u1", "card"),
])
def test_an_empty_tenant_is_refused(table, tenant, call):
    with pytest.raises(ValueError):
        call(tenant)
