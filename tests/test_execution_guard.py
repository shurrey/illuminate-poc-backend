import dataclasses
from unittest import mock

import pytest

import licenses
import snowflake_client
from tenancy import TenantNotReady
from tests.guard_fakes import compiled as compiled_mode, guard_ctx, run
from tests.tenant_fakes import TENANT_A, TENANT_CORE


@pytest.fixture(autouse=True)
def _default_database(monkeypatch):
    monkeypatch.setattr(snowflake_client, "tenant_database", lambda tenant: "MY_DB")


def _validate(sql, *, tenant=TENANT_A, compiled=False, spliced=None, **kwargs):
    """The guard under the tenant's database; spliced stands in for the splice the guard would build, with sql."""
    ctx = guard_ctx(tenant, database=snowflake_client.tenant_database(tenant), **kwargs)
    if spliced is None:
        return run(sql, ctx, compiled_mode(sql) if compiled else snowflake_client.Freehand())
    with mock.patch.object(snowflake_client, "splice", lambda *a, **k: dataclasses.replace(spliced, sql=sql)):
        return run(None, ctx, snowflake_client.Spliced("SELECT 1", {}))


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
    "WITH s AS (SELECT COURSE_ID FROM CDM_LMS.PERSON_COURSE) SELECT COUNT(DISTINCT COURSE_ID) FROM s",
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
    "SELECT INSTITUTION_ROLE FROM CDM_LMS.PERSON QUALIFY ROW_NUMBER() OVER (PARTITION BY EMAIL ORDER BY ID) = 1",
    "SELECT p.INSTITUTION_ROLE FROM CDM_LMS.PERSON p JOIN CDM_LMS.PERSON q ON p.EMAIL = q.EMAIL",
])
def test_freehand_pii_in_a_count_or_filter_is_allowed(sql, executed):
    assert "error" not in _validate(sql)


COMPILED_NAMES = "WITH DS AS (SELECT FIRST_NAME, EMAIL FROM CDM_LMS.PERSON) SELECT FIRST_NAME AS first, EMAIL AS mail FROM DS"


def test_compiled_identity_passes_only_when_the_caller_may_see_identity(monkeypatch):
    monkeypatch.setattr(snowflake_client, "query_sql", lambda sql, params=None, **k: {"columns": [], "rows": []})
    assert "error" in _validate(COMPILED_NAMES, compiled=True)
    assert "error" not in _validate(COMPILED_NAMES, compiled=True, allow_identity=True)


def test_freehand_identity_passes_only_when_the_caller_may_see_identity(executed):
    sql = "SELECT p.EMAIL FROM CDM_LMS.PERSON p"
    assert "personally identifiable" in _validate(sql)["error"]
    assert "error" not in _validate(sql, allow_identity=True)
    assert len(executed) == 1


def test_freehand_extra_pii_is_skipped_with_identity(executed):
    sql = "SELECT CREATOR_NAME FROM CDM_LMS.PERSON"
    assert "error" in _validate(sql, extra_pii={"CREATOR_NAME"})
    assert "error" not in _validate(sql, extra_pii={"CREATOR_NAME"}, allow_identity=True)


def test_freehand_learn_pii_runs_on_premium_with_identity_only(executed):
    sql = "SELECT a.USER_PK1 FROM LEARN.ACTIVITY_ACCUMULATOR a"
    assert "personally identifiable" in _validate(sql)["error"]
    assert "error" not in _validate(sql, allow_identity=True)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM CDM_LMS.PERSON",
    "SELECT p.* FROM CDM_LMS.PERSON p",
    "SELECT $1 FROM CDM_LMS.PERSON",
    "SELECT IDENTIFIER('EMAIL') FROM CDM_LMS.PERSON",
    "SELECT x FROM CDM_LMS.PERSON AS p (x, y)",
    "SELECT OBJECT_CONSTRUCT(p.*) FROM CDM_LMS.PERSON p",
    "SELECT EMAIL FROM CDM_LMS.PERSON LIMIT 5000",
    "SELECT EMAIL FROM OTHER_SCHEMA.PERSON",
    "SELECT EMAIL FROM CDM_LMS.PERSON; SELECT 1",
])
def test_structural_and_other_checks_still_apply_with_identity(executed, sql):
    assert "error" in _validate(sql, allow_identity=True)
    assert executed == []


def test_declared_dataset_pii_is_guarded_for_callers_without_identity(monkeypatch):
    monkeypatch.setattr(snowflake_client, "query_sql", lambda sql, params=None, **k: {"columns": [], "rows": []})
    sql = "WITH DS AS (SELECT CREATOR_NAME FROM CDM_LMS.PERSON) SELECT CREATOR_NAME AS creator_name FROM DS"
    assert "error" in _validate(sql, compiled=True, extra_pii={"CREATOR_NAME"})
    assert "error" not in _validate(sql, compiled=True, extra_pii={"CREATOR_NAME"}, allow_identity=True)


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM OTHER_DB.CDM_LMS.COURSE",
    "SELECT COUNT(*) FROM other_db.cdm_lms.course",
    'SELECT COUNT(*) FROM "OTHER_DB".CDM_LMS.COURSE',
    "SELECT COUNT(*) FROM OTHER_DB.INFORMATION_SCHEMA.TABLES",
    "SELECT TABLE_NAME FROM OTHER_DB.INFORMATION_SCHEMA.DATABASES",
    "SELECT COUNT(*) FROM CDM_LMS.COURSE c JOIN OTHER_DB.CDM_LMS.TERM t ON c.ID = t.ID",
    "WITH x AS (SELECT COUNT(*) AS n FROM OTHER_DB.CDM_LMS.COURSE) SELECT n FROM x",
    "SELECT COUNT(*) FROM MY_DB.CDM_LMS.COURSE WHERE ID IN (SELECT ID FROM OTHER_DB.CDM_LMS.TERM)",
    'SELECT COUNT(*) FROM "my_db".CDM_LMS.COURSE',
    'SELECT "my_db".CDM_LMS.F(1) AS n FROM CDM_LMS.COURSE',
])
def test_a_table_in_another_database_is_a_guard_error(sql, executed):
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
def test_the_tenants_own_database_is_allowed(sql, executed):
    assert "error" not in _validate(sql), sql
    assert len(executed) == 1


def test_a_guard_context_for_a_tenant_that_is_not_set_up_fails_closed(executed, monkeypatch):
    import lambda_handler

    def not_ready(tenant):
        raise TenantNotReady(tenant.tenant_id)

    monkeypatch.setattr(snowflake_client, "tenant_database", not_ready)
    with pytest.raises(TenantNotReady):
        lambda_handler.guard_context(TENANT_A, {"sub": "u1"})
    assert executed == []


CORE = TENANT_CORE


def _validate_as(tenant, sql):
    return _validate(sql, tenant=tenant)


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
    assert _validate(sql)["error"].startswith("INFORMATION_SCHEMA.DATABASES is not accessible.")
    assert executed == []


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM INFORMATION_SCHEMA.ENABLED_ROLES",
    "SELECT COUNT(*) FROM INFORMATION_SCHEMA.APPLICABLE_ROLES",
    "SELECT COUNT(*) FROM INFORMATION_SCHEMA.OBJECT_PRIVILEGES",
    "SELECT COUNT(*) FROM INFORMATION_SCHEMA.LOAD_HISTORY",
    "SELECT COUNT(*) FROM INFORMATION_SCHEMA.FUNCTIONS",
    "SELECT COUNT(*) FROM INFORMATION_SCHEMA.STAGES",
    "SELECT COUNT(*) FROM information_schema.usage_privileges",
    "SELECT COUNT(*) FROM DB_TENANT_A.INFORMATION_SCHEMA.TABLE_PRIVILEGES",
    "SELECT COUNT(*) FROM CDM_LMS.COURSE WHERE NAME IN (SELECT GRANTEE FROM INFORMATION_SCHEMA.OBJECT_PRIVILEGES)",
    'SELECT COUNT(*) FROM INFORMATION_SCHEMA."tables"',
])
def test_information_schema_views_outside_the_allow_list_are_refused(monkeypatch, executed, sql):
    from tests.tenant_fakes import install_tenants
    install_tenants(monkeypatch, TENANT_A)
    assert "is not accessible" in _validate(sql)["error"], sql
    assert executed == []


@pytest.mark.parametrize("view", ["TABLES", "COLUMNS", "VIEWS", "SCHEMATA", "TABLE_CONSTRAINTS", "tables"])
def test_information_schema_allow_listed_views_run(monkeypatch, executed, view):
    from tests.tenant_fakes import install_tenants
    install_tenants(monkeypatch, TENANT_A)
    assert "error" not in _validate(f"SELECT COUNT(*) FROM INFORMATION_SCHEMA.{view}")
    assert "error" not in _validate(f"SELECT COUNT(*) FROM DB_TENANT_A.INFORMATION_SCHEMA.{view}")


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


@pytest.mark.parametrize("sql", [
    "SELECT r.X FROM CDM_LMS.COURSE c, LATERAL RESULT_SCAN(LAST_QUERY_ID(-1)) r",
    "SELECT r.X FROM CDM_LMS.COURSE c, LATERAL TABLE(RESULT_SCAN(LAST_QUERY_ID(-1))) r",
    "SELECT q.X FROM CDM_LMS.COURSE c, LATERAL INFORMATION_SCHEMA.QUERY_HISTORY() q",
    "SELECT r.X FROM CDM_LMS.COURSE c JOIN LATERAL RESULT_SCAN(LAST_QUERY_ID()) r ON TRUE",
    "SELECT r.X FROM CDM_LMS.COURSE c, LATERAL CDM_LMS.MY_FN(c.ID) r",
    "SELECT r.X FROM CDM_LMS.COURSE c, LATERAL \"split_to_table\"(c.NAME, ',') r",
    "SELECT r.X FROM CDM_LMS.COURSE c, LATERAL TABLE(GENERATOR(ROWCOUNT => 100000000)) r",
    "SELECT r.X FROM TABLE(CDM_LMS.MY_FN(1)) r",
    "SELECT r.X FROM UNNEST(ARRAY_CONSTRUCT(1)) r",
])
@pytest.mark.parametrize("compiled", [False, True])
def test_table_functions_outside_the_allow_list_are_refused_wherever_they_appear(sql, compiled, executed):
    result = _validate(sql, compiled=compiled)
    assert "table function" in result["error"], sql
    assert executed == []


@pytest.mark.parametrize("sql", [
    "SELECT f.VALUE AS v FROM CDM_LMS.COURSE c, LATERAL FLATTEN(input => PARSE_JSON(c.NAME)) f",
    "SELECT f.VALUE AS v FROM CDM_LMS.COURSE c, TABLE(FLATTEN(input => PARSE_JSON(c.NAME))) f",
    "SELECT s.VALUE AS v FROM CDM_LMS.COURSE c, LATERAL SPLIT_TO_TABLE(c.NAME, ',') s",
    "SELECT s.VALUE AS v FROM CDM_LMS.COURSE c, LATERAL TABLE(SPLIT_TO_TABLE(c.NAME, ',')) s",
    "SELECT s.VALUE AS v FROM CDM_LMS.COURSE c, LATERAL STRTOK_SPLIT_TO_TABLE(c.NAME, ',') s",
    "SELECT x.N FROM CDM_LMS.COURSE c, LATERAL (SELECT c.NAME AS N) x",
    "SELECT g.N FROM CDM_LMS.COURSE c, LATERAL TABLE(GENERATOR(ROWCOUNT => 5)) g",
])
def test_allow_listed_table_functions_run(sql, executed):
    assert "error" not in _validate(sql), sql
    assert len(executed) == 1


@pytest.mark.parametrize("sql", [
    "SELECT RESULT_SCAN(LAST_QUERY_ID()) AS r",
    "SELECT LAST_QUERY_ID() AS q",
    "SELECT COUNT(*) FROM CDM_LMS.COURSE WHERE NAME = LAST_QUERY_ID(-2)",
    "SELECT QUERY_HISTORY_BY_USER() AS q",
    "SELECT GET_DDL('table', 'CDM_LMS.COURSE') AS d",
    "SELECT SYSTEM$WHITELIST() AS w",
    "SELECT system$allowlist() AS w",
    "SELECT \"SYSTEM$WHITELIST\"() AS w",
    "SELECT GET_QUERY_OPERATOR_STATS('x') AS s",
    "SELECT EXPLAIN_JSON('x') AS e",
    "SELECT SNOWFLAKE.CORTEX.COMPLETE('m', 'p') AS c",
    "SELECT INFORMATION_SCHEMA.CURRENT_TASK_GRAPHS() AS t",
    "SELECT COUNT(*) FROM CDM_LMS.COURSE WHERE NAME = ACCOUNT_USAGE.F()",
])
@pytest.mark.parametrize("compiled", [False, True])
def test_metadata_and_system_functions_are_refused_anywhere(sql, compiled, executed):
    result = _validate(sql, compiled=compiled)
    assert "is not allowed" in result["error"], sql
    assert executed == []


def test_premium_schema_refusal_lists_learn_and_core_does_not(executed):
    premium = _validate("SELECT COUNT(*) FROM PUBLIC.T")["error"]
    core = _validate_as(CORE, "SELECT COUNT(*) FROM PUBLIC.T")["error"]
    assert "not in the allowed list" in premium and "LEARN" in premium
    assert "not in the allowed list" in core and "LEARN" not in core


_PART = "WITH DS AS (SELECT * FROM CDM_LMS.PERSON p JOIN CDM_LMS.COURSE c ON TRUE) SELECT {projection} FROM DS GROUP BY 1"
_ROLE_PEOPLE = "INSTITUTION_ROLE AS role, COUNT(DISTINCT EMAIL) AS people"


def _governed(outer, compiled_sql):
    """(the statement, the Spliced it claims to be): outer with governed_1 hoisted from compiled_sql, bypassing
    the compiler so a part can hold what a test needs."""
    import sqlglot
    import sqlglot.expressions as exp
    from semantic_layer import governed_sql
    from semantic_layer.contract import CompiledQuery, Provenance, QueryContract

    compiled = CompiledQuery(sql=compiled_sql, provenance=Provenance(datasets=[], metrics=[], measures=[]))
    ctes = governed_sql._hoist("governed_1", compiled)
    tree = sqlglot.parse_one(outer, dialect="snowflake")
    outer_with = tree.args.get("with")
    tree.set("with", exp.With(expressions=ctes + (list(outer_with.expressions) if outer_with else [])))
    sql = tree.sql(dialect="snowflake")
    part = governed_sql.Part(contract=QueryContract(measures=["dataset.x.v1:n"]), compiled=compiled, outputs={})
    helpers = frozenset(c.alias for c in ctes[:-1])
    return sql, governed_sql.SplicedSql(sql=sql, parts={"governed_1": part}, part_names=frozenset({"governed_1"}),
                                     helper_names=helpers)


def _spliced(outer, projection=_ROLE_PEOPLE):
    return _governed(outer, _PART.format(projection=projection))


def _validate_governed(statement, **kwargs):
    sql, spliced = statement
    return _validate(sql, spliced=spliced, **kwargs)


def test_governed_bodies_follow_the_compiled_rules_and_the_rest_the_freehand_rules(executed):
    statement = _spliced("SELECT role, people FROM governed_1")
    assert "Star projections" in _validate(statement[0])["error"]
    assert "error" not in _validate_governed(statement)
    assert len(executed) == 1


def test_a_governed_parts_projection_is_its_outermost_projection(executed):
    statement = _spliced("SELECT role FROM governed_1", projection="EMAIL AS role, COUNT(*) AS people")
    assert "personally identifiable" in _validate_governed(statement)["error"]
    assert "error" not in _validate_governed(statement, allow_identity=True)


def test_extra_pii_applies_to_a_governed_parts_projection(executed):
    statement = _spliced("SELECT role FROM governed_1", projection="CREATOR_NAME AS role, COUNT(*) AS people")
    assert "error" not in _validate_governed(statement)
    assert "personally identifiable" in _validate_governed(statement, extra_pii={"CREATOR_NAME"})["error"]


@pytest.mark.parametrize("outer", [
    "SELECT g.role, p.EMAIL FROM governed_1 g JOIN CDM_LMS.PERSON p ON TRUE",
    "SELECT g.role, p.CREATOR_NAME FROM governed_1 g JOIN CDM_LMS.PERSON p ON TRUE",
    "SELECT * FROM governed_1",
    "SELECT $1 FROM governed_1",
    "SELECT a.x FROM governed_1 g JOIN CDM_LMS.PERSON AS a (x) ON TRUE",
    "SELECT role FROM governed_1, CDM_LMS.ROLE",
    "SELECT role FROM governed_1 JOIN CDM_LMS.PERSON ON TRUE LIMIT 5000",
    "SELECT role FROM governed_1 JOIN OTHER_DB.CDM_LMS.PERSON ON TRUE",
    "SELECT role FROM governed_1 JOIN PERSON ON TRUE",
    "SELECT role FROM governed_1 JOIN PUBLIC.T ON TRUE",
    "SELECT role, RESULT_SCAN(LAST_QUERY_ID()) AS r FROM governed_1",
    "SELECT EMAIL FROM governed_1__DS",
    "SELECT d.role FROM governed_1 g JOIN governed_1__DS d ON TRUE",
])
def test_freehand_and_structural_rules_still_apply_outside_governed_bodies(outer, executed):
    assert "error" in _validate_governed(_spliced(outer)), outer
    assert executed == []


def test_whole_row_check_ignores_names_inside_governed_bodies(executed):
    statement = _spliced("SELECT course, governed_1.people FROM governed_1",
                         projection="INSTITUTION_ROLE AS course, COUNT(*) AS people")
    assert "error" not in _validate_governed(statement)


@pytest.mark.parametrize("identity", [False, True])
def test_a_whole_row_reference_to_a_governed_part_is_refused(identity, executed):
    statement = _spliced("SELECT governed_1 FROM governed_1")
    assert _validate_governed(statement, allow_identity=identity)["error"] == snowflake_client.WHOLE_ROW_REFUSAL


@pytest.mark.parametrize("identity", [False, True])
def test_a_governed_part_whose_projection_has_a_star_is_refused(identity, executed):
    statement = _governed("SELECT g.BIRTH_DATE FROM governed_1 g", "SELECT * FROM CDM_LMS.PERSON")
    assert "name its columns" in _validate_governed(statement, allow_identity=identity)["error"]
    assert executed == []


def test_an_outer_read_of_a_helper_no_part_reads_is_refused(executed):
    statement = _governed("SELECT d.INSTITUTION_ROLE FROM governed_1 g JOIN governed_1__DS d ON TRUE",
                          "WITH DS AS (SELECT INSTITUTION_ROLE FROM CDM_LMS.PERSON) SELECT 1 AS n")
    assert "internal to a governed part" in _validate_governed(statement, allow_identity=True)["error"]
    assert executed == []


def test_outer_columns_read_from_a_governed_part_resolve_as_derived_outputs(executed):
    statement = _spliced("SELECT g.role, g.people, c.NAME FROM governed_1 g JOIN CDM_LMS.COURSE c ON c.NAME = g.role")
    assert "error" not in _validate_governed(statement)


@pytest.mark.parametrize("sql", [
    "WITH governed_1 AS (SELECT 1 AS n) SELECT n FROM governed_1, (WITH governed_1 AS (SELECT 2 AS n) SELECT n FROM governed_1)",
    "SELECT n FROM (WITH governed_1 AS (SELECT 1 AS n) SELECT n FROM governed_1)",
    "WITH governed_1__DS AS (SELECT 1 AS n) SELECT n FROM governed_1__DS",
])
def test_governed_ctes_must_each_be_defined_once_at_the_top_level(sql, executed):
    assert "error" in _validate(sql, spliced=_spliced("SELECT role FROM governed_1")[1])
    assert executed == []


def test_governed_ctes_are_not_accepted_as_compiled_sql(executed):
    sql, _ = _spliced("SELECT role FROM governed_1")
    ctx = guard_ctx()
    assert run(sql, ctx, compiled_mode("SELECT role FROM CDM_LMS.PERSON"))["error"] == snowflake_client.COMPILED_MISMATCH
    assert executed == []


PROBE = ("WITH governed_1 AS (SELECT n FROM (SELECT p.EMAIL AS n FROM CDM_LMS.PERSON p) t)"
         " SELECT n FROM governed_1")


def test_a_governed_name_over_a_body_the_splice_did_not_produce_is_refused_for_a_viewer(executed):
    _, spliced = _spliced("SELECT role FROM governed_1")
    assert "error" in _validate(PROBE, spliced=spliced)
    assert executed == []


def test_a_tampered_helper_body_is_refused(executed):
    sql, spliced = _spliced("SELECT role, people FROM governed_1")
    tampered = sql.replace("JOIN CDM_LMS.COURSE AS c ON TRUE", "JOIN CDM_LMS.COURSE AS c ON c.ID = p.ID")
    assert tampered != sql
    assert "does not match" in _validate(tampered, spliced=spliced)["error"]
    assert executed == []


def test_a_tampered_part_body_is_refused(executed):
    sql, spliced = _spliced("SELECT role, people FROM governed_1")
    tampered = sql.replace("COUNT(DISTINCT EMAIL) AS people", "COUNT(EMAIL) AS people")
    assert tampered != sql
    assert "does not match" in _validate(tampered, spliced=spliced)["error"]


def test_a_splice_part_missing_from_the_statement_is_refused(executed):
    _, spliced = _spliced("SELECT role FROM governed_1")
    assert "error" in _validate("SELECT COUNT(*) AS n FROM CDM_LMS.PERSON", spliced=spliced)
    assert executed == []


def test_a_governed_cte_the_splice_does_not_have_is_refused(executed):
    sql, spliced = _spliced("WITH governed_2 AS (SELECT COUNT(*) AS n FROM CDM_LMS.PERSON)"
                            " SELECT g.role, h.n FROM governed_1 g JOIN governed_2 h ON TRUE")
    assert "error" in _validate(sql, spliced=spliced)
    assert executed == []


def test_a_column_list_that_renames_a_parts_outputs_is_refused(executed):
    sql, spliced = _spliced("SELECT role, people FROM governed_1")
    tampered = sql.replace("governed_1 AS (", "governed_1 (people, role) AS (")
    assert tampered != sql
    assert "does not match" in _validate(tampered, spliced=spliced)["error"]
