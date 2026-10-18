"""Tools the chat model calls, and the artifacts their results become for the client.

Tool content goes back to the model (row-capped); artifacts carry every row plus the query
contract, SQL and provenance so the client can render, re-run or pin the result.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from pydantic import ValidationError

import snowflake_client
from licenses import allows_learn
from snowflake_client import Compiled, GuardContext, Refusal, Result, Validated, validate
from tenancy import Tenant

from .compiler import CompileError, compile_query, named_result
from .dictionary import describe_table
from .overlays import overlays_used
from .contract import QueryContract
from .schema import Catalog
from .search import search_catalog
from .sql_service import describe, run_sql

MODEL_ROW_LIMIT = 200
MIN_REASON_LENGTH = 15
_GOVERNED_TOOLS = {"search_catalog", "query_semantic"}
_CONTRACT_FIELDS = set(QueryContract.model_fields)

_FILTER_SCHEMA = {
    "type": "object",
    "properties": {
        "dimension": {"type": "string"},
        "op": {"type": "string", "enum": ["eq", "neq", "in", "not_in", "gt", "gte", "lt", "lte",
                                          "between", "is_null", "not_null", "contains"]},
        "values": {"type": "array", "items": {"type": ["string", "number", "boolean"]}},
    },
    "required": ["dimension", "op"],
}

_CONTRACT_PROPERTIES = {
    "metrics": {"type": "array", "items": {"type": "string"}},
    "measures": {"type": "array", "items": {"type": "string"}},
    "dimensions": {"type": "array", "items": {"type": "string"}},
    "filters": {"type": "array", "items": _FILTER_SCHEMA},
    "time_range": {"type": "object", "properties": {
        "dimension": {"type": "string"},
        "start": {"type": "string", "description": "YYYY-MM-DD"},
        "end": {"type": "string", "description": "YYYY-MM-DD"},
    }, "required": ["dimension"]},
    "order_by": {"type": "array", "items": {"type": "object", "properties": {
        "field": {"type": "string"}, "direction": {"type": "string", "enum": ["asc", "desc"]},
    }, "required": ["field"]}},
    "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
}

_EXECUTE_SQL_TEXT = (
    "Run read-only SQL against the CDM schemas{learn} when query_semantic alone cannot answer. "
    "For a question the catalog covers only in part, put each governed part in `governed` as governed_1, "
    "governed_2, ... (each shaped like query_semantic's input) and write SQL that selects from or joins them "
    "by name; never re-derive a governed metric or measure by hand. Each result column is labelled governed or "
    "ungoverned; tell the user which are governed. `reason` must say why the catalog alone does not answer."
)

SPECS = [
    {
        "name": "search_catalog",
        "description": (
            "Find governed metrics, measures and dimensions that match a question. "
            "Call this first; use the ids it returns in query_semantic."
        ),
        "inputSchema": {"json": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        }},
    },
    {
        "name": "query_semantic",
        "description": (
            "Run a governed query over the semantic layer. Name metrics (metric ids) or measures "
            "('<dataset id>:<measure>'), optional dimensions (a dimension name, '<name>__<grain>' for "
            "time grains, or '<dataset id>:<name>'), filters on dimensions, an optional time range, "
            "ordering and a limit (max 1000). Optionally ask for a chart of the result."
        ),
        "inputSchema": {"json": {
            "type": "object",
            "properties": {
                **_CONTRACT_PROPERTIES,
                "title": {"type": "string", "description": "Short title for the result shown to the user"},
                "chart": {"type": "object", "properties": {
                    "type": {"type": "string", "enum": ["bar", "line", "pie", "scatter"]},
                    "x": {"type": "string", "description": "Result column for the x axis"},
                    "y": {"type": "string", "description": "Result column for the y axis"},
                }, "required": ["type", "x", "y"]},
            },
        }},
    },
    {
        "name": "describe_cdm_table",
        "description": (
            "List the columns of a raw CDM table (schema like CDM_LMS). Only for writing execute_sql "
            "when no governed metric, measure or dimension fits."
        ),
        "inputSchema": {"json": {
            "type": "object",
            "properties": {"schema": {"type": "string"}, "table": {"type": "string"}},
            "required": ["schema", "table"],
        }},
    },
    {
        "name": "execute_sql",
        "description": _EXECUTE_SQL_TEXT.format(learn=""),
        "inputSchema": {"json": {
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "reason": {"type": "string", "description": "Why the catalog alone does not answer the question"},
                "title": {"type": "string"},
                "governed": {
                    "type": "object",
                    "description": ("Governed parts by name (governed_1, governed_2, ...) that the SQL selects from. "
                                    "A part returns all its rows unless it sets a limit, which needs an order_by."),
                    "additionalProperties": {"type": "object", "properties": _CONTRACT_PROPERTIES},
                },
            },
            "required": ["sql", "reason"],
        }},
    },
]


_LEARN_SPEC_TEXT = {
    "describe_cdm_table": (
        "List the columns of a raw CDM table (schema like CDM_LMS) or a LEARN table (schema LEARN). "
        "Only for writing execute_sql when no governed metric, measure or dimension fits."
    ),
    "execute_sql": _EXECUTE_SQL_TEXT.format(
        learn=" and the LEARN schema (name every table with its schema, e.g. LEARN.<table> or <database>.LEARN.<table>)"),
}


def _with_learn(spec: dict) -> dict:
    text = _LEARN_SPEC_TEXT.get(spec["name"])
    return {**spec, "description": text} if text else spec


@dataclass
class ToolResult:
    content: dict
    artifacts: list[dict] = field(default_factory=list)


def _model_error(message: str) -> str:
    """A warehouse error as the model sees it: without object or role names."""
    message = re.sub(r"Object '[^']*'", "An object", message)
    return re.sub(r",?\s*role \S+", "", message)


def _contract(tool_input: dict) -> QueryContract:
    return QueryContract(**{k: v for k, v in tool_input.items() if k in _CONTRACT_FIELDS})


def _artifact(artifact_type: str, title: str, data, **extra) -> dict:
    return {"id": uuid.uuid4().hex, "type": artifact_type, "title": title, "data": data, **extra}


class ChatTools:
    def __init__(self, catalog: Catalog, database: str, *, tenant: Tenant,
                 execute: Optional[Callable[[Validated, GuardContext], Result]] = None,
                 describe: Callable[[str, str], Optional[list[dict]]] = describe_table,
                 overlays: Iterable = (), allow_identity: bool = False,
                 identity_audit: Optional[Callable[[str, object], None]] = None):
        """execute: runs a statement the guard accepted; defaults to the tenant's warehouse. overlays: applied to catalog, reported in provenance.
        allow_identity: the caller's role may see PII columns (governed and freehand).
        identity_audit: for an identity-capable query, called with ("governed", contract) before a governed query
        runs, and once the guard accepts execute_sql with ("governed", contract) per part, then ("freehand", None)."""
        self.catalog, self.database, self.tenant, self.describe = catalog, database, tenant, describe
        self.execute = execute or (lambda v, ctx: snowflake_client.execute(v, ctx))
        self.overlays = list(overlays)
        self.allow_identity, self.identity_audit = allow_identity, identity_audit
        self.guard = GuardContext.of(tenant, catalog, allow_identity=allow_identity)

    def _run(self, checked: Validated | Refusal) -> dict:
        """The guard's refusal, or the warehouse result as a dict of columns, rows and truncated."""
        if isinstance(checked, Refusal):
            return {"error": checked.message}
        result = self.execute(checked, self.guard)
        if result.error is not None:
            return {"error": _model_error(result.error)}
        return {"columns": result.columns, "rows": result.rows, "truncated": result.truncated}

    @property
    def specs(self) -> list[dict]:
        if not allows_learn(self.tenant):
            return SPECS
        return [_with_learn(spec) for spec in SPECS]

    def dispatch(self, name: str, tool_input: dict, called: Iterable[str] = ()) -> ToolResult:
        """called: the tools already run earlier in this turn."""
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return ToolResult({"error": f"Unknown tool: {name}"})
        if name == "execute_sql" and not _GOVERNED_TOOLS & set(called):
            return ToolResult({"error": "Call search_catalog first; execute_sql is only for questions the catalog cannot answer"})
        return handler(tool_input)

    def _tool_search_catalog(self, tool_input: dict) -> ToolResult:
        return ToolResult({"matches": search_catalog(tool_input.get("question", ""), self.catalog)})

    def _tool_query_semantic(self, tool_input: dict) -> ToolResult:
        try:
            contract = _contract(tool_input)
            compiled = compile_query(contract, self.catalog, self.database, allow_identity=self.allow_identity)
        except (ValidationError, CompileError) as e:
            return ToolResult({"error": str(e)})

        if self.allow_identity and self.identity_audit:
            self.identity_audit("governed", contract)
        result = self._run(validate(compiled.sql, self.guard, Compiled(compiled)))
        if "error" in result:
            return ToolResult({"error": result["error"]})

        result = named_result(result)
        rows, columns = result["rows"], result["columns"]
        compiled.provenance.overlays = overlays_used(compiled.provenance, self.catalog, self.overlays)
        provenance = compiled.provenance.model_dump()
        query = contract.model_dump(mode="json", exclude_defaults=True)
        title = tool_input.get("title") or "Query result"
        common = {"kind": "semantic", "query": query, "sql": compiled.sql, "provenance": provenance}
        content = {
            "columns": columns, "rows": rows[:MODEL_ROW_LIMIT], "row_count": len(rows),
            "truncated": len(rows) > MODEL_ROW_LIMIT, "provenance": provenance,
        }
        artifacts = [
            _artifact("table", title, {"columns": columns, "rows": rows}, **common),
            _artifact("sql", title, compiled.sql, **common),
        ]
        chart = tool_input.get("chart")
        if chart:
            x, y = (str(chart.get(k, "")).lower() for k in ("x", "y"))
            missing = [c for c in (x, y) if c not in columns]
            if missing:
                content["chart_error"] = f"chart columns not in the result: {missing}; result columns are {columns}"
            else:
                artifacts.append(_artifact("chart", title, {
                    "chart_type": chart["type"], "title": title, "x_axis": x, "y_axis": y, "data": rows,
                }, **common))
        return ToolResult(content, artifacts)

    def _tool_describe_cdm_table(self, tool_input: dict) -> ToolResult:
        out = describe(self.guard, tool_input.get("schema", ""), tool_input.get("table", ""), self.describe)
        return ToolResult({"error": out["error"]} if "error" in out else out)

    def _tool_execute_sql(self, tool_input: dict) -> ToolResult:
        reason = (tool_input.get("reason") or "").strip()
        if len(reason) < MIN_REASON_LENGTH:
            return ToolResult({"error": "execute_sql needs a reason (a sentence) saying why no governed definition fits"})
        sql = tool_input.get("sql", "")
        result = run_sql(self.guard, sql=sql, governed=tool_input.get("governed") or None, reason=reason,
                         overlays=self.overlays, execute=self.execute, identity_audit=self.identity_audit)
        if "error" in result:
            return ToolResult({"error": _model_error(result["error"]) if result.get("warehouse") else result["error"]})
        rows, columns, provenance, cut = result["rows"], result["columns"], result["provenance"], result["truncated"]
        extra = {"kind": "sql", "sql": sql}
        if "executed_sql" in result:
            extra |= {"executed_sql": result["executed_sql"], "governed_parts": result["governed_parts"]}
        if cut:
            extra["truncated"] = True
        title = tool_input.get("title") or {
            "full": "Governed query result", "partial": "Partly governed query result",
        }.get(provenance["governance"], "Ungoverned query result")
        content = {
            "columns": columns, "rows": rows[:MODEL_ROW_LIMIT], "row_count": len(rows),
            "truncated": cut or len(rows) > MODEL_ROW_LIMIT, "provenance": provenance,
        }
        if cut:
            from snowflake_client import MAX_ROWS
            content["note"] = (f"The warehouse returned only the first {MAX_ROWS} rows; tell the user the result "
                               f"was cut to {MAX_ROWS} rows.")
        return ToolResult(content, [
            _artifact("table", title, {"columns": columns, "rows": rows}, provenance=provenance, **extra),
            _artifact("sql", title, sql, provenance=provenance, **extra),
        ])
