import pytest

import snowflake_client
from semantic_layer import sql_service
from semantic_layer.contract import QueryContract
from snowflake_client import Compiled, Freehand, Refusal, Result, Spliced
from tests.guard_fakes import guard_ctx
from tests.semantic_fixtures import COURSES, ENROLLMENTS, catalog
from tests.tenant_fakes import TENANT_A

CATALOG = catalog(ENROLLMENTS, COURSES)
PART = {"measures": ["dataset.enrollments.v1:people"], "dimensions": ["course_role"]}
OUTER = "SELECT course_role, people FROM governed_1"


def _ctx(**kw):
    return guard_ctx(catalog=CATALOG, **kw)


def test_caller_sql_is_never_validated_as_compiled(monkeypatch):
    modes = []
    real = snowflake_client.validate
    monkeypatch.setattr(sql_service, "validate", lambda sql, ctx, mode: modes.append(mode) or real(sql, ctx, mode))
    ran = lambda v, ctx: Result(columns=["N"], rows=[{"N": 1}])
    sql_service.run_sql(_ctx(), sql="SELECT COUNT(*) AS n FROM CDM_LMS.COURSE", governed=None, reason="r", execute=ran)
    sql_service.run_sql(_ctx(), sql=OUTER, governed={"governed_1": PART}, reason="r", execute=ran)
    sql_service.validate_sql(_ctx(), sql=OUTER, governed={"governed_1": PART})
    assert [type(m) for m in modes] == [Freehand, Spliced, Spliced]
    assert not any(isinstance(m, Compiled) for m in modes)


def test_spliced_mode_refuses_a_part_that_is_not_a_contract():
    refusal = snowflake_client.validate(None, _ctx(), Spliced(OUTER, {"governed_1": PART}))
    assert isinstance(refusal, Refusal) and "governed_1" in refusal.message


def test_the_guard_context_database_is_the_tenants_own(monkeypatch):
    monkeypatch.setattr(snowflake_client, "tenant_database", lambda tenant: "TENANT_DB")
    ctx = snowflake_client.GuardContext.of(TENANT_A, CATALOG, allow_identity=False)
    assert ctx.database == "TENANT_DB"
    with pytest.raises(TypeError):
        snowflake_client.GuardContext.of(TENANT_A, CATALOG, "OTHER_DB", allow_identity=False)


def test_identity_audit_runs_after_acceptance_only_for_identity_callers():
    calls = []
    audit = lambda kind, subject: calls.append(kind)
    ran = lambda v, ctx: Result(columns=["course_role", "people"], rows=[])
    sql_service.run_sql(_ctx(), sql=OUTER, governed={"governed_1": PART}, reason="r", execute=ran, identity_audit=audit)
    assert calls == []
    sql_service.run_sql(_ctx(allow_identity=True), sql="SELECT * FROM CDM_LMS.PERSON", governed=None, reason="r",
                        execute=ran, identity_audit=audit)
    assert calls == []
    sql_service.run_sql(_ctx(allow_identity=True), sql=OUTER, governed={"governed_1": PART}, reason="r",
                        execute=ran, identity_audit=audit)
    assert calls == ["governed", "freehand"]
