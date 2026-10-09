import pytest
from snowflake.connector.errors import ProgrammingError

import snowflake_client


class FakeCursor:
    def __init__(self, conn):
        self.conn, self.description = conn, [("N",)]

    def execute(self, sql, params=None):
        if self.conn.error:
            raise self.conn.error

    def fetchmany(self, n):
        return [(1,)]

    def close(self):
        pass


class FakeConnection:
    def __init__(self, error=None):
        self.error, self.closed = error, False

    def cursor(self):
        return FakeCursor(self)

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True


@pytest.fixture
def connections(monkeypatch):
    queue = []
    monkeypatch.setattr(snowflake_client, "_sf_connection", None)
    monkeypatch.setattr(snowflake_client, "get_connection", lambda: snowflake_client._sf_connection or _next(queue))

    def _next(q):
        snowflake_client._sf_connection = q.pop(0)
        return snowflake_client._sf_connection
    return queue


def test_an_expired_session_reconnects_and_retries_once(connections):
    expired = FakeConnection(ProgrammingError(msg="Session no longer exists.", errno=390111))
    connections.extend([expired, FakeConnection()])
    assert snowflake_client.query_sql("SELECT 1 AS N")["rows"] == [{"N": 1}]
    assert expired.closed


def test_other_warehouse_errors_are_not_retried(connections):
    broken = FakeConnection(ProgrammingError(msg="SQL compilation error", errno=1003))
    connections.extend([broken, FakeConnection()])
    with pytest.raises(ProgrammingError):
        snowflake_client.query_sql("SELECT 1 AS N")
    assert len(connections) == 1
