import json
import types

import pytest
import snowflake.connector
from botocore.exceptions import ClientError

import snowflake_client
from tenancy import Tenant, TenantNotReady

T1 = Tenant("T1", "One", "full", True)
T2 = Tenant("T2", "Two", "full", True)


class FakeConn:
    def __init__(self, **kw):
        self.kw, self.closed = kw, False

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def clean():
    snowflake_client._connections.clear()
    snowflake_client._credentials.clear()
    yield
    snowflake_client._connections.clear()
    snowflake_client._credentials.clear()


@pytest.fixture
def aws(monkeypatch):
    """Fake Secrets Manager and snowflake connector; records secret ids read and connections opened."""
    state = types.SimpleNamespace(secret_ids=[], connects=[], secrets={})

    class SM:
        def get_secret_value(self, SecretId):
            state.secret_ids.append(SecretId)
            if SecretId not in state.secrets:
                raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "x"}}, "GetSecretValue")
            return {"SecretString": json.dumps(state.secrets[SecretId])}

    monkeypatch.setattr(snowflake_client.boto3, "client", lambda *a, **k: SM())

    def connect(**kw):
        conn = FakeConn(**kw)
        state.connects.append(conn)
        return conn

    monkeypatch.setattr(snowflake.connector, "connect", connect)
    for t in ("T1", "T2"):
        state.secrets[f"illuminate/tenants/{t}/snowflake"] = {
            "account": "acct", "user": "u", "password": "p", "database": f"DB_{t}", "warehouse": "w", "role": "r"}
    return state


def test_credentials_read_from_tenant_secret(aws):
    snowflake_client.get_connection(T1)
    assert aws.secret_ids == ["illuminate/tenants/T1/snowflake"]


def test_connections_are_cached_per_tenant(aws):
    a = snowflake_client.get_connection(T1)
    b = snowflake_client.get_connection(T2)
    c = snowflake_client.get_connection(T1)
    assert len(aws.connects) == 2
    assert a is c and a is not b
    assert b.kw["database"] == "DB_T2"


def test_missing_secret_raises_tenant_not_ready(aws):
    del aws.secrets["illuminate/tenants/T1/snowflake"]
    with pytest.raises(TenantNotReady):
        snowflake_client.get_connection(T1)
    assert aws.secret_ids == ["illuminate/tenants/T1/snowflake"]
    assert aws.connects == []


def test_other_secret_errors_propagate(monkeypatch):
    class SM:
        def get_secret_value(self, SecretId):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "GetSecretValue")

    monkeypatch.setattr(snowflake_client.boto3, "client", lambda *a, **k: SM())
    with pytest.raises(ClientError):
        snowflake_client.tenant_database(T1)


def test_tenant_database_from_secret(aws):
    assert snowflake_client.tenant_database(T1) == "DB_T1"
    assert snowflake_client.tenant_database(T2) == "DB_T2"


def test_tenant_database_missing_raises_tenant_not_ready(aws):
    aws.secrets["illuminate/tenants/T1/snowflake"].pop("database")
    with pytest.raises(TenantNotReady):
        snowflake_client.tenant_database(T1)


def test_session_expiry_reconnects_same_tenant_only(aws, monkeypatch):
    from snowflake.connector.errors import ProgrammingError

    snowflake_client.get_connection(T1)
    t2_conn = snowflake_client.get_connection(T2)
    calls = []

    def execute(conn, sql, params):
        calls.append(conn)
        if len(calls) == 1:
            raise ProgrammingError(msg="expired", errno=390111)
        return {"columns": [], "rows": [], "truncated": False}

    monkeypatch.setattr(snowflake_client, "_execute", execute)
    snowflake_client.query_sql("SELECT 1", tenant=T1)
    assert calls[0].closed and not calls[1].closed
    assert calls[0] is not calls[1]
    assert not t2_conn.closed
    assert snowflake_client._connections["T2"] is t2_conn


def test_invalidate_affects_only_that_tenant(aws):
    c1 = snowflake_client.get_connection(T1)
    c2 = snowflake_client.get_connection(T2)
    snowflake_client.invalidate("T1")
    assert c1.closed and not c2.closed
    assert "T1" not in snowflake_client._connections and "T1" not in snowflake_client._credentials
    assert "T2" in snowflake_client._connections and "T2" in snowflake_client._credentials


def test_none_tenant_raises_tenant_not_ready(aws):
    with pytest.raises(TenantNotReady):
        snowflake_client.query_sql("SELECT 1")
    with pytest.raises(TenantNotReady):
        snowflake_client.query_preview("CDM_LMS", "T")
    with pytest.raises(TenantNotReady):
        snowflake_client.tenant_database(None)
    assert aws.secret_ids == [] and aws.connects == []


def test_validate_and_execute_propagates_none_tenant(aws):
    with pytest.raises(TenantNotReady):
        snowflake_client.validate_and_execute("SELECT 1 FROM CDM_LMS.T", tenant=None)
    assert aws.secret_ids == [] and aws.connects == []


def test_validate_and_execute_propagates_missing_secret(aws):
    del aws.secrets["illuminate/tenants/T1/snowflake"]
    with pytest.raises(TenantNotReady):
        snowflake_client.validate_and_execute("SELECT 1 FROM CDM_LMS.T", tenant=T1)
    assert aws.connects == []


def test_show_branch_propagates_tenant_not_ready(monkeypatch):
    def not_ready(*a, **k):
        raise TenantNotReady("T1")

    monkeypatch.setattr(snowflake_client, "query_sql", not_ready)
    with pytest.raises(TenantNotReady):
        snowflake_client.validate_and_execute("SHOW TABLES", tenant=None)


def test_secret_without_database_raises_before_connecting(aws):
    aws.secrets["illuminate/tenants/T1/snowflake"].pop("database")
    with pytest.raises(TenantNotReady):
        snowflake_client.get_connection(T1)
    assert aws.connects == []
