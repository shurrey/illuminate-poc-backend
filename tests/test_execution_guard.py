import pytest

import snowflake_client


@pytest.fixture
def executed(monkeypatch):
    calls = []
    monkeypatch.setattr(snowflake_client, "query_sql", lambda sql, params=None: calls.append(sql) or {"columns": [], "rows": []})
    return calls


def test_pii_wrapped_in_an_unknown_function_is_blocked(executed):
    result = snowflake_client.validate_and_execute("SELECT MY_UDF(FIRST_NAME) FROM CDM_LMS.PERSON")
    assert "personally identifiable" in result["error"]
    assert executed == []


def test_pii_under_a_real_aggregate_is_allowed(executed):
    result = snowflake_client.validate_and_execute("SELECT COUNT(DISTINCT EMAIL) FROM CDM_LMS.PERSON")
    assert "error" not in result
    assert len(executed) == 1


def test_bare_pii_column_is_blocked(executed):
    result = snowflake_client.validate_and_execute("SELECT EMAIL FROM CDM_LMS.PERSON")
    assert "personally identifiable" in result["error"]
