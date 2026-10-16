import pytest

import licenses
import snowflake_client
from tenancy import TenantNotReady
from tests.tenant_fakes import TENANT_A, TENANT_CORE


def _validate(sql, **kwargs):
    return snowflake_client.validate_and_execute(sql, tenant=TENANT_A, **kwargs)


@pytest.fixture
def executed(monkeypatch):
    calls = []
    monkeypatch.setattr(snowflake_client, "query_sql", lambda sql, params=None, **k: calls.append(sql) or {"columns": [], "rows": []})
    return calls


def test_pii_wrapped_in_an_unknown_function_is_blocked(executed):
    result = _validate("SELECT MY_UDF(FIRST_NAME) FROM CDM_LMS.PERSON")
    assert "personally identifiable" in result["error"]
    assert executed == []


def test_pii_under_a_real_aggregate_is_allowed(executed):
    result = _validate("SELECT COUNT(DISTINCT EMAIL) FROM CDM_LMS.PERSON")
    assert "error" not in result
    assert len(executed) == 1


def test_bare_pii_column_is_blocked(executed):
    result = _validate("SELECT EMAIL FROM CDM_LMS.PERSON")
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
    result = _validate(sql)
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
    assert "error" not in _validate(sql), sql
    assert len(executed) == 1


@pytest.mark.parametrize("sql", [
    "SELECT * FROM CDM_LMS.PERSON",
    "SELECT p.* FROM CDM_LMS.PERSON p",
    "SELECT OBJECT_CONSTRUCT(*) FROM CDM_LMS.PERSON",
    "SELECT x FROM (SELECT EMAIL AS x FROM CDM_LMS.PERSON)",
    "WITH p AS (SELECT LAST_NAME AS n FROM CDM_LMS.PERSON) SELECT n FROM p",
    "SELECT COUNT(*) FROM CDM_LMS.PERSON UNION ALL SELECT EMAIL FROM CDM_LMS.PERSON",
])
def test_freehand_sql_cannot_reach_pii_through_stars_subqueries_or_unions(sql, executed):
    assert "error" in _validate(sql), sql
    assert executed == []


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM CDM_LMS.PERSON",
    "WITH s AS (SELECT PERSON_ID, COURSE_ID FROM CDM_LMS.PERSON_COURSE) SELECT COUNT(DISTINCT PERSON_ID) FROM s",
])
def test_freehand_sql_without_pii_projections_runs(sql, executed):
    assert "error" not in _validate(sql), sql


def test_compiled_sql_may_project_pii_inside_its_ctes(executed):
    sql = "WITH d AS (SELECT p.*, 1 AS K FROM CDM_LMS.PERSON p) SELECT COUNT(DISTINCT EMAIL) AS n FROM d"
    assert "error" in _validate(sql)
    assert "error" not in _validate(sql, compiled=True)


class _Cursor:
    description = [("N",)]

    def __init__(self, n):
        self.n = n

    def execute(self, sql, params=None):
        pass

    def fetchmany(self, size):
        return [(i,) for i in range(min(size, self.n))]

    def fetchall(self):
        raise AssertionError("fetchall pulls the whole result")

    def close(self):
        pass


def test_results_are_capped_at_max_rows_and_marked_truncated(monkeypatch):
    class Conn:
        def cursor(self):
            return _Cursor(5000)

    monkeypatch.setattr(snowflake_client, "get_connection", lambda tenant: Conn())
    result = snowflake_client.query_sql("SELECT N FROM CDM_LMS.T", tenant=object())
    assert len(result["rows"]) == snowflake_client.MAX_ROWS
    assert result["truncated"] is True


@pytest.mark.parametrize("sql", [
    "SELECT 1 AS N FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))",
    "SELECT QUERY_TEXT FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY())",
    "SELECT 1 AS N FROM TABLE(GENERATOR(ROWCOUNT => 100000000))",
])
@pytest.mark.parametrize("compiled", [False, True])
def test_table_functions_other_than_a_bounded_generator_are_blocked(sql, compiled, executed):
    result = _validate(sql, compiled=compiled)
    assert "table function" in result["error"]
    assert executed == []


def test_bounded_generator_and_lateral_functions_are_allowed(executed):
    result = _validate(
        "SELECT SEQ4() AS N FROM TABLE(GENERATOR(ROWCOUNT => 10)) "
        "UNION ALL SELECT s.VALUE FROM CDM_LMS.COURSE c, LATERAL SPLIT_TO_TABLE(c.NAME, ',') s")
    assert "error" not in result


@pytest.mark.parametrize("sql", [
    "SELECT v FROM CDM_LMS.PERSON UNPIVOT(v FOR c IN (first_name, last_name))",
    "SELECT f.value AS v FROM CDM_LMS.PERSON p, LATERAL FLATTEN(input => ARRAY_CONSTRUCT(p.first_name, p.email)) f",
    "SELECT \"'x'\" AS n FROM CDM_LMS.PERSON PIVOT(MAX(first_name) FOR a IN ('x'))",
    "SELECT f AS v FROM CDM_LMS.PERSON MATCH_RECOGNIZE(ORDER BY id MEASURES first_name AS f "
    "ONE ROW PER MATCH PATTERN (x) DEFINE x AS TRUE)",
    "SELECT ID FROM CDM_LMS.PERSON ORDER BY LAST_NAME",
    "SELECT ID FROM CDM_LMS.PERSON GROUP BY ID, EMAIL",
])
def test_freehand_pii_outside_a_count_or_filter_is_blocked_anywhere_in_the_statement(sql, executed):
    result = _validate(sql)
    assert "personally identifiable" in result["error"]
    assert executed == []


@pytest.mark.parametrize("sql", [
    "SELECT COUNT_IF(EMAIL LIKE '%@example.edu') AS n FROM CDM_LMS.PERSON",
    "SELECT COUNT(*) AS n FROM CDM_LMS.PERSON HAVING MAX(EMAIL) = 'a@b.c'",
    "SELECT ID FROM CDM_LMS.PERSON QUALIFY ROW_NUMBER() OVER (PARTITION BY EMAIL ORDER BY ID) = 1",
    "SELECT p.ID FROM CDM_LMS.PERSON p JOIN CDM_LMS.PERSON q ON p.EMAIL = q.EMAIL",
])
def test_freehand_pii_in_a_count_or_filter_is_allowed(sql, executed):
    assert "error" not in _validate(sql)


COMPILED_NAMES = "WITH DS AS (SELECT FIRST_NAME, EMAIL FROM CDM_LMS.PERSON) SELECT FIRST_NAME AS first, EMAIL AS mail FROM DS"


def test_compiled_identity_passes_only_when_the_caller_may_see_identity(monkeypatch):
    monkeypatch.setattr(snowflake_client, "query_sql", lambda sql, params=None, **k: {"columns": [], "rows": []})
    assert "error" in _validate(COMPILED_NAMES, compiled=True)
    assert "error" not in _validate(COMPILED_NAMES, compiled=True, allow_identity=True)


def test_freehand_sql_never_returns_identity_whatever_the_role(monkeypatch):
    monkeypatch.setattr(snowflake_client, "query_sql", lambda sql, params=None, **k: {"columns": [], "rows": []})
    assert "error" in _validate("SELECT EMAIL FROM CDM_LMS.PERSON", allow_identity=True)


def test_declared_dataset_pii_is_guarded_for_callers_without_identity(monkeypatch):
    monkeypatch.setattr(snowflake_client, "query_sql", lambda sql, params=None, **k: {"columns": [], "rows": []})
    sql = "WITH DS AS (SELECT CREATOR_NAME FROM CDM_LMS.PERSON) SELECT CREATOR_NAME AS creator_name FROM DS"
    assert "error" in _validate(sql, compiled=True, extra_pii={"CREATOR_NAME"})
    assert "error" not in _validate(sql, compiled=True, extra_pii={"CREATOR_NAME"}, allow_identity=True)


@pytest.fixture
def own_database(monkeypatch):
    monkeypatch.setattr(snowflake_client, "tenant_database", lambda tenant: "MY_DB")


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM OTHER_DB.CDM_LMS.COURSE",
    "SELECT COUNT(*) FROM other_db.cdm_lms.course",
    'SELECT COUNT(*) FROM "OTHER_DB".CDM_LMS.COURSE',
    "SELECT COUNT(*) FROM OTHER_DB.INFORMATION_SCHEMA.TABLES",
    "SELECT TABLE_NAME FROM OTHER_DB.INFORMATION_SCHEMA.DATABASES",
    "SELECT COUNT(*) FROM CDM_LMS.COURSE c JOIN OTHER_DB.CDM_LMS.TERM t ON c.ID = t.ID",
    "WITH x AS (SELECT COUNT(*) AS n FROM OTHER_DB.CDM_LMS.COURSE) SELECT n FROM x",
    "SELECT COUNT(*) FROM MY_DB.CDM_LMS.COURSE WHERE ID IN (SELECT ID FROM OTHER_DB.CDM_LMS.TERM)",
])
def test_a_table_in_another_database_is_a_guard_error(sql, executed, own_database):
    result = _validate(sql)
    assert "not the tenant's database" in result["error"], sql
    assert "warehouse_error" not in result
    assert executed == []


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM MY_DB.CDM_LMS.COURSE",
    "SELECT COUNT(*) FROM my_db.cdm_lms.course",
    'SELECT COUNT(*) FROM "MY_DB".CDM_LMS.COURSE',
    "SELECT TABLE_NAME FROM MY_DB.INFORMATION_SCHEMA.TABLES",
    "SELECT COUNT(*) FROM CDM_LMS.COURSE",
])
def test_the_tenants_own_database_is_allowed(sql, executed, own_database):
    assert "error" not in _validate(sql), sql
    assert len(executed) == 1


def test_a_database_qualified_table_for_a_tenant_that_is_not_set_up_fails_closed(executed, monkeypatch):
    def not_ready(tenant):
        raise TenantNotReady(tenant.tenant_id)

    monkeypatch.setattr(snowflake_client, "tenant_database", not_ready)
    with pytest.raises(TenantNotReady):
        _validate("SELECT COUNT(*) FROM MY_DB.CDM_LMS.COURSE")
    assert executed == []


CORE = TENANT_CORE


def _validate_as(tenant, sql):
    return snowflake_client.validate_and_execute(sql, tenant=tenant)


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM LEARN.USERS",
    'SELECT COUNT(*) FROM "LEARN"."USERS"',
    "SELECT COUNT(*) FROM learn.users",
    "SELECT COUNT(*) FROM DB_CORE_COLLEGE.LEARN.USERS",
    "WITH u AS (SELECT * FROM LEARN.USERS) SELECT COUNT(*) FROM u",
    "SELECT COUNT(*) FROM CDM_LMS.COURSE c JOIN LEARN.USERS u ON c.ID = u.ID",
    "SELECT COUNT(*) FROM CDM_LMS.COURSE WHERE ID IN (SELECT ID FROM LEARN.USERS)",
    "SELECT ID FROM CDM_LMS.COURSE UNION ALL SELECT ID FROM LEARN.USERS",
    "SELECT LEARN.F(1)",
])
def test_core_learn_refused_with_premium_message(monkeypatch, executed, sql):
    from tests.tenant_fakes import install_tenants
    install_tenants(monkeypatch, CORE, database="DB_CORE_COLLEGE")
    result = _validate_as(CORE, sql)
    assert result == {"error": licenses.LEARN_REFUSAL}
    assert executed == []


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM LEARN.USERS",
    "SELECT COUNT(*) FROM CDM_LMS.COURSE c JOIN learn.users u ON c.ID = u.ID",
])
def test_premium_learn_allowed(executed, sql):
    assert "error" not in _validate(sql)
    assert len(executed) == 1


@pytest.mark.parametrize("sql", ["SHOW TABLES", "DESCRIBE TABLE CDM_LMS.COURSE", "show schemas"])
def test_show_and_describe_are_refused(executed, sql):
    result = _validate(sql)
    assert "error" in result and "warehouse_error" not in result
    assert executed == []


@pytest.mark.parametrize("sql", [
    "SELECT OTHER_DB.CDM_LMS.F(1)",
    "SELECT * FROM CDM_LMS.COURSE, LATERAL FLATTEN(input => OTHER_DB.S.F())",
])
def test_function_in_other_database_refused(monkeypatch, executed, sql):
    from tests.tenant_fakes import install_tenants
    install_tenants(monkeypatch, TENANT_A)
    result = _validate(sql)
    assert "not the tenant's database" in result["error"]
    assert executed == []


def test_function_in_own_database_allowed(monkeypatch, executed):
    from tests.tenant_fakes import install_tenants
    install_tenants(monkeypatch, TENANT_A)
    assert "error" not in _validate("SELECT DB_TENANT_A.CDM_LMS.F(1) FROM CDM_LMS.COURSE")
    assert len(executed) == 1


def test_function_in_disallowed_schema_refused(executed):
    assert "not in the allowed list" in _validate("SELECT PUBLIC.F(1)")["error"]
    assert executed == []


@pytest.mark.parametrize("sql", [
    "SELECT DATABASE_NAME FROM INFORMATION_SCHEMA.DATABASES",
    "SELECT DATABASE_NAME FROM DB_TENANT_A.INFORMATION_SCHEMA.DATABASES",
    "SELECT database_name FROM information_schema.databases",
])
def test_information_schema_databases_refused(monkeypatch, executed, sql):
    from tests.tenant_fakes import install_tenants
    install_tenants(monkeypatch, TENANT_A)
    assert _validate(sql) == {"error": "INFORMATION_SCHEMA.DATABASES is not accessible."}
    assert executed == []


def test_information_schema_tables_still_allowed(executed):
    assert "error" not in _validate("SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES")


@pytest.mark.parametrize("sql", [
    "SELECT ID FROM USERS",
    "SELECT ID FROM DB_CORE_COLLEGE..USERS",
    "SELECT ID FROM CDM_LMS.COURSE WHERE ID IN (SELECT ID FROM USERS)",
    "WITH a AS (SELECT 1 AS ID FROM CDM_LMS.COURSE) SELECT ID FROM a UNION ALL SELECT ID FROM USERS",
    "SELECT ID FROM (WITH USERS AS (SELECT 1 AS ID FROM CDM_LMS.COURSE) SELECT ID FROM USERS) UNION ALL SELECT ID FROM USERS",
    "WITH a AS (SELECT ID FROM USERS), USERS AS (SELECT 1 AS ID FROM CDM_LMS.COURSE) SELECT ID FROM a",
    "WITH USERS AS (SELECT ID FROM USERS) SELECT ID FROM USERS",
    'WITH "users" AS (SELECT 1 AS ID FROM CDM_LMS.COURSE) SELECT ID FROM users',
    'WITH "Users" AS (SELECT 1 AS ID FROM CDM_LMS.COURSE) SELECT ID FROM USERS',
    'WITH USERS AS (SELECT 1 AS ID FROM CDM_LMS.COURSE) SELECT ID FROM "users"',
    "WITH a AS (SELECT ID FROM b), b AS (SELECT 1 AS ID FROM CDM_LMS.COURSE) SELECT ID FROM a",
])
def test_unqualified_table_is_refused_for_core_and_premium(monkeypatch, executed, sql):
    from tests.tenant_fakes import install_tenants
    install_tenants(monkeypatch, CORE, TENANT_A, database="DB_CORE_COLLEGE")
    for tenant in (CORE, TENANT_A):
        result = _validate_as(tenant, sql)
        assert "Name the schema for table" in result["error"]
    assert executed == []


def test_unqualified_table_message_names_the_table(executed):
    assert _validate("SELECT ID FROM USERS")["error"] == "Name the schema for table USERS (for example CDM_LMS.USERS)."


@pytest.mark.parametrize("sql", [
    "WITH a AS (SELECT ID FROM CDM_LMS.COURSE) SELECT ID FROM a",
    "WITH a AS (SELECT ID FROM CDM_LMS.COURSE) SELECT ID FROM CDM_LMS.COURSE WHERE ID IN (SELECT ID FROM a)",
    "WITH a AS (SELECT ID FROM CDM_LMS.COURSE), b AS (SELECT ID FROM a) SELECT ID FROM b",
    "SELECT ID FROM (WITH a AS (SELECT ID FROM CDM_LMS.COURSE) SELECT ID FROM a) x",
    "WITH a AS (WITH b AS (SELECT ID FROM CDM_LMS.COURSE) SELECT ID FROM b) SELECT ID FROM a",
    "WITH a AS (SELECT ID FROM CDM_LMS.COURSE), b AS (WITH c AS (SELECT ID FROM a) SELECT ID FROM c) SELECT ID FROM b",
    "WITH users AS (SELECT 1 AS ID FROM CDM_LMS.COURSE) SELECT ID FROM USERS",
    'WITH "USERS" AS (SELECT 1 AS ID FROM CDM_LMS.COURSE) SELECT ID FROM users',
    'WITH "users" AS (SELECT 1 AS ID FROM CDM_LMS.COURSE) SELECT ID FROM "users"',
    "WITH RECURSIVE a AS (SELECT 1 AS ID UNION ALL SELECT ID + 1 FROM a WHERE ID < 5) SELECT ID FROM a",
    "SELECT SEQ4() FROM TABLE(GENERATOR(ROWCOUNT => 5))",
])
def test_cte_names_and_generator_are_allowed_unqualified(executed, sql):
    assert "error" not in _validate(sql)
    assert len(executed) == 1
