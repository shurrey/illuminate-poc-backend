"""Tools the chat model calls, and the artifacts their results become for the client.

Tool content goes back to the model (row-capped); artifacts carry every row plus the query
contract, SQL and provenance so the client can render, re-run or pin the result.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from pydantic import ValidationError

from .compiler import CompileError, compile_query, named_result
from .dictionary import describe_table
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
        "values": {"type": "array", "items": {}},
    },
    "required": ["dimension", "op"],
}

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
        "description": (
            "Last resort: run read-only SQL against the CDM schemas when search_catalog and "
            "query_semantic cannot answer. Results are shown to the user as ungoverned. `reason` must "
            "say why no governed definition fits."
        ),
        "inputSchema": {"json": {
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "reason": {"type": "string", "description": "Why no governed metric or dataset answers the question"},
                "title": {"type": "string"},
            },
            "required": ["sql", "reason"],
        }},
    },
]


@dataclass
class ToolResult:
    content: dict
    artifacts: list[dict] = field(default_factory=list)


def _default_execute(sql: str, params: Optional[dict] = None, compiled: bool = False) -> dict:
    from snowflake_client import validate_and_execute

    return validate_and_execute(sql, params, compiled=compiled)


def _artifact(kind: str, title: str, data, **extra) -> dict:
    return {"id": uuid.uuid4().hex, "type": kind, "title": title, "data": data, **extra}


class ChatTools:
    def __init__(self, catalog: Catalog, database: str,
                 execute: Callable[..., dict] = _default_execute,
                 describe: Callable[[str, str], Optional[list[dict]]] = describe_table):
        self.catalog, self.database, self.execute, self.describe = catalog, database, execute, describe

    @property
    def specs(self) -> list[dict]:
        return SPECS

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
            contract = QueryContract(**{k: v for k, v in tool_input.items() if k in _CONTRACT_FIELDS})
            compiled = compile_query(contract, self.catalog, self.database)
        except (ValidationError, CompileError) as e:
            return ToolResult({"error": str(e)})

        result = self.execute(compiled.sql, None, compiled=True)
        if "error" in result:
            return ToolResult({"error": result["error"], "sql": compiled.sql})

        result = named_result(result)
        rows, columns = result["rows"], result["columns"]
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
        columns = self.describe(schema, table)
        if not columns:
            return ToolResult({"error": f"No dictionary entry for {schema.upper()}.{table.upper()}"})
        return ToolResult({"table": f"{schema.upper()}.{table.upper()}", "columns": columns})

    def _tool_execute_sql(self, tool_input: dict) -> ToolResult:
        reason = (tool_input.get("reason") or "").strip()
        if len(reason) < MIN_REASON_LENGTH:
            return ToolResult({"error": "execute_sql needs a reason (a sentence) saying why no governed definition fits"})
        sql = tool_input.get("sql", "")
        result = self.execute(sql, None)
        if "error" in result:
            return ToolResult({"error": result["error"]})
        rows, columns = result["rows"], result["columns"]
        provenance = {"governed": False, "reason": reason}
        title = tool_input.get("title") or "Ungoverned query result"
        content = {
            "columns": columns, "rows": rows[:MODEL_ROW_LIMIT], "row_count": len(rows),
            "truncated": len(rows) > MODEL_ROW_LIMIT, "provenance": provenance,
        }
        return ToolResult(content, [
            _artifact("table", title, {"columns": columns, "rows": rows}, sql=sql, provenance=provenance),
            _artifact("sql", title, sql, sql=sql, provenance=provenance),
        ])
