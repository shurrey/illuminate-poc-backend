"""Running, validating and describing SQL for a person or the chat model, always through the execution guard.

Callers pass a GuardContext built from the token. Caller SQL is only ever validated as Freehand or Spliced,
never Compiled. Errors come back as {"error": message}; "warehouse": True marks one the warehouse raised.
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional

from pydantic import ValidationError

import snowflake_client
from licenses import LEARN_REFUSAL, allows_learn
from snowflake_client import Freehand, GuardContext, Refusal, Result, Spliced, Validated, validate

from .contract import QueryContract
from .dictionary import describe_table
from .governed_sql import SpliceError, SplicedSql, label
from .overlays import overlays_used

_CONTRACT_FIELDS = set(QueryContract.model_fields)

Execute = Callable[[Validated, GuardContext], Result]
IdentityAudit = Callable[[str, object], None]


def contract_of(part: dict) -> QueryContract:
    return QueryContract(**{k: v for k, v in part.items() if k in _CONTRACT_FIELDS})


def parse_parts(governed) -> dict[str, QueryContract]:
    """Raises SpliceError for a malformed `governed` input, naming the part at fault."""
    if not isinstance(governed, dict):
        raise SpliceError("governed must be an object mapping governed_1, governed_2, ... to query_semantic inputs")
    contracts = {}
    for name, part in governed.items():
        if not isinstance(part, dict):
            raise SpliceError(f"{name}: a governed part takes the same fields as query_semantic")
        try:
            contracts[name] = contract_of(part)
        except ValidationError as e:
            raise SpliceError(f"{name}: {e}") from e
    return contracts


def _check(ctx: GuardContext, sql: str, governed: dict | None) -> Validated | Refusal:
    if governed:
        try:
            return validate(None, ctx, Spliced(sql, parse_parts(governed)))
        except SpliceError as e:
            return Refusal(str(e))
    return validate(sql, ctx, Freehand())


def validate_sql(ctx: GuardContext, *, sql: str, governed: dict | None = None) -> dict:
    """{"ok": True, "governance_preview"} when the guard accepts the statement for this caller, else
    {"ok": False, "error"}. The preview is "partial" with governed parts, else "none"; a run's label can be lower.
    No warehouse call."""
    checked = _check(ctx, sql, governed)
    if isinstance(checked, Refusal):
        return {"ok": False, "error": checked.message}
    return {"ok": True, "governance_preview": "partial" if governed else "none"}


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _governed_provenance(spliced: SplicedSql, columns: list[str], reason: str, ctx: GuardContext,
                         overlays: Iterable) -> dict:
    labels = label(spliced, columns, reason=reason)
    compiled = [part.compiled.provenance for part in spliced.parts.values()]
    overlays = list(overlays)
    return {
        "governed": labels["governance"] == "full", **labels, "reason": reason,
        "metrics": _unique(m for p in compiled for m in p.metrics),
        "measures": _unique(m for p in compiled for m in p.measures),
        "datasets": _unique(d for p in compiled for d in p.datasets),
        "overlays": _unique(o for p in compiled for o in overlays_used(p, ctx.catalog, overlays)),
    }


def run_sql(ctx: GuardContext, *, sql: str, governed: dict | None, reason: str, overlays: Iterable = (),
            execute: Optional[Execute] = None, identity_audit: Optional[IdentityAudit] = None) -> dict:
    """Validate, run and label one statement.

    Returns {columns, rows, truncated, provenance, sql, executed_sql?, governed_parts?}, or {"error"}.
    identity_audit is called after the guard accepts, only for identity-capable callers.
    """
    checked = _check(ctx, sql, governed)
    if isinstance(checked, Refusal):
        return {"error": checked.message}
    spliced = checked.spliced
    if ctx.allow_identity and identity_audit:
        for part in (spliced.parts.values() if spliced else ()):
            identity_audit("governed", part.contract)
        identity_audit("freehand", None)
    result = (execute or snowflake_client.execute)(checked, ctx)
    if result.error is not None:
        return {"error": result.error} if result.refused else {"error": result.error, "warehouse": True}

    columns = result.columns
    out = {"columns": columns, "rows": result.rows, "sql": sql}
    if spliced:
        provenance = _governed_provenance(spliced, columns, reason, ctx, overlays)
        out["executed_sql"] = spliced.sql
        out["governed_parts"] = {
            name: part.contract.model_dump(mode="json", exclude_unset=True) for name, part in spliced.parts.items()}
    else:
        provenance = {"governed": False, "governance": "none", "rows": "modified", "reason": reason,
                      "columns": {c: {"governed": False, "reason": reason} for c in columns}}
    # The warehouse returns at most MAX_ROWS; a cut result is not the governed row set.
    if result.truncated:
        provenance |= {"truncated": True, "rows": "modified", "governed": False,
                       "governance": "partial" if provenance["governance"] == "full" else provenance["governance"]}
    out["truncated"] = result.truncated
    out["provenance"] = provenance
    return out


def describe(ctx: GuardContext, schema: str, table: str,
             lookup: Callable[[str, str], Optional[list[dict]]] = describe_table) -> dict:
    """{"table", "columns"}, or {"error", "status"} with 403 for LEARN on a Core tenant and 404 for an unknown table."""
    if schema.upper() == "LEARN" and not allows_learn(ctx.tenant):
        return {"error": LEARN_REFUSAL, "status": 403}
    columns = lookup(schema, table)
    if not columns:
        return {"error": f"No dictionary entry for {schema.upper()}.{table.upper()}", "status": 404}
    return {"table": f"{schema.upper()}.{table.upper()}", "columns": columns}
