import pytest

import snowflake_client
from snowflake_client import Freehand
from tests.guard_fakes import guard_ctx


class WideCursor:
    """Serves `rows` rows of one value `width` characters long, in whatever batches are asked for."""

    def __init__(self, rows, width):
        self.left, self.value, self.description, self.fetched = rows, "x" * width, [("V",)], 0

    def execute(self, sql, params=None):
        pass

    def fetchmany(self, n):
        batch = min(n, self.left)
        self.left -= batch
        self.fetched += batch
        return [(self.value,)] * batch

    def close(self):
        pass


class Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def test_a_result_over_the_byte_cap_is_refused_without_rows():
    cursor = WideCursor(rows=1000, width=20_000)  # 20 MB
    with pytest.raises(snowflake_client.ResultTooLarge):
        snowflake_client._execute(Conn(cursor), "SELECT V FROM CDM_LMS.T", None)
    assert cursor.fetched < 1000


def test_a_result_under_the_byte_cap_is_returned_whole():
    out = snowflake_client._execute(Conn(WideCursor(rows=1000, width=9_000)), "SELECT V FROM CDM_LMS.T", None)
    assert len(out["rows"]) == 1000 and out["truncated"] is False


def test_rows_past_the_row_cap_still_mark_truncated():
    out = snowflake_client._execute(Conn(WideCursor(rows=1500, width=10)), "SELECT V FROM CDM_LMS.T", None)
    assert len(out["rows"]) == snowflake_client.MAX_ROWS and out["truncated"] is True


def test_the_guard_reports_a_too_large_result_as_a_refusal_not_a_warehouse_error(monkeypatch):
    def too_large(sql, params=None, **k):
        raise snowflake_client.ResultTooLarge()

    monkeypatch.setattr(snowflake_client, "query_sql", too_large)
    ctx = guard_ctx(allow_identity=True)
    checked = snowflake_client.validate("SELECT N FROM CDM_LMS.T", ctx, Freehand())
    result = snowflake_client.execute(checked, ctx)
    assert result == snowflake_client.Result(columns=[], rows=[], error=snowflake_client.TOO_LARGE, refused=True)
    assert snowflake_client.TOO_LARGE == "The result is too large; select fewer columns or rows."


def test_sql_run_returns_a_too_large_result_as_a_400(monkeypatch):
    from semantic_layer.sql_service import run_sql

    def too_large(v, ctx):
        return snowflake_client.Result(columns=[], rows=[], error=snowflake_client.TOO_LARGE, refused=True)

    out = run_sql(guard_ctx(allow_identity=True), sql="SELECT N FROM CDM_LMS.T", governed=None, reason="r", execute=too_large)
    assert out == {"error": snowflake_client.TOO_LARGE}


def test_a_statement_timeout_is_an_ordinary_masked_warehouse_error(monkeypatch):
    from snowflake.connector.errors import ProgrammingError

    def timed_out(sql, params=None, **k):
        raise ProgrammingError(msg="Statement reached its statement or warehouse timeout of 120 second(s) and was canceled.",
                               errno=630, sqlstate="57014")

    monkeypatch.setattr(snowflake_client, "query_sql", timed_out)
    ctx = guard_ctx()
    result = snowflake_client.execute(snowflake_client.validate("SELECT COUNT(*) AS N FROM CDM_LMS.PERSON", ctx, Freehand()), ctx)
    assert result == snowflake_client.Result(columns=[], rows=[],
                                             error="The warehouse could not run this query (error 630).")
