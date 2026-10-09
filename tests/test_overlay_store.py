import boto3
import pytest
from moto import mock_aws

import overlay_store
from semantic_layer.overlays import Overlay

TARGET = "measure:dataset.student_grade.v1:average_grade_percentage"


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        t = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="overlays-test",
            KeySchema=[{"AttributeName": "tenant_id", "KeyType": "HASH"}, {"AttributeName": "metric_id", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "tenant_id", "AttributeType": "S"}, {"AttributeName": "metric_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(overlay_store, "_table", t)
        yield t


def _ov(expr="ROUND(GRADE_PERCENTAGE, 0)"):
    return Overlay(target=TARGET, expr=expr, description="rounded")


def test_saving_numbers_versions_and_records_who(table):
    first = overlay_store.put_overlay("t1", _ov(), "alice", expected_version=0)
    second = overlay_store.put_overlay("t1", _ov("FLOOR(GRADE_PERCENTAGE)"), "bob", expected_version=1)
    assert (first.version, second.version) == (1, 2)
    current = overlay_store.get_overlay("t1", TARGET)
    assert current.expr == "FLOOR(GRADE_PERCENTAGE)" and current.updated_by == "bob" and current.updated_at


def test_a_stale_version_is_a_conflict_and_changes_nothing(table):
    overlay_store.put_overlay("t1", _ov(), "alice", expected_version=0)
    with pytest.raises(overlay_store.OverlayConflict):
        overlay_store.put_overlay("t1", _ov("FLOOR(GRADE_PERCENTAGE)"), "bob", expected_version=0)
    assert overlay_store.get_overlay("t1", TARGET).expr == "ROUND(GRADE_PERCENTAGE, 0)"


def test_history_lists_every_version_newest_first(table):
    overlay_store.put_overlay("t1", _ov("A"), "alice", expected_version=0)
    overlay_store.put_overlay("t1", _ov("B"), "alice", expected_version=1)
    assert [h.expr for h in overlay_store.history("t1", TARGET)] == ["B", "A"]


def test_revert_saves_an_earlier_version_as_the_newest(table):
    overlay_store.put_overlay("t1", _ov("A"), "alice", expected_version=0)
    overlay_store.put_overlay("t1", _ov("B"), "alice", expected_version=1)
    reverted = overlay_store.revert("t1", TARGET, version=1, updated_by="carol", expected_version=2)
    assert (reverted.expr, reverted.version, reverted.updated_by) == ("A", 3, "carol")
    assert [h.version for h in overlay_store.history("t1", TARGET)] == [3, 2, 1]


def test_reverting_to_a_missing_version_is_an_error(table):
    overlay_store.put_overlay("t1", _ov("A"), "alice", expected_version=0)
    with pytest.raises(KeyError):
        overlay_store.revert("t1", TARGET, version=9, updated_by="carol", expected_version=1)


def test_delete_keeps_history_and_the_next_save_continues_the_numbering(table):
    overlay_store.put_overlay("t1", _ov("A"), "alice", expected_version=0)
    overlay_store.delete_overlay("t1", TARGET, expected_version=1)
    assert overlay_store.get_overlay("t1", TARGET) is None
    again = overlay_store.put_overlay("t1", _ov("B"), "alice", expected_version=0)
    assert again.version == 2 and len(overlay_store.history("t1", TARGET)) == 2


def test_list_returns_current_overlays_only_for_that_tenant(table):
    overlay_store.put_overlay("t1", _ov("A"), "alice", expected_version=0)
    overlay_store.put_overlay("t1", _ov("B"), "alice", expected_version=1)
    overlay_store.put_overlay("t2", _ov("C"), "zed", expected_version=0)
    table.put_item(Item={"tenant_id": "t1", "metric_id": "metric.dashboard.legacy.v1", "measure_sql": "SELECT 1"})
    assert [(o.target, o.expr) for o in overlay_store.list_overlays("t1")] == [(TARGET, "B")]


def test_history_reads_are_consistent(table, monkeypatch):
    calls = []
    real = table.query
    monkeypatch.setattr(table, "query", lambda **kw: calls.append(kw) or real(**kw))
    overlay_store.history("t1", TARGET)
    assert calls and all(c.get("ConsistentRead") for c in calls)


def test_an_existing_history_version_is_never_overwritten(table):
    table.put_item(Item={"tenant_id": "t1", "metric_id": f"{TARGET}#v000001", "target": TARGET, "expr": "OLD", "version": 1})
    overlay_store.put_overlay("t1", _ov("NEW"), "alice", expected_version=0)
    assert [(h.version, h.expr) for h in overlay_store.history("t1", TARGET)] == [(2, "NEW"), (1, "OLD")]
