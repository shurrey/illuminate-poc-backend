import dataclasses

import pytest

import snowflake_client
from semantic_layer.compiler import compile_query
from semantic_layer.contract import QueryContract
from semantic_layer.governed_sql import splice
from snowflake_client import Compiled, Freehand, Refusal, Result, Spliced, Validated, execute, validate
from tests.guard_fakes import DATABASE, compiled, guard_ctx
from tests.semantic_fixtures import COURSES, ENROLLMENTS, catalog
from tests.tenant_fakes import TENANT_B

CATALOG = catalog(ENROLLMENTS, COURSES)
PEOPLE = QueryContract(measures=["dataset.enrollments.v1:people"], dimensions=["course_role"])
OUTER = "SELECT course_role, people FROM governed_1"


@pytest.fixture
def no_warehouse(monkeypatch):
    def unreachable(*args, **kwargs):
        pytest.fail("reached the warehouse")

    for name in ("query_sql", "_run", "_execute", "get_connection", "tenant_database", "_load_credentials"):
        monkeypatch.setattr(snowflake_client, name, unreachable)


@pytest.fixture
def executed(monkeypatch):
    calls = []
    monkeypatch.setattr(snowflake_client, "query_sql",
                        lambda sql, params=None, **k: calls.append(sql) or {"columns": ["N"], "rows": [{"N": 1}]})
    return calls


def _ctx(**kwargs):
    return guard_ctx(catalog=CATALOG, **kwargs)


def test_validate_makes_no_warehouse_call_in_any_mode(no_warehouse):
    ctx = _ctx()
    governed = compile_query(PEOPLE, CATALOG, DATABASE)
    assert isinstance(validate("SELECT COUNT(*) AS n FROM MY_DB.CDM_LMS.COURSE", ctx, Freehand()), Validated)
    assert isinstance(validate(governed.sql, ctx, Compiled(governed)), Validated)
    assert isinstance(validate(None, ctx, Spliced(OUTER, {"governed_1": PEOPLE})), Validated)
    assert isinstance(validate("SELECT * FROM CDM_LMS.PERSON", ctx, Freehand()), Refusal)


def test_a_validated_statement_carries_its_sql_parsed_statement_and_mode():
    mode = Freehand()
    v = validate("  SELECT COUNT(*) AS n FROM CDM_LMS.COURSE  ", _ctx(), mode)
    assert v.sql == "SELECT COUNT(*) AS n FROM CDM_LMS.COURSE"
    assert v.mode is mode and v.spliced is None
    assert v.stmt.sql(dialect="snowflake") == v.sql


def test_compiled_mode_refuses_sql_that_differs_from_the_compiled_sql():
    governed = compile_query(PEOPLE, CATALOG, DATABASE)
    edited = governed.sql.replace("COUNT(DISTINCT", "SUM(DISTINCT", 1)
    assert edited != governed.sql
    refusal = validate(edited, _ctx(), Compiled(governed))
    assert isinstance(refusal, Refusal) and "does not match" in refusal.message


def test_compiled_mode_compares_the_parsed_statement_not_its_layout():
    mode = compiled("SELECT course_role AS role FROM CDM_LMS.COURSE")
    assert isinstance(validate("select   COURSE_ROLE as ROLE\nfrom cdm_lms.course", _ctx(), mode), Validated)
    assert isinstance(validate('SELECT "course_role" AS role FROM CDM_LMS.COURSE', _ctx(), mode), Refusal)


def test_spliced_mode_splices_from_the_contracts_and_ignores_caller_sql(executed):
    ctx = _ctx()
    expected = splice(OUTER, {"governed_1": PEOPLE}, CATALOG, DATABASE, allow_identity=False)
    v = validate("SELECT p.EMAIL FROM CDM_LMS.PERSON p", ctx, Spliced(OUTER, {"governed_1": PEOPLE}))
    assert isinstance(v, Validated)
    assert v.sql == expected.sql.strip() and v.spliced.sql == expected.sql
    assert v.spliced.part_names == frozenset({"governed_1"})
    execute(v, ctx)
    assert executed == [expected.sql.strip()]


def test_spliced_mode_compiles_with_the_contexts_identity_rights():
    email = {"governed_1": QueryContract(measures=["dataset.enrollments.v1:people"], dimensions=["email"])}
    refusal = validate(None, _ctx(), Spliced("SELECT email FROM governed_1", email))
    assert isinstance(refusal, Refusal) and "governed_1" in refusal.message
    assert isinstance(validate(None, _ctx(allow_identity=True), Spliced("SELECT email FROM governed_1", email)),
                      Validated)


def test_a_tampered_stored_contract_is_still_checked():
    for contract in (QueryContract(metrics=["metric.nope.v1"]),
                     QueryContract(measures=["dataset.enrollments.v1:people"], dimensions=["email"])):
        refusal = validate(None, _ctx(), Spliced("SELECT * FROM governed_1", {"governed_1": contract}))
        assert isinstance(refusal, Refusal) and refusal.message.startswith("governed_1:")


def test_spliced_mode_still_applies_the_freehand_rules_to_the_outer_sql():
    refusal = validate(None, _ctx(), Spliced("SELECT * FROM governed_1", {"governed_1": PEOPLE}))
    assert isinstance(refusal, Refusal) and "Star projections" in refusal.message


def test_execute_runs_validated_sql_and_returns_a_result(executed):
    ctx = _ctx()
    result = execute(validate("SELECT COUNT(*) AS n FROM CDM_LMS.COURSE", ctx, Freehand()), ctx)
    assert result == Result(columns=["N"], rows=[{"N": 1}], truncated=False, error=None)
    assert executed == ["SELECT COUNT(*) AS n FROM CDM_LMS.COURSE"]


def test_execute_refuses_anything_validate_did_not_return(executed):
    ctx = _ctx()
    v = validate("SELECT COUNT(*) AS n FROM CDM_LMS.COURSE", ctx, Freehand())
    forged = Validated(sql="SELECT EMAIL FROM CDM_LMS.PERSON", stmt=v.stmt, mode=Freehand(), ctx=ctx)
    for bad in (forged, dataclasses.replace(v, sql="SELECT EMAIL FROM CDM_LMS.PERSON"),
                validate("SELECT * FROM CDM_LMS.PERSON", ctx, Freehand()), "SELECT 1", None):
        with pytest.raises(TypeError):
            execute(bad, ctx)
    assert executed == []


def test_execute_refuses_a_validated_statement_under_another_context(executed):
    v = validate("SELECT COUNT(*) AS n FROM CDM_LMS.COURSE", _ctx(), Freehand())
    with pytest.raises(TypeError):
        execute(v, guard_ctx(TENANT_B, catalog=CATALOG))
    assert executed == []
