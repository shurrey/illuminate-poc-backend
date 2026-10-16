import pytest

import snowflake_client
from semantic_layer.learn import learn_columns, learn_pii
from tests.tenant_fakes import TENANT_A


@pytest.fixture
def executed(monkeypatch):
    calls = []
    monkeypatch.setattr(snowflake_client, "query_sql", lambda sql, params=None, **k: calls.append(sql) or {"columns": [], "rows": []})
    return calls


def _validate(sql):
    return snowflake_client.validate_and_execute(sql, tenant=TENANT_A)


def _refused_as_pii(result):
    return "personally identifiable" in result.get("error", "")


def test_learn_flagged_column_is_pii():
    assert "USER_PK1" in learn_columns("ACTIVITY_ACCUMULATOR")
    assert learn_pii("ACTIVITY_ACCUMULATOR", "USER_PK1")
    assert learn_pii("USERS", "EMAIL")


def test_learn_unlisted_column_is_pii():
    assert learn_pii("ACTIVITY_ACCUMULATOR", "NOT_A_COLUMN")
    assert learn_columns("NOT_A_TABLE") is None
    assert learn_pii("NOT_A_TABLE", "EVENT_TYPE")


def test_learn_unflagged_column_allowed():
    assert not learn_pii("ACTIVITY_ACCUMULATOR", "EVENT_TYPE")
    assert not learn_pii("COURSE_MAIN", "COURSE_NAME")


@pytest.mark.parametrize("sql", [
    "SELECT U.EMAIL FROM LEARN.USERS U",
    "SELECT a.USER_PK1 FROM LEARN.ACTIVITY_ACCUMULATOR a",
    "SELECT USER_PK1 FROM LEARN.ACTIVITY_ACCUMULATOR",
    "SELECT u.INST_EMAIL FROM learn.users u",
    "SELECT a.EVENT_TYPE, a.USER_PK1 FROM LEARN.ACTIVITY_ACCUMULATOR a GROUP BY 1, 2",
    "SELECT MIN(u.STUDENT_ID) FROM LEARN.USERS u",
    "SELECT a.NOT_IN_THE_DICTIONARY FROM LEARN.ACTIVITY_ACCUMULATOR a",
    "SELECT NOT_IN_THE_DICTIONARY FROM LEARN.ACTIVITY_ACCUMULATOR",
    "SELECT x.EVENT_TYPE FROM LEARN.NOT_A_TABLE x",
    "SELECT c.COURSE_NAME, u.STUDENT_ID FROM LEARN.COURSE_MAIN c JOIN LEARN.COURSE_USERS cu ON cu.CRSMAIN_PK1 = c.PK1"
    " JOIN LEARN.USERS u ON u.PK1 = cu.USERS_PK1",
])
def test_guard_refuses_learn_pii_outside_count(executed, sql):
    assert _refused_as_pii(_validate(sql))
    assert executed == []


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(DISTINCT U.EMAIL) FROM LEARN.USERS U",
    "SELECT a.EVENT_TYPE, COUNT(DISTINCT a.USER_PK1) AS users FROM LEARN.ACTIVITY_ACCUMULATOR a GROUP BY 1",
    "SELECT EVENT_TYPE, APPROX_COUNT_DISTINCT(USER_PK1) FROM LEARN.ACTIVITY_ACCUMULATOR GROUP BY EVENT_TYPE",
    "SELECT a.EVENT_TYPE FROM LEARN.ACTIVITY_ACCUMULATOR a WHERE a.USER_PK1 = 5",
])
def test_guard_allows_count_of_learn_pii(executed, sql):
    assert "error" not in _validate(sql)
    assert len(executed) == 1


@pytest.mark.parametrize("sql", [
    "WITH people AS (SELECT u.EMAIL AS contact FROM LEARN.USERS u) SELECT p.contact FROM people p",
    "WITH people AS (SELECT EMAIL AS contact FROM LEARN.USERS) SELECT contact FROM people",
    "SELECT x.who FROM (SELECT a.USER_PK1 AS who FROM LEARN.ACTIVITY_ACCUMULATOR a) x",
    "WITH acts AS (SELECT a.USER_PK1 AS who, a.EVENT_TYPE FROM LEARN.ACTIVITY_ACCUMULATOR a) SELECT COUNT(who), EVENT_TYPE FROM acts GROUP BY 2",
])
def test_guard_learn_alias_and_cte(executed, sql):
    assert _refused_as_pii(_validate(sql))
    assert executed == []


@pytest.mark.parametrize("sql", [
    "SELECT n FROM (SELECT COUNT(*) AS n FROM LEARN.USERS) ORDER BY n",
    "WITH people AS (SELECT COUNT(DISTINCT u.EMAIL) AS people FROM LEARN.USERS u) SELECT p.people FROM people p",
    "WITH acts AS (SELECT a.EVENT_TYPE AS kind FROM LEARN.ACTIVITY_ACCUMULATOR a) SELECT kind, COUNT(*) AS n FROM acts GROUP BY kind ORDER BY n DESC",
    "SELECT COUNT(*) AS n FROM LEARN.USERS GROUP BY 1 HAVING n > 1 ORDER BY n",
    "SELECT a.EVENT_TYPE, c.COURSE_NAME FROM LEARN.ACTIVITY_ACCUMULATOR a JOIN LEARN.COURSE_MAIN c ON c.PK1 = a.COURSE_PK1",
    "SELECT EVENT_TYPE, COURSE_NAME FROM LEARN.ACTIVITY_ACCUMULATOR a JOIN LEARN.COURSE_MAIN c ON c.PK1 = a.COURSE_PK1",
    "SELECT a.EVENT_TYPE FROM LEARN.ACTIVITY_ACCUMULATOR a WHERE EXISTS (SELECT 1 FROM LEARN.USERS u WHERE u.PK1 = a.USER_PK1)",
])
def test_guard_allows_learn_aliases_of_safe_values(executed, sql):
    assert "error" not in _validate(sql)
    assert len(executed) == 1


def test_guard_resolves_cdm_columns_in_a_learn_query(executed):
    sql = ("SELECT c.NAME, COUNT(*) FROM CDM_LMS.COURSE c JOIN LEARN.COURSE_MAIN m ON m.COURSE_ID = c.SOURCE_ID"
           " GROUP BY 1")
    assert "error" not in _validate(sql)


def test_guard_refuses_learn_star(executed):
    assert "Star projections" in _validate("SELECT * FROM LEARN.ACTIVITY_ACCUMULATOR")["error"]
    assert executed == []


def test_guard_does_not_bind_an_unlisted_learn_column_to_an_outer_table(executed):
    sql = "SELECT c.NAME, (SELECT MAX(NAME) FROM LEARN.USERS) FROM CDM_LMS.COURSE c"
    assert _refused_as_pii(_validate(sql))


def test_guard_resolves_a_qualified_correlated_reference_to_the_outer_table(executed):
    sql = "SELECT (SELECT MAX(c.NAME) FROM LEARN.COURSE_MAIN m WHERE m.COURSE_ID = c.SOURCE_ID) FROM CDM_LMS.COURSE c"
    assert "error" not in _validate(sql)
