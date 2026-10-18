import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

import lambda_handler
import mcp_access
import snowflake_client
from tests.test_mcp_audience import AUTH, FakeSessions, mcp_user, world  # noqa: F401

CONTRACT = {"metrics": ["metric.reportable_courses.v1"], "dimensions": ["term_name"]}
FREEHAND = "SELECT COUNT(*) AS n FROM CDM_LMS.COURSE"
REASON = "Counting courses the semantic layer has no metric for"
GOVERNED_FIRST = ("Try a governed tool first (run_query or compile_query); raw SQL is for what the semantic layer "
                  "can't answer.")


def _conditional_failure():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")


class FakeSessionTable(FakeSessions):
    """Rate counters as in FakeSessions, plus the session item operations with their conditions."""

    def __init__(self):
        super().__init__()
        self.deleted = []

    def get_item(self, Key, ConsistentRead=False):
        assert ConsistentRead
        item = self.items.get(Key["pk"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, ConditionExpression, ExpressionAttributeValues):
        assert ConditionExpression == ("attribute_not_exists(pk) OR attribute_not_exists(expires_at) "
                                       "OR expires_at <= :now")
        current = self.items.get(Item["pk"])
        if current and "expires_at" in current and current["expires_at"] > ExpressionAttributeValues[":now"]:
            raise _conditional_failure()
        self.items[Item["pk"]] = dict(Item)

    def delete_item(self, Key):
        self.deleted.append(Key["pk"])
        self.items.pop(Key["pk"], None)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, **kw):
        if not Key["pk"].startswith("session#"):
            return super().update_item(Key, UpdateExpression, ExpressionAttributeValues, **kw)
        assert UpdateExpression == "SET governed = :g"
        assert kw["ConditionExpression"] == "#t = :t AND #s = :s"
        item = self.items.get(Key["pk"])
        if not item or (item["tenant_id"], item["sub"]) != (ExpressionAttributeValues[":t"], ExpressionAttributeValues[":s"]):
            raise _conditional_failure()
        item["governed"] = ExpressionAttributeValues[":g"]


@pytest.fixture
def w(world, monkeypatch):
    world.sessions = FakeSessionTable()
    monkeypatch.setattr(mcp_access, "_sessions_table", world.sessions)
    world.tenants.rows["tc"] = {"tenant_id": "tc", "name": "tc", "license": "premium", "mcp_enabled": True}
    world.executed = []

    def query_sql(sql, params=None, **k):
        world.executed.append(sql)
        return {"columns": ["TERM_NAME", "REPORTABLE_COURSES"], "rows": [{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3}]}
    monkeypatch.setattr(snowflake_client, "query_sql", query_sql)
    world.as_user(mcp_user("tb"))
    return world


def H(session="s-1"):
    return {**AUTH, "X-Mcp-Session": session}


def sql(w, session="s-1", reason=REASON):
    body = {"sql": FREEHAND} if reason is None else {"sql": FREEHAND, "reason": reason}
    return w.client.post("/api/v1/sql/run", headers=H(session), json=body)


def governed(w, session="s-1"):
    return w.client.post("/api/v1/semantic/query", headers=H(session), json=CONTRACT)


def item(w, session="s-1"):
    return w.sessions.items.get(f"session#{session}")


# --- governed first ---------------------------------------------------------------------------------------------

def test_sql_is_refused_before_a_governed_call_and_allowed_after(w):
    r = sql(w)
    assert (r.status_code, r.json()) == (400, {"detail": GOVERNED_FIRST}) and w.executed == []
    assert governed(w).status_code == 200
    r = sql(w)
    assert r.status_code == 200 and r.json()["provenance"]["reason"] == REASON
    assert w.executed[-1] == FREEHAND


def test_governed_first_holds_per_session(w):
    assert governed(w, "s-1").status_code == 200
    assert sql(w, "s-2").json() == {"detail": GOVERNED_FIRST}
    assert sql(w, "s-1").status_code == 200


def test_the_session_item_records_owner_and_a_day_of_life(w):
    governed(w)
    assert item(w) == {"pk": "session#s-1", "tenant_id": "tb", "sub": "u1", "governed": True,
                       "expires_at": int(w.clock.wall) + 24 * 3600}


def test_a_failed_governed_call_does_not_mark_the_session(w):
    r = w.client.post("/api/v1/semantic/query", headers=H(), json={"metrics": ["metric.nope.v1"]})
    assert r.status_code == 400 and item(w)["governed"] is False
    assert sql(w).json() == {"detail": GOVERNED_FIRST}


def test_a_warehouse_failure_on_a_governed_call_does_not_mark_the_session(w, monkeypatch):
    def boom(sql, params=None, **k):
        raise RuntimeError("warehouse suspended")
    monkeypatch.setattr(snowflake_client, "query_sql", boom)
    assert governed(w).status_code == 502 and item(w)["governed"] is False


def test_compile_is_a_governed_call(w):
    assert w.client.post("/api/v1/semantic/compile", headers=H(), json=CONTRACT).status_code == 200
    assert item(w)["governed"] is True and w.executed == []


def test_a_report_run_is_a_governed_call(w, monkeypatch):
    from pathlib import Path
    from semantic_layer.reports import load_reports
    monkeypatch.setattr(lambda_handler, "_reports", lambda: load_reports(Path(__file__).parent / "fixtures" / "reports"))
    monkeypatch.setattr(lambda_handler, "_current_terms", lambda tenant: [
        {"term_name": "Q4: 2026", "term_start": "2026-04-01", "term_end": "2026-06-30"}])
    r = w.client.post("/api/v1/reports/report.sample.v1/run", headers=H(),
                      json={"visual": "sessions_by_day", "query": "main", "values": {"term": ["Q4: 2026"]}})
    assert r.status_code == 200 and "rows" in r.json() and item(w)["governed"] is True


def _saved(monkeypatch, row):
    monkeypatch.setattr(lambda_handler, "_query_item", lambda t, s, q: row)
    monkeypatch.setattr(lambda_handler, "_replace_query", lambda *a: row)


def test_a_governed_saved_query_run_is_a_governed_call(w, monkeypatch):
    _saved(monkeypatch, {"id": "q_1", "query_kind": "governed", "contract": CONTRACT})
    assert w.client.post("/api/v1/me/queries/q_1/run", headers=H()).status_code == 200
    assert item(w)["governed"] is True


SQL_ROW = {"id": "q_1", "query_kind": "sql", "sql": FREEHAND, "description": "Courses per term"}


def _sql_kind(monkeypatch, route):
    """Install an SQL-kind query behind route ('saved' or 'shared'); returns the path that runs it."""
    if route == "saved":
        _saved(monkeypatch, SQL_ROW)
        return "/api/v1/me/queries/q_1/run"
    monkeypatch.setattr(lambda_handler, "_shared_item", lambda t, s, i: ({"item_kind": "query"}, SQL_ROW, "Owner"))
    return "/api/v1/shared/sh_1/run"


@pytest.mark.parametrize("route", ["saved", "shared"])
def test_an_sql_kind_run_is_refused_before_a_governed_call(w, monkeypatch, route):
    r = w.client.post(_sql_kind(monkeypatch, route), headers=H())
    assert (r.status_code, r.json()) == (400, {"detail": GOVERNED_FIRST})
    assert w.executed == [] and item(w)["governed"] is False


@pytest.mark.parametrize("route", ["saved", "shared"])
def test_an_sql_kind_run_is_allowed_after_a_governed_call_and_marks_nothing(w, monkeypatch, route):
    path = _sql_kind(monkeypatch, route)
    governed(w)
    w.sessions.items["session#s-1"]["governed"] = True
    r = w.client.post(path, headers=H())
    assert r.status_code == 200 and w.executed[-1] == FREEHAND


@pytest.mark.parametrize("route", ["saved", "shared"])
def test_an_sql_kind_run_needs_the_session_header(w, monkeypatch, route):
    r = w.client.post(_sql_kind(monkeypatch, route), headers=AUTH)
    assert (r.status_code, r.json()) == (400, {"detail": mcp_access.SESSION_REQUIRED}) and w.executed == []


@pytest.mark.parametrize("route", ["saved", "shared"])
def test_a_web_app_token_runs_an_sql_kind_query_without_a_session(w, monkeypatch, route):
    w.as_user({"sub": "u1", "custom:tenant_id": "tb", "cognito:groups": []})
    assert w.client.post(_sql_kind(monkeypatch, route), headers=AUTH).status_code == 200
    assert w.executed == [FREEHAND]


@pytest.mark.parametrize("kind,row", [
    ("card", {"id": "c_1", "contract": CONTRACT}),
    ("query", {"id": "q_1", "query_kind": "governed", "contract": CONTRACT}),
])
def test_a_shared_governed_run_is_a_governed_call(w, monkeypatch, kind, row):
    monkeypatch.setattr(lambda_handler, "_shared_item", lambda t, s, i: ({"item_kind": kind}, row, "Owner"))
    assert w.client.post("/api/v1/shared/sh_1/run", headers=H()).status_code == 200
    assert item(w)["governed"] is True


def test_a_report_run_that_ran_nothing_does_not_mark_the_session(w, monkeypatch):
    from pathlib import Path
    from semantic_layer.reports import load_reports
    monkeypatch.setattr(lambda_handler, "_reports", lambda: load_reports(Path(__file__).parent / "fixtures" / "reports"))
    monkeypatch.setattr(lambda_handler, "_current_terms", lambda tenant: [])
    r = w.client.post("/api/v1/reports/report.sample.v1/run", headers=H(),
                      json={"visual": "roster", "query": "main", "values": {}})
    assert r.json() == {"unavailable": "Requires Author access"} and item(w)["governed"] is False


def test_a_failed_mark_still_returns_the_governed_result(w, monkeypatch):
    def throttled(*a):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem")
    monkeypatch.setattr(mcp_access, "mark_governed", throttled)
    assert governed(w).status_code == 200 and item(w)["governed"] is False


def test_a_governed_call_without_a_session_header_works_and_writes_nothing(w):
    r = w.client.post("/api/v1/semantic/query", headers=AUTH, json=CONTRACT)
    assert r.status_code == 200 and w.sessions.items.keys() == {"rate#tb#u1#1000"}


# --- reason and header -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("reason", [None, "", "too short", "  " + "x" * 19 + "   "])
def test_a_short_or_missing_reason_is_refused(w, reason):
    governed(w)
    r = sql(w, reason=reason)
    assert (r.status_code, r.json()) == (400, {"detail": mcp_access.REASON_REFUSAL}) and w.executed[-1] != FREEHAND


def test_a_twenty_character_reason_is_enough(w):
    governed(w)
    assert sql(w, reason="  " + "x" * 20 + " ").status_code == 200


def test_sql_needs_the_session_header(w):
    r = w.client.post("/api/v1/sql/run", headers=AUTH, json={"sql": FREEHAND, "reason": REASON})
    assert (r.status_code, r.json()) == (400, {"detail": mcp_access.SESSION_REQUIRED}) and w.executed == []


@pytest.mark.parametrize("bad", ["", "a b", "a#b", "x" * 129, "s/1", "a.b"])
def test_a_malformed_session_header_is_a_400_on_any_route(w, bad):
    for method, path in [("GET", "/api/v1/me"), ("POST", "/api/v1/sql/run")]:
        r = w.client.request(method, path, headers=H(bad), json={"sql": FREEHAND} if method == "POST" else None)
        assert (r.status_code, r.json()) == (400, {"detail": mcp_access.SESSION_HEADER_REFUSAL})
    assert not any(k.startswith("session#") for k in w.sessions.items)


def test_the_longest_session_id_is_accepted(w):
    assert w.client.get("/api/v1/me", headers=H("aZ0_-" + "x" * 123)).status_code == 200


def test_validate_and_describe_are_not_gated(w, monkeypatch):
    r = w.client.post("/api/v1/sql/validate", headers=H(), json={"sql": FREEHAND})
    assert r.status_code == 200
    r = w.client.post("/api/v1/sql/validate", headers=AUTH, json={"sql": FREEHAND})
    assert r.status_code == 200
    assert w.client.get("/api/v1/schema/describe?schema=CDM_LMS&table=COURSE", headers=AUTH).status_code == 200


# --- tenant guard ------------------------------------------------------------------------------------------------

def test_a_session_id_from_another_tenant_is_refused_and_deleted(w):
    governed(w)
    w.as_user(mcp_user("tc"))
    r = w.client.get("/api/v1/me", headers=H())
    assert (r.status_code, r.json()) == (403, {"detail": mcp_access.SESSION_REFUSAL})
    assert item(w) is None and w.sessions.deleted == ["session#s-1"]


def test_a_session_id_from_another_user_in_the_tenant_is_refused_on_sql(w):
    governed(w)
    w.as_user(mcp_user("tb", sub="u2"))
    r = sql(w)
    assert (r.status_code, r.json()) == (403, {"detail": mcp_access.SESSION_REFUSAL})
    assert item(w) is None and w.executed[-1] != FREEHAND


def test_after_a_refusal_the_owner_starts_again_ungoverned(w):
    governed(w)
    w.as_user(mcp_user("tb", sub="u2"))
    assert w.client.get("/api/v1/me", headers=H()).status_code == 403
    w.as_user(mcp_user("tb"))
    assert sql(w).json() == {"detail": GOVERNED_FIRST}


def test_web_app_tokens_ignore_the_header(w):
    w.as_user({"sub": "u1", "custom:tenant_id": "tb", "cognito:groups": []})
    assert w.client.get("/api/v1/me", headers=H("not a valid id")).status_code == 200
    r = w.client.post("/api/v1/sql/run", headers=H("x"), json={"sql": FREEHAND})
    assert r.status_code == 200 and w.sessions.items == {}


# --- mcp_access.session -------------------------------------------------------------------------------------------

@pytest.fixture
def table(monkeypatch):
    t = FakeSessionTable()
    monkeypatch.setattr(mcp_access, "_sessions_table", t)
    return t


def test_session_creates_once_and_returns_the_stored_item(table):
    first = mcp_access.session("s", "ta", "u1", now=100.0)
    table.items["session#s"]["governed"] = True
    again = mcp_access.session("s", "ta", "u1", now=200.0)
    assert first["governed"] is False and again["governed"] is True and again["expires_at"] == 100 + 24 * 3600


def test_an_expired_session_is_replaced_even_with_another_owner(table):
    mcp_access.session("s", "ta", "u1", now=100.0)
    mcp_access.mark_governed("s", "ta", "u1")
    fresh = mcp_access.session("s", "tb", "u9", now=100.0 + 24 * 3600)
    assert (fresh["tenant_id"], fresh["sub"], fresh["governed"]) == ("tb", "u9", False)


def test_a_lost_creation_race_rereads_and_compares(table, monkeypatch):
    put = table.put_item

    def racing_put(**kw):
        table.items["session#s"] = {"pk": "session#s", "tenant_id": "tb", "sub": "u2", "governed": False,
                                    "expires_at": 10_000}
        put(**kw)
    monkeypatch.setattr(table, "put_item", racing_put)
    with pytest.raises(HTTPException) as e:
        mcp_access.session("s", "ta", "u1", now=100.0)
    assert (e.value.status_code, e.value.detail) == (403, mcp_access.SESSION_REFUSAL)
    assert "session#s" not in table.items


def test_a_lost_race_to_the_same_owner_returns_their_session(table, monkeypatch):
    put = table.put_item

    def racing_put(**kw):
        table.items["session#s"] = {"pk": "session#s", "tenant_id": "ta", "sub": "u1", "governed": True,
                                    "expires_at": 10_000}
        put(**kw)
    monkeypatch.setattr(table, "put_item", racing_put)
    assert mcp_access.session("s", "ta", "u1", now=100.0)["governed"] is True


def test_a_session_that_keeps_losing_the_creation_race_is_a_409(table, monkeypatch):
    def always_lose(**kw):
        raise _conditional_failure()
    monkeypatch.setattr(table, "put_item", always_lose)
    with pytest.raises(HTTPException) as e:
        mcp_access.session("s", "ta", "u1", now=100.0)
    assert e.value.status_code == 409


def test_an_item_without_an_expiry_is_replaced(table):
    table.items["session#s"] = {"pk": "session#s", "tenant_id": "tb", "sub": "u2", "governed": True}
    assert mcp_access.session("s", "ta", "u1", now=100.0)["tenant_id"] == "ta"


def test_mark_governed_never_marks_a_session_that_changed_owner(table):
    mcp_access.session("s", "tb", "u2", now=100.0)
    mcp_access.mark_governed("s", "ta", "u1")
    mcp_access.mark_governed("gone", "ta", "u1")
    assert table.items["session#s"]["governed"] is False and "session#gone" not in table.items
