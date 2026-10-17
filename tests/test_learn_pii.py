import pytest

import snowflake_client
from semantic_layer.learn import learn_columns, learn_pii
from tests.guard_fakes import guard
from tests.tenant_fakes import TENANT_A


@pytest.fixture
def executed(monkeypatch):
    calls = []
    monkeypatch.setattr(snowflake_client, "query_sql", lambda sql, params=None, **k: calls.append(sql) or {"columns": [], "rows": []})
    return calls


def _validate(sql):
    return guard(sql, tenant=TENANT_A)


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


POSITIONAL_REFUSAL = "Refer to columns by name; positional ($1) and IDENTIFIER() column references are not allowed."


@pytest.mark.parametrize("sql", [
    "SELECT $1 FROM LEARN.USERS",
    "SELECT $2, $3 FROM LEARN.USERS",
    "SELECT u.$1 FROM LEARN.USERS u",
    "SELECT COUNT($1) FROM LEARN.USERS",
    "SELECT IDENTIFIER('FIRSTNAME') FROM LEARN.USERS",
    "SELECT $1 FROM CDM_LMS.PERSON",
    "SELECT p.$2 FROM CDM_LMS.PERSON p",
    "SELECT IDENTIFIER('EMAIL') FROM CDM_LMS.PERSON",
    "SELECT ID FROM CDM_LMS.PERSON WHERE IDENTIFIER('EMAIL') LIKE 'a%'",
])
def test_guard_refuses_positional_and_identifier_columns(executed, sql):
    assert _validate(sql) == {"error": POSITIONAL_REFUSAL}
    assert executed == []


def test_compiled_sql_is_not_checked_for_positional_columns(executed):
    result = guard("SELECT $1 FROM CDM_LMS.COURSE", tenant=TENANT_A, compiled_sql=True)
    assert "error" not in result


@pytest.mark.parametrize("sql", [
    "SELECT x.FOO FROM LEARN.NOT_A_TABLE y",
    "SELECT COUNT(*) FROM LEARN.NOT_A_TABLE y ORDER BY z.Q",
    "WITH users AS (SELECT 1 AS ID FROM LEARN.COURSE) SELECT ID FROM USERS",
    'SELECT U.FIRSTNAME FROM CDM_LMS.COURSE "u" JOIN LEARN.USERS u ON 1=1',
])
def test_guard_refuses_unresolvable_learn_columns_without_raising(executed, sql):
    assert _refused_as_pii(_validate(sql))
    assert executed == []


def test_resolver_exception_is_a_pii_refusal(monkeypatch, executed):
    def boom(*a, **k):
        raise RuntimeError("resolver failure")
    monkeypatch.setattr(snowflake_client, "_resolve", boom)
    assert _refused_as_pii(_validate("SELECT a.EVENT_TYPE FROM LEARN.ACTIVITY_ACCUMULATOR a"))
    assert executed == []


def test_unresolvable_column_listed_by_a_referenced_cdm_table_is_allowed(executed):
    # x matches no FROM alias, so the column cannot be resolved and falls back to the name lists.
    sql = "SELECT COUNT(*), MAX(x.NAME) FROM CDM_LMS.COURSE c JOIN LEARN.COURSE_MAIN m ON m.PK1 = c.ID"
    assert "error" not in _validate(sql)


def test_unresolvable_column_only_a_learn_table_lists_is_pii(executed):
    sql = "SELECT MAX(x.EVENT_TYPE) FROM CDM_LMS.COURSE c JOIN LEARN.ACTIVITY_ACCUMULATOR a ON a.COURSE_PK1 = c.ID"
    assert not learn_pii("ACTIVITY_ACCUMULATOR", "EVENT_TYPE")
    assert _refused_as_pii(_validate(sql))


def test_learn_pii_path_exception_is_a_pii_refusal(monkeypatch, executed):
    def boom(*a, **k):
        raise RuntimeError("LEARN PII failure")
    monkeypatch.setattr(snowflake_client, "_dictionary_pii_columns", boom)
    assert _refused_as_pii(_validate("SELECT a.EVENT_TYPE FROM LEARN.ACTIVITY_ACCUMULATOR a"))
    assert executed == []


ALIAS_LIST_REFUSAL = "Column alias lists on tables are not allowed; alias columns in the SELECT list instead."


@pytest.mark.parametrize("sql", [
    "SELECT C1 FROM CDM_LMS.PERSON AS p (C1)",
    "SELECT C1 FROM CDM_LMS.PERSON p (C1)",
    "SELECT C1 FROM (SELECT ID FROM CDM_LMS.PERSON) AS p (C1)",
    "SELECT C1 FROM LEARN.USERS AS u (C1)",
    "SELECT C1 FROM (SELECT PK1 FROM LEARN.USERS WHERE PK1 > 0) u (C1)",
    "SELECT COUNT(*) FROM CDM_LMS.COURSE c JOIN LEARN.USERS u (C1, C2) ON c.ID = u.C1",
])
def test_guard_refuses_column_alias_lists_on_tables(executed, sql):
    assert _validate(sql) == {"error": ALIAS_LIST_REFUSAL}
    assert executed == []


@pytest.mark.parametrize("sql", [
    "WITH c (X) AS (SELECT EMAIL FROM CDM_LMS.PERSON) SELECT X FROM c",
    "WITH c (X) AS (SELECT u.EMAIL FROM LEARN.USERS u) SELECT X FROM c",
])
def test_cte_column_list_rename_is_caught_at_the_base_read(executed, sql):
    assert _refused_as_pii(_validate(sql))
    assert executed == []


def test_compiled_sql_is_not_checked_for_alias_lists(executed):
    result = guard("SELECT C1 FROM CDM_LMS.COURSE AS c (C1)", tenant=TENANT_A, compiled_sql=True)
    assert "error" not in result


WHOLE_ROW_REFUSAL = "Refer to columns by name; whole-row references are not allowed."


@pytest.mark.parametrize("sql", [
    "SELECT TO_VARIANT(p) FROM CDM_LMS.PERSON p",
    "SELECT f.VALUE FROM CDM_LMS.PERSON p, LATERAL FLATTEN(input => p) f",
    "SELECT TO_VARIANT(PERSON) FROM CDM_LMS.PERSON",
    "SELECT TO_VARIANT(c) FROM (SELECT ID, EMAIL FROM CDM_LMS.PERSON WHERE ID > 0) c",
    "WITH c AS (SELECT ID FROM CDM_LMS.PERSON) SELECT TO_JSON(c) FROM c",
    "SELECT TO_VARIANT(u) FROM LEARN.USERS u",
    "SELECT f.VALUE FROM LEARN.USERS u, LATERAL FLATTEN(input => u) f",
    'SELECT TO_VARIANT("U") FROM LEARN.USERS u',
    "SELECT TO_VARIANT(NAME) FROM CDM_LMS.PERSON NAME WHERE EXISTS (SELECT 1 FROM CDM_LMS.COURSE)",
    "WITH x AS (SELECT 1 AS k FROM CDM_LMS.COURSE) SELECT TO_VARIANT(NAME) FROM CDM_LMS.PERSON NAME",
    "SELECT TO_VARIANT(NAME) FROM CDM_LMS.PERSON NAME, (SELECT 1 AS z FROM CDM_LMS.COURSE) s",
    "SELECT TO_VARIANT(ID) FROM CDM_LMS.PERSON ID",
    "SELECT NAME FROM CDM_LMS.COURSE name",
])
def test_guard_refuses_whole_row_references(executed, sql):
    assert _validate(sql) == {"error": WHOLE_ROW_REFUSAL}
    assert executed == []


@pytest.mark.parametrize("sql", [
    "SELECT c.NAME FROM CDM_LMS.COURSE c",
    "SELECT NAME FROM CDM_LMS.COURSE c",
    "SELECT c.NAME, t.NAME FROM CDM_LMS.COURSE c JOIN CDM_LMS.TERM t ON c.TERM_ID = t.ID",
    "SELECT x.n FROM (SELECT COUNT(*) AS n FROM CDM_LMS.COURSE) x",
])
def test_columns_that_do_not_name_a_row_are_allowed(executed, sql):
    assert "error" not in _validate(sql)
    assert len(executed) == 1


@pytest.mark.parametrize("sql", [
    "SELECT OBJECT_CONSTRUCT(p.*) FROM CDM_LMS.PERSON p",
    "SELECT ARRAY_CONSTRUCT(p.*) FROM CDM_LMS.PERSON p",
    "SELECT HASH(p.*) FROM CDM_LMS.PERSON p",
    "SELECT TO_JSON(OBJECT_CONSTRUCT(p.*)) FROM CDM_LMS.PERSON p",
    "SELECT OBJECT_CONSTRUCT(*) FROM CDM_LMS.PERSON",
    "SELECT OBJECT_CONSTRUCT(u.*) FROM LEARN.USERS u",
    "SELECT HASH(u.*) FROM LEARN.USERS u",
])
def test_guard_refuses_row_stars_inside_functions(executed, sql):
    assert "Star projections" in _validate(sql)["error"]
    assert executed == []


def test_whole_row_check_exception_is_a_refusal(monkeypatch, executed):
    def boom(*a, **k):
        raise RuntimeError("table lookup failure")
    monkeypatch.setattr(snowflake_client, "_folded", boom)
    assert _validate("SELECT a.EVENT_TYPE FROM LEARN.ACTIVITY_ACCUMULATOR a") == {"error": WHOLE_ROW_REFUSAL}
    assert executed == []
