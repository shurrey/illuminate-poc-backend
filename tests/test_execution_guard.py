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


@pytest.mark.parametrize("sql", [
    "SELECT ARRAY_AGG(EMAIL) FROM CDM_LMS.PERSON",
    "SELECT LISTAGG(EMAIL, ',') FROM CDM_LMS.PERSON",
    "SELECT ARRAY_UNIQUE_AGG(EMAIL) FROM CDM_LMS.PERSON",
    "SELECT ANY_VALUE(EMAIL) FROM CDM_LMS.PERSON",
    "SELECT MIN_BY(EMAIL, ID) FROM CDM_LMS.PERSON",
    "SELECT MIN(EMAIL) FROM CDM_LMS.PERSON",
    "SELECT MAX(LAST_NAME) FROM CDM_LMS.PERSON",
    "SELECT LAG(EMAIL) OVER (ORDER BY ID) FROM CDM_LMS.PERSON",
    "SELECT FIRST_VALUE(EMAIL) OVER (PARTITION BY ID ORDER BY ID) FROM CDM_LMS.PERSON",
    "SELECT COUNT(EMAIL) OVER (PARTITION BY EMAIL), EMAIL FROM CDM_LMS.PERSON",
    "SELECT EMAIL, COUNT(*) OVER () FROM CDM_LMS.PERSON",
    "SELECT EMAIL, COUNT(*) FROM CDM_LMS.PERSON GROUP BY EMAIL",
    "SELECT UPPER(EMAIL), COUNT(*) FROM CDM_LMS.PERSON GROUP BY 1",
])
def test_pii_must_be_inside_a_counting_aggregate(sql, executed):
    result = snowflake_client.validate_and_execute(sql)
    assert "personally identifiable" in result.get("error", ""), sql
    assert executed == []


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(DISTINCT EMAIL) FROM CDM_LMS.PERSON",
    "SELECT COUNT_IF(EMAIL IS NOT NULL) FROM CDM_LMS.PERSON",
    "SELECT APPROX_COUNT_DISTINCT(EMAIL) FROM CDM_LMS.PERSON",
    "SELECT INSTITUTION_ROLE, COUNT(DISTINCT EMAIL) FROM CDM_LMS.PERSON GROUP BY 1",
    "SELECT COUNT(*) FROM CDM_LMS.PERSON WHERE EMAIL LIKE '%@example.edu'",
])
def test_counting_pii_is_allowed(sql, executed):
    assert "error" not in snowflake_client.validate_and_execute(sql), sql
    assert len(executed) == 1
