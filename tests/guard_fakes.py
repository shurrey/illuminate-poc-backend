"""Helpers for driving the SQL guard (validate, then execute) from tests."""
from semantic_layer.contract import CompiledQuery, Provenance

import snowflake_client
from snowflake_client import Compiled, Freehand, GuardContext, Refusal
from tests.semantic_fixtures import catalog as fixture_catalog
from tests.tenant_fakes import TENANT_A

DATABASE = "MY_DB"


def guard_ctx(tenant=TENANT_A, *, allow_identity=False, extra_pii=(), catalog=None, database=DATABASE) -> GuardContext:
    """extra_pii is used as given rather than derived from catalog, so a test can name any column."""
    return GuardContext(tenant=tenant, allow_identity=allow_identity, extra_pii=frozenset(n.upper() for n in extra_pii),
                        catalog=catalog if catalog is not None else fixture_catalog(), database=database)


def compiled(sql: str) -> Compiled:
    """Compiled mode for SQL that did not come from the compiler."""
    return Compiled(CompiledQuery(sql=sql, provenance=Provenance(datasets=[], metrics=[], measures=[])))


def run(sql, ctx: GuardContext, mode=None) -> dict:
    """validate then execute, as a dict: columns, rows and truncated; or error, with warehouse_error when it ran."""
    checked = snowflake_client.validate(sql, ctx, mode or Freehand())
    if isinstance(checked, Refusal):
        return {"error": checked.message}
    result = snowflake_client.execute(checked, ctx)
    if result.error is not None:
        return {"error": result.error, "warehouse_error": True}
    return {"columns": result.columns, "rows": result.rows, "truncated": result.truncated}


def guard(sql, *, tenant=TENANT_A, compiled_sql=False, allow_identity=False, extra_pii=(), database=DATABASE) -> dict:
    """run() under a context built from the arguments; compiled_sql treats sql as the compiler's output."""
    ctx = guard_ctx(tenant, allow_identity=allow_identity, extra_pii=extra_pii, database=database)
    return run(sql, ctx, compiled(sql) if compiled_sql else Freehand())
