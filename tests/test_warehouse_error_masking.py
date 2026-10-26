import logging

import pytest
from fastapi.testclient import TestClient

import chat_engine
import lambda_handler
import snowflake_client
from semantic_layer.catalog import load_catalog
from semantic_layer.chat_tools import ChatTools
from tests.guard_fakes import guard
from tests.tenant_fakes import TENANT_A, install_tenants, user_of

# Passes the guard for a Viewer; the fake warehouse then fails with an error that echoes a value.
VIEWER_SQL = "SELECT COUNT(*) AS n FROM CDM_LMS.PERSON p WHERE p.FIRST_NAME = 'Alice'"
ECHOES = [
    "Numeric value 'x' Alice' is not recognized",
    "Numeric value x'Alice' is not recognized",
    "Numeric value N'Alice' is not recognized",
    "Numeric value ‘Alice’ is not recognized",
    "Numeric value “Alice” is not recognized",
    "Numeric value `Alice` is not recognized",
    'Error parsing JSON: unknown keyword "Alice", pos 6',
]


class SnowflakeError(Exception):
    def __init__(self, message, errno=100038, sqlstate="22018"):
        super().__init__(message)
        self.errno, self.sqlstate = errno, sqlstate


@pytest.fixture(params=ECHOES)
def warehouse_fails(request, monkeypatch):
    def fail(sql, params=None, **k):
        raise SnowflakeError(f"100038 (22018): 01b2c3d4: {request.param}")

    monkeypatch.setattr(snowflake_client, "query_sql", fail)
    monkeypatch.setattr(snowflake_client, "_run", fail)
    return request.param


def test_a_viewers_warehouse_error_is_only_the_code_and_the_log_never_has_the_value(warehouse_fails, caplog):
    caplog.set_level(logging.DEBUG)
    result = guard(VIEWER_SQL, tenant=TENANT_A)
    assert result == {"error": "The warehouse could not run this query (error 100038).", "warehouse_error": True}
    assert "Alice" not in caplog.text and "100038" in caplog.text


def test_an_admins_warehouse_error_keeps_the_text_before_the_first_quote(warehouse_fails, caplog):
    caplog.set_level(logging.DEBUG)
    result = guard(VIEWER_SQL, tenant=TENANT_A, allow_identity=True)
    assert "Alice" not in result["error"] and "Alice" not in caplog.text
    assert result["error"].startswith("100038 (22018): 01b2c3d4: ") and result["error"].endswith("…")


@pytest.mark.parametrize("raw, shown", [
    ("Numeric value 'x' Alice' is not recognized", "Numeric value …"),
    ("Division by zero", "Division by zero"),
    ("Can’t parse Alice", "Can…"),
] + [(f"value {q}Alice", "value …") for q in "«»‹›‚„′″＇＂"])
def test_warehouse_text_is_cut_at_the_first_quote_like_character(raw, shown):
    assert snowflake_client.before_first_quote(raw) == shown


def test_a_viewers_chat_tool_result_and_its_log_line_never_carry_the_value(warehouse_fails, caplog):
    caplog.set_level(logging.DEBUG)
    tools = ChatTools(load_catalog(), tenant=TENANT_A)
    use = {"toolUseId": "t1", "name": "execute_sql",
           "input": {"sql": VIEWER_SQL, "reason": "no governed metric parses first names"}}
    artifacts = []
    message = chat_engine._run_tool(use, artifacts, ["search_catalog"], tools)
    assert "error 100038" in str(message) and "Alice" not in str(message)
    assert artifacts == [] and "Alice" not in caplog.text


def test_a_semantic_query_502_and_its_log_line_never_carry_the_value(monkeypatch, warehouse_fails, caplog):
    caplog.set_level(logging.DEBUG)
    install_tenants(monkeypatch, TENANT_A, database="TESTDB")
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user_of(TENANT_A))
    r = TestClient(lambda_handler.app).post("/api/v1/semantic/query", headers={"Authorization": "Bearer t"},
                                            json={"metrics": ["metric.reportable_courses.v1"]})
    assert r.status_code == 502
    assert "Alice" not in r.text and "Alice" not in caplog.text


def test_a_preview_failure_is_raised_cut_at_the_first_quote(warehouse_fails):
    with pytest.raises(snowflake_client.WarehouseError) as raised:
        snowflake_client.query_preview("CDM_LMS", "PERSON", tenant=TENANT_A)
    assert "Alice" not in str(raised.value) and raised.value.__cause__ is None


@pytest.mark.parametrize("error", [RuntimeError("x 'Alice'"), SnowflakeError("x 'Alice'", errno=-1, sqlstate=None)])
def test_an_error_without_a_code_is_an_unknown_error(monkeypatch, error):
    def fail(*a, **k):
        raise error

    monkeypatch.setattr(snowflake_client, "query_sql", fail)
    assert guard(VIEWER_SQL, tenant=TENANT_A)["error"] == \
        "The warehouse could not run this query (error unknown)."


def test_a_viewers_error_log_line_carries_only_the_code(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    def fail(*a, **k):
        raise SnowflakeError("100069 (22P02): Error parsing Geo input: Alice. Did you mean something else?")

    monkeypatch.setattr(snowflake_client, "query_sql", fail)
    guard(VIEWER_SQL, tenant=TENANT_A)
    assert "100038" in caplog.text and "Alice" not in caplog.text and "Geo" not in caplog.text


def test_an_admins_error_log_line_carries_the_code_and_the_text_before_the_first_quote(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    def fail(*a, **k):
        raise SnowflakeError("Numeric value 'Alice' is not recognized")

    monkeypatch.setattr(snowflake_client, "query_sql", fail)
    guard(VIEWER_SQL, tenant=TENANT_A, allow_identity=True)
    assert "100038" in caplog.text and "Numeric value …" in caplog.text and "Alice" not in caplog.text


def test_the_snowflake_connector_never_logs_below_warning():
    assert logging.getLogger("snowflake.connector").level == logging.WARNING
