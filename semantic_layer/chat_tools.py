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

from licenses import LEARN_REFUSAL, allows_learn
from tenancy import Tenant

from .compiler import CompileError, compile_query, named_result
from .dictionary import describe_table
from .governed_sql import SpliceError, Spliced, label, splice
from .overlays import overlays_used
from .contract import QueryContract
from .schema import Catalog
from .search import search_catalog

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


def _model_error(result: dict) -> str:
    """The error the model sees: warehouse messages without object or role names."""
    message = result["error"]
    if not result.get("warehouse_error"):
        return message
    message = re.sub(r"Object '[^']*'", "An object", message)
    return re.sub(r",?\s*role \S+", "", message)


def _contract(tool_input: dict) -> QueryContract:
    return QueryContract(**{k: v for k, v in tool_input.items() if k in _CONTRACT_FIELDS})


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _artifact(kind: str, title: str, data, **extra) -> dict:
    return {"id": uuid.uuid4().hex, "type": kind, "title": title, "data": data, **extra}


class ChatTools:
    def __init__(self, catalog: Catalog, database: str, *, tenant: Tenant,
                 execute: Optional[Callable[..., dict]] = None,
                 describe: Callable[[str, str], Optional[list[dict]]] = describe_table,
                 overlays: Iterable = (), allow_identity: bool = False,
                 identity_audit: Optional[Callable[[str, object], None]] = None):
        """execute: defaults to running against the tenant's warehouse. overlays: applied to catalog, reported in provenance.
        allow_identity: the caller's role may see PII columns (governed and freehand).
        identity_audit: called with ("governed", contract) before, or ("freehand", None) after the guard accepts, an identity-capable query."""
        self.catalog, self.database, self.tenant, self.describe = catalog, database, tenant, describe
        self.execute = execute or self._execute_for_tenant
        self.overlays = list(overlays)
        self.allow_identity, self.identity_audit = allow_identity, identity_audit

    def _execute_for_tenant(self, sql: str, params: Optional[dict] = None, compiled: bool = False,
                            spliced: Optional[Spliced] = None) -> dict:
        """spliced: the splice sql was built from, which the guard checks the governed bodies against."""
        from snowflake_client import validate_and_execute

        extra_pii = frozenset(c.upper() for ds in self.catalog.datasets.values() for c in ds.pii_columns)
        return validate_and_execute(sql, params, tenant=self.tenant, compiled=compiled,
                                    allow_identity=self.allow_identity, extra_pii=extra_pii, spliced=spliced)

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
        result = self.execute(compiled.sql, None, compiled=True)
        if "error" in result:
            return ToolResult({"error": _model_error(result)})

        result = named_result(result)
        rows, columns = result["rows"], result["columns"]
        compiled.provenance.overlays = overlays_used(compiled.provenance, self.catalog, self.overlays)
        provenance = compiled.provenance.model_dump()
        query = contract.model_dump(mode="json", exclude_defaults=True)
        title = tool_input.get("title") or "Query result"
        common = {"query": query, "sql": compiled.sql, "provenance": provenance}
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
        schema, table = tool_input.get("schema", ""), tool_input.get("table", "")
        if schema.upper() == "LEARN" and not allows_learn(self.tenant):
            return ToolResult({"error": LEARN_REFUSAL})
        columns = self.describe(schema, table)
        if not columns:
            return ToolResult({"error": f"No dictionary entry for {schema.upper()}.{table.upper()}"})
        return ToolResult({"table": f"{schema.upper()}.{table.upper()}", "columns": columns})

    def _spliced(self, sql: str, governed) -> Spliced:
        """Raises SpliceError for a malformed `governed` input, naming the part at fault."""
        if not isinstance(governed, dict):
            raise SpliceError("governed must be an object mapping governed_1, governed_2, ... to query_semantic inputs")
        contracts = {}
        for name, part in governed.items():
            if not isinstance(part, dict):
                raise SpliceError(f"{name}: a governed part takes the same fields as query_semantic")
            try:
                contracts[name] = _contract(part)
            except ValidationError as e:
                raise SpliceError(f"{name}: {e}") from e
        return splice(sql, contracts, self.catalog, self.database, allow_identity=self.allow_identity)

    def _governed_provenance(self, spliced: Spliced, columns: list[str], reason: str) -> dict:
        labels = label(spliced, columns, reason=reason)
        compiled = [part.compiled.provenance for part in spliced.parts.values()]
        return {
            "governed": labels["governance"] == "full", **labels, "reason": reason,
            "metrics": _unique(m for p in compiled for m in p.metrics),
            "measures": _unique(m for p in compiled for m in p.measures),
            "datasets": _unique(d for p in compiled for d in p.datasets),
            "overlays": _unique(o for p in compiled for o in overlays_used(p, self.catalog, self.overlays)),
        }

    def _tool_execute_sql(self, tool_input: dict) -> ToolResult:
        reason = (tool_input.get("reason") or "").strip()
        if len(reason) < MIN_REASON_LENGTH:
            return ToolResult({"error": "execute_sql needs a reason (a sentence) saying why no governed definition fits"})
        sql = tool_input.get("sql", "")
        spliced = None
        if tool_input.get("governed"):
            try:
                spliced = self._spliced(sql, tool_input["governed"])
            except SpliceError as e:
                return ToolResult({"error": str(e)})
            if self.allow_identity and self.identity_audit:
                for part in spliced.parts.values():
                    self.identity_audit("governed", part.contract)
            result = self.execute(spliced.sql, None, spliced=spliced)
        else:
            result = self.execute(sql, None)
        # Logged only once the guard accepted the statement; a warehouse failure still means it was accepted.
        if self.allow_identity and self.identity_audit and ("error" not in result or "warehouse_error" in result):
            self.identity_audit("freehand", None)
        if "error" in result:
            return ToolResult({"error": _model_error(result)})
        rows, columns = result["rows"], result["columns"]
        extra = {"sql": sql}
        if spliced:
            provenance = self._governed_provenance(spliced, columns, reason)
            extra |= {"executed_sql": spliced.sql, "governed_parts": {
                name: part.contract.model_dump(mode="json", exclude_unset=True) for name, part in spliced.parts.items()}}
        else:
            provenance = {"governed": False, "governance": "none", "rows": "modified", "reason": reason,
                          "columns": {c: {"governed": False, "reason": reason} for c in columns}}
        title = tool_input.get("title") or {
            "full": "Governed query result", "partial": "Partly governed query result",
        }.get(provenance["governance"], "Ungoverned query result")
        content = {
            "columns": columns, "rows": rows[:MODEL_ROW_LIMIT], "row_count": len(rows),
            "truncated": len(rows) > MODEL_ROW_LIMIT, "provenance": provenance,
        }
        return ToolResult(content, [
            _artifact("table", title, {"columns": columns, "rows": rows}, provenance=provenance, **extra),
            _artifact("sql", title, sql, provenance=provenance, **extra),
        ])
