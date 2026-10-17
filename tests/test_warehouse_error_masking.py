import logging

import pytest
from fastapi.testclient import TestClient

import chat_engine
import lambda_handler
import snowflake_client
from semantic_layer.catalog import load_catalog
from semantic_layer.chat_tools import ChatTools
from tests.tenant_fakes import TENANT_A, install_tenants, user_of

NUMERIC = "100038 (22018): 01b2c3d4: Numeric value 'Alice' is not recognized"
VIEWER_SQL = "SELECT COUNT(TO_NUMBER(p.FIRST_NAME)) AS n FROM CDM_LMS.PERSON p"


@pytest.fixture
def warehouse_fails(monkeypatch):
    def fail(sql, params=None, **k):
        raise RuntimeError(NUMERIC)

    monkeypatch.setattr(snowflake_client, "query_sql", fail)
    monkeypatch.setattr(snowflake_client, "_run", fail)


@pytest.mark.parametrize("raw, masked", [
    ("Numeric value 'Alice' is not recognized", "Numeric value '***' is not recognized"),
    ("String 'O''Brien' is too long", "String '***' is too long"),
    (r"String 'O\'Brien' is too long", "String '***' is too long"),
    ("Can't parse 'Alice' as timestamp with format 'AUTO'", "Can't parse '***' as timestamp with format '***'"),
    ("String 'O'Brien' is too long", "String '***' is too long"),
    ('Error parsing JSON: unknown keyword "Alice", pos 6', 'Error parsing JSON: unknown keyword "***", pos 6'),
    ("Date 'Alice is not recognized", "Date '***'"),
    ("Division by zero", "Division by zero"),
])
def test_mask_literals_hides_every_quoted_value(raw, masked):
    assert snowflake_client.mask_literals(raw) == masked


def test_a_warehouse_error_is_returned_and_logged_with_its_literals_masked(warehouse_fails, caplog):
    caplog.set_level(logging.DEBUG)
    result = snowflake_client.validate_and_execute(VIEWER_SQL, tenant=TENANT_A)
    assert result["warehouse_error"] is True
    assert "Alice" not in result["error"] and "is not recognized" in result["error"]
    assert "Alice" not in caplog.text


def test_a_viewers_chat_tool_result_and_its_log_line_never_carry_the_value(warehouse_fails, caplog):
    caplog.set_level(logging.DEBUG)
    tools = ChatTools(load_catalog(), "DB", tenant=TENANT_A)
    use = {"toolUseId": "t1", "name": "execute_sql",
           "input": {"sql": VIEWER_SQL, "reason": "no governed metric parses first names"}}
    artifacts = []
    message = chat_engine._run_tool(use, artifacts, ["search_catalog"], tools)
    assert "is not recognized" in str(message) and "Alice" not in str(message)
    assert artifacts == [] and "Alice" not in caplog.text


def test_a_semantic_query_502_and_its_log_line_never_carry_the_value(monkeypatch, warehouse_fails, caplog):
    caplog.set_level(logging.DEBUG)
    install_tenants(monkeypatch, TENANT_A, database="TESTDB")
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user_of(TENANT_A))
    r = TestClient(lambda_handler.app).post("/api/v1/semantic/query", headers={"Authorization": "Bearer t"},
                                            json={"metrics": ["metric.reportable_courses.v1"]})
    assert r.status_code == 502
    assert "Alice" not in r.text and "Alice" not in caplog.text


def test_a_preview_failure_is_raised_with_its_literals_masked(warehouse_fails):
    with pytest.raises(snowflake_client.WarehouseError) as raised:
        snowflake_client.query_preview("CDM_LMS", "PERSON", tenant=TENANT_A)
    assert "Alice" not in str(raised.value) and raised.value.__cause__ is None
