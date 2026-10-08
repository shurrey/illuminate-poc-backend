# Semantic Layer Phase 5 (Chat Grounded in the Semantic Layer) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The chat model answers through the semantic layer: search the catalog, run governed queries, and fall back to freehand SQL only with a stated reason. Every answer's data comes back to the client as structured artifacts with provenance.

**Architecture:** `semantic_layer/chat_tools.py` holds the tools and builds artifacts from their results. `semantic_layer/prompt.py` builds the system prompt from the public catalog. `chat_engine.py` is a thin Bedrock Converse loop over those pieces. Turn history keeps each turn's query contracts.

**Tech Stack:** Python 3.11, Bedrock Converse (prompt caching), FastAPI SSE, DynamoDB (moto in tests).

**Spec:** `docs/superpowers/specs/2026-10-08-semantic-layer-bbd-parity-design.md` §5.4. Roadmap: Phase 5.

## Global Constraints

- Nothing calls Bedrock or the data-dictionary API in tests; both are injected or stubbed.
- The model sees at most 200 rows per tool call; the artifacts carry every row.
- Artifacts keep the shape the current frontend renders (`{id, type, title, data}`) and add `query`, `sql` and `provenance`.
- Tenant overlays are not applied to semantic queries until Phase 6b.

## Review Focus

1. A freehand query without a stated reason never runs (5d `test_execute_sql_requires_a_reason_and_runs_nothing_without_one`).
2. A follow-up question can modify the previous contract (5f `test_follow_up_questions_see_the_previous_query_contract`).
3. Another user's conversation id never loads their history (5f `test_someone_elses_conversation_id_is_replaced`).
4. Numeric and date values in artifacts reach the browser as JSON numbers and ISO strings (5c `test_stream_completes_with_tool_artifacts_serialised_as_json`).
5. PII dimensions are not offered to the model as groupable (5a search, 5c prompt "filter only").

---

### Task 1 (PR 5a): rank catalog metrics, measures and dimensions against a question

**Files:** `semantic_layer/search.py`, `tests/test_semantic_search.py`

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b feat/semantic-search origin/main
```

- [ ] **Step 2: Write the tests**

```diff
diff --git a/tests/test_semantic_search.py b/tests/test_semantic_search.py
new file mode 100644
index 0000000..c2b5a62
--- /dev/null
+++ b/tests/test_semantic_search.py
@@ -0,0 +1,41 @@
+from semantic_layer.catalog import load_catalog
+from semantic_layer.search import search_catalog
+from tests.semantic_fixtures import catalog
+
+CATALOG = load_catalog()
+
+
+def _ids(question, **kw):
+    return [hit["id"] for hit in search_catalog(question, CATALOG, **kw)]
+
+
+def test_metric_synonyms_rank_first():
+    assert _ids("how many active learners do we have")[0] == "metric.active_students.v1"
+
+
+def test_grade_questions_find_grade_metrics():
+    hits = _ids("what is the average grade by term")
+    assert "metric.average_grade.v1" in hits[:3]
+
+
+def test_dimension_hits_carry_their_dataset():
+    hits = search_catalog("department", CATALOG)
+    dims = [h for h in hits if h["kind"] == "dimension"]
+    assert dims and all(h["id"].startswith("dataset.") and ":" in h["id"] for h in dims)
+    assert any(h["id"].endswith(":ih_level_3") for h in dims)
+
+
+def test_results_are_limited_and_scored_descending():
+    hits = search_catalog("courses students grades activity", CATALOG, limit=5)
+    assert len(hits) == 5
+    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)
+
+
+def test_unrelated_question_returns_nothing():
+    assert search_catalog("weather in paris tomorrow", CATALOG) == []
+
+
+def test_internal_datasets_are_not_searchable():
+    from tests.semantic_fixtures import dataset
+    hidden = dataset(visibility="internal")
+    assert search_catalog("enrollments", catalog(hidden, metrics=())) == []
```

- [ ] **Step 3: Run them to verify they fail**

Run: `$PY -m pytest -q tests/test_semantic_search.py`
Expected: collection error: no module semantic_layer.search.

- [ ] **Step 4: Implement**

```diff
diff --git a/semantic_layer/search.py b/semantic_layer/search.py
new file mode 100644
index 0000000..8caaec9
--- /dev/null
+++ b/semantic_layer/search.py
@@ -0,0 +1,64 @@
+"""Rank catalog entries (metrics, measures, dimensions) against a natural-language question.
+
+Deterministic token overlap: synonym and name matches outweigh description matches, and
+governed metrics get a small boost so the model reaches for them before raw measures.
+"""
+
+from __future__ import annotations
+
+import re
+
+from .schema import Catalog
+
+_STOPWORDS = frozenset(
+    "a an and are as at by do does each for from give have how i in is it list many me much of on or our "
+    "per show tell than that the their there this to us was we were what which who with".split()
+)
+_WEIGHTS = {"synonym": 3, "name": 2, "question": 1, "description": 1}
+_METRIC_BOOST = 1
+
+
+def _tokens(text: str) -> set[str]:
+    words = re.findall(r"[a-z0-9]+", text.lower().replace("_", " "))
+    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words if w not in _STOPWORDS}
+
+
+def _score(question: set[str], fields: dict[str, list[str]]) -> int:
+    best: dict[str, int] = {}
+    for kind, texts in fields.items():
+        field_tokens = set()
+        for text in texts:
+            field_tokens |= _tokens(text)
+        for token in question & field_tokens:
+            best[token] = max(best.get(token, 0), _WEIGHTS[kind])
+    return sum(best.values())
+
+
+def search_catalog(question: str, catalog: Catalog, limit: int = 8) -> list[dict]:
+    """Best matches first: {kind, id, display_name, description, score}; empty when nothing matches."""
+    q = _tokens(question)
+    hits = []
+    public = {i: d for i, d in catalog.datasets.items() if d.visibility == "public"}
+    for m in catalog.metrics.values():
+        if m.dataset_id not in public:
+            continue
+        s = _score(q, {"synonym": m.synonyms, "name": [m.display_name, m.short_name],
+                       "question": m.example_questions, "description": [m.description]})
+        if s:
+            hits.append({"kind": "metric", "id": m.id, "display_name": m.display_name,
+                         "description": m.description, "score": s + _METRIC_BOOST})
+    for ds in public.values():
+        for meas in ds.measures:
+            s = _score(q, {"synonym": meas.synonyms, "name": [meas.name], "description": [meas.description]})
+            if s:
+                hits.append({"kind": "measure", "id": f"{ds.id}:{meas.name}", "display_name": meas.name,
+                             "description": meas.description or ds.display_name, "score": s})
+        for dim in ds.dimensions:
+            if ds.is_pii(dim.column):
+                continue
+            s = _score(q, {"synonym": dim.synonyms, "name": [dim.name], "description": [dim.description]})
+            if s:
+                hits.append({"kind": "dimension", "id": f"{ds.id}:{dim.name}", "display_name": dim.name,
+                             "description": dim.description or ds.display_name, "score": s})
+    hits.sort(key=lambda h: (-h["score"], h["kind"] != "metric", h["id"]))
+    return hits[:limit]
```

- [ ] **Step 5: Verify**

Run: `$PY -m pytest -q tests`
Expected: all pass.

Live run recorded during planning: n/a (no dataset added).

- [ ] **Step 6: Commit, open the PR, merge** (PR body: the summary above plus the live-run line).

### Task 2 (PR 5b): chat tools search_catalog and query_semantic

**Files:** `semantic_layer/chat_tools.py`, `tests/test_chat_tools.py`

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b feat/chat-tool-query-semantic origin/main
```

- [ ] **Step 2: Write the tests**

```diff
diff --git a/tests/test_chat_tools.py b/tests/test_chat_tools.py
new file mode 100644
index 0000000..272221b
--- /dev/null
+++ b/tests/test_chat_tools.py
@@ -0,0 +1,89 @@
+from semantic_layer.catalog import load_catalog
+from semantic_layer.chat_tools import MODEL_ROW_LIMIT, ChatTools
+
+CATALOG = load_catalog()
+
+
+class FakeWarehouse:
+    def __init__(self, rows=None, error=None):
+        self.rows, self.error, self.sql = rows if rows is not None else [{"N": 1}], error, []
+
+    def __call__(self, sql, params=None):
+        self.sql.append(sql)
+        if self.error:
+            return {"error": self.error}
+        return {"columns": list(self.rows[0]) if self.rows else [], "rows": self.rows}
+
+
+def _tools(warehouse=None):
+    return ChatTools(CATALOG, "DB", execute=warehouse or FakeWarehouse())
+
+
+def test_specs_name_every_tool_once():
+    names = [s["name"] for s in _tools().specs]
+    assert names == sorted(set(names), key=names.index)
+    assert {"search_catalog", "query_semantic"} <= set(names)
+
+
+def test_search_catalog_returns_matches():
+    out = _tools().dispatch("search_catalog", {"question": "average grade"})
+    assert out.artifacts == []
+    assert out.content["matches"][0]["id"] == "metric.average_grade.v1"
+
+
+def test_query_semantic_runs_compiled_sql_and_returns_provenance():
+    wh = FakeWarehouse([{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3}])
+    out = _tools(wh).dispatch("query_semantic", {"metrics": ["metric.reportable_courses.v1"], "dimensions": ["term_name"],
+                                                 "title": "Courses by term"})
+    assert wh.sql and "DS_COURSE_FILTERS_V1" in wh.sql[0]
+    assert out.content["rows"] == [{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3}]
+    assert out.content["provenance"]["governed"] is True
+    table = next(a for a in out.artifacts if a["type"] == "table")
+    assert table["title"] == "Courses by term"
+    assert table["data"] == {"columns": ["TERM_NAME", "REPORTABLE_COURSES"], "rows": [{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3}]}
+    assert table["query"]["metrics"] == ["metric.reportable_courses.v1"]
+    assert table["sql"] == wh.sql[0]
+    assert any(a["type"] == "sql" and a["data"] == wh.sql[0] for a in out.artifacts)
+
+
+def test_model_sees_at_most_the_row_limit_but_the_artifact_has_every_row():
+    rows = [{"N": i} for i in range(MODEL_ROW_LIMIT + 50)]
+    out = _tools(FakeWarehouse(rows)).dispatch("query_semantic", {"metrics": ["metric.reportable_courses.v1"], "limit": 1000})
+    assert len(out.content["rows"]) == MODEL_ROW_LIMIT
+    assert out.content["truncated"] is True and out.content["row_count"] == len(rows)
+    assert len(next(a for a in out.artifacts if a["type"] == "table")["data"]["rows"]) == len(rows)
+
+
+def test_invalid_contract_is_reported_to_the_model_and_nothing_runs():
+    wh = FakeWarehouse()
+    out = _tools(wh).dispatch("query_semantic", {"metrics": ["metric.nope.v1"]})
+    assert "unknown metric" in out.content["error"]
+    assert wh.sql == [] and out.artifacts == []
+    out = _tools(wh).dispatch("query_semantic", {"limit": 5000})
+    assert "error" in out.content and wh.sql == []
+
+
+def test_warehouse_errors_are_returned_with_the_sql():
+    out = _tools(FakeWarehouse(error="boom")).dispatch("query_semantic", {"metrics": ["metric.reportable_courses.v1"]})
+    assert out.content["error"] == "boom" and "DS_COURSE_FILTERS_V1" in out.content["sql"]
+    assert out.artifacts == []
+
+
+def test_chart_artifact_uses_the_result_rows():
+    wh = FakeWarehouse([{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3}])
+    out = _tools(wh).dispatch("query_semantic", {"metrics": ["metric.reportable_courses.v1"], "dimensions": ["term_name"],
+                                                 "chart": {"type": "bar", "x": "TERM_NAME", "y": "REPORTABLE_COURSES"}})
+    chart = next(a for a in out.artifacts if a["type"] == "chart")
+    assert chart["data"]["chart_type"] == "bar"
+    assert chart["data"]["x_axis"] == "TERM_NAME" and chart["data"]["data"] == wh.rows
+
+
+def test_chart_naming_missing_columns_is_dropped_with_a_note():
+    out = _tools().dispatch("query_semantic", {"metrics": ["metric.reportable_courses.v1"],
+                                              "chart": {"type": "bar", "x": "NOPE", "y": "N"}})
+    assert not any(a["type"] == "chart" for a in out.artifacts)
+    assert "NOPE" in out.content["chart_error"]
+
+
+def test_unknown_tool():
+    assert "Unknown tool" in _tools().dispatch("drop_tables", {}).content["error"]
```

- [ ] **Step 3: Run them to verify they fail**

Run: `$PY -m pytest -q tests/test_chat_tools.py`
Expected: collection error: no module semantic_layer.chat_tools.

- [ ] **Step 4: Implement**

```diff
diff --git a/semantic_layer/chat_tools.py b/semantic_layer/chat_tools.py
new file mode 100644
index 0000000..cb09a5b
--- /dev/null
+++ b/semantic_layer/chat_tools.py
@@ -0,0 +1,151 @@
+"""Tools the chat model calls, and the artifacts their results become for the client.
+
+Tool content goes back to the model (row-capped); artifacts carry every row plus the query
+contract, SQL and provenance so the client can render, re-run or pin the result.
+"""
+
+from __future__ import annotations
+
+import uuid
+from dataclasses import dataclass, field
+from typing import Callable, Optional
+
+from pydantic import ValidationError
+
+from .compiler import CompileError, compile_query
+from .contract import QueryContract
+from .schema import Catalog
+from .search import search_catalog
+
+MODEL_ROW_LIMIT = 200
+_CONTRACT_FIELDS = set(QueryContract.model_fields)
+
+_FILTER_SCHEMA = {
+    "type": "object",
+    "properties": {
+        "dimension": {"type": "string"},
+        "op": {"type": "string", "enum": ["eq", "neq", "in", "not_in", "gt", "gte", "lt", "lte",
+                                          "between", "is_null", "not_null", "contains"]},
+        "values": {"type": "array", "items": {}},
+    },
+    "required": ["dimension", "op"],
+}
+
+SPECS = [
+    {
+        "name": "search_catalog",
+        "description": (
+            "Find governed metrics, measures and dimensions that match a question. "
+            "Call this first; use the ids it returns in query_semantic."
+        ),
+        "inputSchema": {"json": {
+            "type": "object",
+            "properties": {"question": {"type": "string"}},
+            "required": ["question"],
+        }},
+    },
+    {
+        "name": "query_semantic",
+        "description": (
+            "Run a governed query over the semantic layer. Name metrics (metric ids) or measures "
+            "('<dataset id>:<measure>'), optional dimensions (a dimension name, '<name>__<grain>' for "
+            "time grains, or '<dataset id>:<name>'), filters on dimensions, an optional time range, "
+            "ordering and a limit (max 1000). Optionally ask for a chart of the result."
+        ),
+        "inputSchema": {"json": {
+            "type": "object",
+            "properties": {
+                "metrics": {"type": "array", "items": {"type": "string"}},
+                "measures": {"type": "array", "items": {"type": "string"}},
+                "dimensions": {"type": "array", "items": {"type": "string"}},
+                "filters": {"type": "array", "items": _FILTER_SCHEMA},
+                "time_range": {"type": "object", "properties": {
+                    "dimension": {"type": "string"},
+                    "start": {"type": "string", "description": "YYYY-MM-DD"},
+                    "end": {"type": "string", "description": "YYYY-MM-DD"},
+                }, "required": ["dimension"]},
+                "order_by": {"type": "array", "items": {"type": "object", "properties": {
+                    "field": {"type": "string"}, "direction": {"type": "string", "enum": ["asc", "desc"]},
+                }, "required": ["field"]}},
+                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
+                "title": {"type": "string", "description": "Short title for the result shown to the user"},
+                "chart": {"type": "object", "properties": {
+                    "type": {"type": "string", "enum": ["bar", "line", "pie", "scatter"]},
+                    "x": {"type": "string", "description": "Result column for the x axis"},
+                    "y": {"type": "string", "description": "Result column for the y axis"},
+                }, "required": ["type", "x", "y"]},
+            },
+        }},
+    },
+]
+
+
+@dataclass
+class ToolResult:
+    content: dict
+    artifacts: list[dict] = field(default_factory=list)
+
+
+def _default_execute(sql: str, params: Optional[dict] = None) -> dict:
+    from snowflake_client import validate_and_execute
+
+    return validate_and_execute(sql, params)
+
+
+def _artifact(kind: str, title: str, data, **extra) -> dict:
+    return {"id": uuid.uuid4().hex, "type": kind, "title": title, "data": data, **extra}
+
+
+class ChatTools:
+    def __init__(self, catalog: Catalog, database: str,
+                 execute: Callable[[str, Optional[dict]], dict] = _default_execute):
+        self.catalog, self.database, self.execute = catalog, database, execute
+
+    @property
+    def specs(self) -> list[dict]:
+        return SPECS
+
+    def dispatch(self, name: str, tool_input: dict) -> ToolResult:
+        handler = getattr(self, f"_tool_{name}", None)
+        if handler is None:
+            return ToolResult({"error": f"Unknown tool: {name}"})
+        return handler(tool_input)
+
+    def _tool_search_catalog(self, tool_input: dict) -> ToolResult:
+        return ToolResult({"matches": search_catalog(tool_input.get("question", ""), self.catalog)})
+
+    def _tool_query_semantic(self, tool_input: dict) -> ToolResult:
+        try:
+            contract = QueryContract(**{k: v for k, v in tool_input.items() if k in _CONTRACT_FIELDS})
+            compiled = compile_query(contract, self.catalog, self.database)
+        except (ValidationError, CompileError) as e:
+            return ToolResult({"error": str(e)})
+
+        result = self.execute(compiled.sql, None)
+        if "error" in result:
+            return ToolResult({"error": result["error"], "sql": compiled.sql})
+
+        rows, columns = result["rows"], result["columns"]
+        provenance = compiled.provenance.model_dump()
+        query = contract.model_dump(mode="json", exclude_defaults=True)
+        title = tool_input.get("title") or "Query result"
+        common = {"query": query, "sql": compiled.sql, "provenance": provenance}
+        content = {
+            "columns": columns, "rows": rows[:MODEL_ROW_LIMIT], "row_count": len(rows),
+            "truncated": len(rows) > MODEL_ROW_LIMIT, "provenance": provenance, "sql": compiled.sql,
+        }
+        artifacts = [
+            _artifact("table", title, {"columns": columns, "rows": rows}, **common),
+            _artifact("sql", title, compiled.sql, **common),
+        ]
+        chart = tool_input.get("chart")
+        if chart:
+            missing = [c for c in (chart.get("x"), chart.get("y")) if c not in columns]
+            if missing:
+                content["chart_error"] = f"chart columns not in the result: {missing}; result columns are {columns}"
+            else:
+                artifacts.append(_artifact("chart", title, {
+                    "chart_type": chart["type"], "title": title,
+                    "x_axis": chart["x"], "y_axis": chart["y"], "data": rows,
+                }, **common))
+        return ToolResult(content, artifacts)
```

- [ ] **Step 5: Verify**

Run: `$PY -m pytest -q tests`
Expected: all pass.

Live run recorded during planning: n/a (no dataset added).

- [ ] **Step 6: Commit, open the PR, merge** (PR body: the summary above plus the live-run line).

### Task 3 (PR 5d): labelled freehand fallback tools (describe_cdm_table, execute_sql with reason)

**Files:** `semantic_layer/chat_tools.py`, `semantic_layer/dictionary.py`, `tests/test_chat_tools.py`, `tests/test_semantic_dictionary.py`

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b feat/chat-fallback-tools origin/main
```

- [ ] **Step 2: Write the tests**

```diff
diff --git a/tests/test_chat_tools.py b/tests/test_chat_tools.py
index 272221b..4ea4419 100644
--- a/tests/test_chat_tools.py
+++ b/tests/test_chat_tools.py
@@ -87,3 +87,55 @@ def test_chart_naming_missing_columns_is_dropped_with_a_note():
 
 def test_unknown_tool():
     assert "Unknown tool" in _tools().dispatch("drop_tables", {}).content["error"]
+
+
+DICTIONARY = {
+    ("CDM_LMS", "COURSE"): [
+        {"name": "ID", "type": "NUMBER", "pii": False, "description": "Primary key"},
+        {"name": "NAME", "type": "TEXT", "pii": False, "description": "Course name"},
+    ],
+}
+
+
+def _fallback(warehouse=None):
+    return ChatTools(CATALOG, "DB", execute=warehouse or FakeWarehouse(),
+                     describe=lambda schema, table: DICTIONARY.get((schema.upper(), table.upper())))
+
+
+def test_execute_sql_requires_a_reason_and_runs_nothing_without_one():
+    wh = FakeWarehouse()
+    for reason in (None, "", "   "):
+        out = _fallback(wh).dispatch("execute_sql", {"sql": "SELECT 1", "reason": reason})
+        assert "reason" in out.content["error"]
+    assert wh.sql == []
+
+
+def test_execute_sql_results_are_marked_ungoverned_with_the_reason():
+    wh = FakeWarehouse([{"N": 7}])
+    out = _fallback(wh).dispatch("execute_sql", {"sql": "SELECT COUNT(*) AS N FROM DB.CDM_LMS.COURSE",
+                                                 "reason": "No dataset covers raw course counts by instance"})
+    assert wh.sql == ["SELECT COUNT(*) AS N FROM DB.CDM_LMS.COURSE"]
+    assert out.content["provenance"] == {"governed": False, "reason": "No dataset covers raw course counts by instance"}
+    assert all(a["provenance"]["governed"] is False for a in out.artifacts)
+    assert "query" not in out.artifacts[0]
+
+
+def test_execute_sql_errors_pass_through():
+    out = _fallback(FakeWarehouse(error="Schema 'X' is not in the allowed list")).dispatch(
+        "execute_sql", {"sql": "SELECT * FROM X.Y", "reason": "testing"})
+    assert "allowed list" in out.content["error"] and out.artifacts == []
+
+
+def test_describe_cdm_table_returns_columns():
+    out = _fallback().dispatch("describe_cdm_table", {"schema": "cdm_lms", "table": "course"})
+    assert [c["name"] for c in out.content["columns"]] == ["ID", "NAME"]
+
+
+def test_describe_unknown_table_is_an_error():
+    out = _fallback().dispatch("describe_cdm_table", {"schema": "CDM_LMS", "table": "NOPE"})
+    assert "CDM_LMS.NOPE" in out.content["error"]
+
+
+def test_specs_include_the_fallback_tools():
+    names = {s["name"] for s in _fallback().specs}
+    assert {"execute_sql", "describe_cdm_table"} <= names
diff --git a/tests/test_semantic_dictionary.py b/tests/test_semantic_dictionary.py
new file mode 100644
index 0000000..2d4bb09
--- /dev/null
+++ b/tests/test_semantic_dictionary.py
@@ -0,0 +1,40 @@
+import io
+import json
+
+import pytest
+
+from semantic_layer import dictionary
+
+DEFINITIONS = [
+    {"name": "CDM_LMS.PERSON.EMAIL", "columnDataType": "TEXT", "text": "Email",
+     "technicalSpecifications": [{"isPii": True}]},
+    {"name": "CDM_LMS.PERSON.ID", "columnDataType": "NUMBER", "technicalSpecifications": []},
+    {"name": "LEARN.USERS.EMAIL", "columnDataType": "TEXT"},
+    {"name": "CDM_LMS.PERSON", "text": "table-level entry"},
+]
+
+
+@pytest.fixture(autouse=True)
+def fresh_cache():
+    dictionary._tables.cache_clear()
+    yield
+    dictionary._tables.cache_clear()
+
+
+def test_columns_come_from_definitions_with_pii_flags(monkeypatch):
+    monkeypatch.setattr(dictionary.urllib.request, "urlopen",
+                        lambda url, timeout: io.BytesIO(json.dumps(DEFINITIONS).encode()))
+    cols = dictionary.describe_table("cdm_lms", "person")
+    assert cols == [
+        {"name": "EMAIL", "type": "TEXT", "pii": True, "description": "Email"},
+        {"name": "ID", "type": "NUMBER", "pii": False, "description": ""},
+    ]
+    assert dictionary.describe_table("LEARN", "USERS") is None
+
+
+def test_unreachable_dictionary_returns_none(monkeypatch):
+    def down(url, timeout):
+        raise OSError("connection refused")
+
+    monkeypatch.setattr(dictionary.urllib.request, "urlopen", down)
+    assert dictionary.describe_table("CDM_LMS", "PERSON") is None
```

- [ ] **Step 3: Run them to verify they fail**

Run: `$PY -m pytest -q tests/test_chat_tools.py`
Expected: 6 failures: ChatTools has no describe argument or fallback tools.

- [ ] **Step 4: Implement**

```diff
diff --git a/semantic_layer/chat_tools.py b/semantic_layer/chat_tools.py
index cb09a5b..e7819f5 100644
--- a/semantic_layer/chat_tools.py
+++ b/semantic_layer/chat_tools.py
@@ -13,6 +13,7 @@ from typing import Callable, Optional
 from pydantic import ValidationError
 
 from .compiler import CompileError, compile_query
+from .dictionary import describe_table
 from .contract import QueryContract
 from .schema import Catalog
 from .search import search_catalog
@@ -77,6 +78,35 @@ SPECS = [
             },
         }},
     },
+    {
+        "name": "describe_cdm_table",
+        "description": (
+            "List the columns of a raw CDM table (schema like CDM_LMS). Only for writing execute_sql "
+            "when no governed metric, measure or dimension fits."
+        ),
+        "inputSchema": {"json": {
+            "type": "object",
+            "properties": {"schema": {"type": "string"}, "table": {"type": "string"}},
+            "required": ["schema", "table"],
+        }},
+    },
+    {
+        "name": "execute_sql",
+        "description": (
+            "Last resort: run read-only SQL against the CDM schemas when search_catalog and "
+            "query_semantic cannot answer. Results are shown to the user as ungoverned. `reason` must "
+            "say why no governed definition fits."
+        ),
+        "inputSchema": {"json": {
+            "type": "object",
+            "properties": {
+                "sql": {"type": "string"},
+                "reason": {"type": "string", "description": "Why no governed metric or dataset answers the question"},
+                "title": {"type": "string"},
+            },
+            "required": ["sql", "reason"],
+        }},
+    },
 ]
 
 
@@ -98,8 +128,9 @@ def _artifact(kind: str, title: str, data, **extra) -> dict:
 
 class ChatTools:
     def __init__(self, catalog: Catalog, database: str,
-                 execute: Callable[[str, Optional[dict]], dict] = _default_execute):
-        self.catalog, self.database, self.execute = catalog, database, execute
+                 execute: Callable[[str, Optional[dict]], dict] = _default_execute,
+                 describe: Callable[[str, str], Optional[list[dict]]] = describe_table):
+        self.catalog, self.database, self.execute, self.describe = catalog, database, execute, describe
 
     @property
     def specs(self) -> list[dict]:
@@ -149,3 +180,30 @@ class ChatTools:
                     "x_axis": chart["x"], "y_axis": chart["y"], "data": rows,
                 }, **common))
         return ToolResult(content, artifacts)
+
+    def _tool_describe_cdm_table(self, tool_input: dict) -> ToolResult:
+        schema, table = tool_input.get("schema", ""), tool_input.get("table", "")
+        columns = self.describe(schema, table)
+        if not columns:
+            return ToolResult({"error": f"No dictionary entry for {schema.upper()}.{table.upper()}"})
+        return ToolResult({"table": f"{schema.upper()}.{table.upper()}", "columns": columns})
+
+    def _tool_execute_sql(self, tool_input: dict) -> ToolResult:
+        reason = (tool_input.get("reason") or "").strip()
+        if not reason:
+            return ToolResult({"error": "execute_sql needs a reason saying why no governed definition fits"})
+        sql = tool_input.get("sql", "")
+        result = self.execute(sql, None)
+        if "error" in result:
+            return ToolResult({"error": result["error"]})
+        rows, columns = result["rows"], result["columns"]
+        provenance = {"governed": False, "reason": reason}
+        title = tool_input.get("title") or "Ungoverned query result"
+        content = {
+            "columns": columns, "rows": rows[:MODEL_ROW_LIMIT], "row_count": len(rows),
+            "truncated": len(rows) > MODEL_ROW_LIMIT, "provenance": provenance,
+        }
+        return ToolResult(content, [
+            _artifact("table", title, {"columns": columns, "rows": rows}, sql=sql, provenance=provenance),
+            _artifact("sql", title, sql, sql=sql, provenance=provenance),
+        ])
diff --git a/semantic_layer/dictionary.py b/semantic_layer/dictionary.py
new file mode 100644
index 0000000..32e751a
--- /dev/null
+++ b/semantic_layer/dictionary.py
@@ -0,0 +1,42 @@
+"""Column metadata for CDM tables from the Blackboard data dictionary, fetched once per process."""
+
+from __future__ import annotations
+
+import json
+import logging
+import os
+import urllib.request
+from functools import lru_cache
+from typing import Optional
+
+logger = logging.getLogger("API-PROXY")
+
+_BASE_URL = os.environ.get("DATA_DICTIONARY_URL", "https://us.data.api.blackboard.com/api/v1/data/dictionary")
+_TIMEOUT = int(os.environ.get("DATA_DICTIONARY_TIMEOUT", "15"))
+
+
+@lru_cache(maxsize=1)
+def _tables() -> dict[tuple[str, str], list[dict]]:
+    with urllib.request.urlopen(f"{_BASE_URL}/definitions", timeout=_TIMEOUT) as resp:
+        definitions = json.loads(resp.read().decode())
+    tables: dict[tuple[str, str], list[dict]] = {}
+    for d in definitions:
+        parts = d.get("name", "").upper().split(".")
+        if len(parts) != 3 or not parts[0].startswith("CDM_"):
+            continue
+        tables.setdefault((parts[0], parts[1]), []).append({
+            "name": parts[2],
+            "type": d.get("columnDataType", ""),
+            "pii": any(s.get("isPii") for s in d.get("technicalSpecifications") or []),
+            "description": (d.get("text") or "")[:200],
+        })
+    return tables
+
+
+def describe_table(schema: str, table: str) -> Optional[list[dict]]:
+    """Columns of SCHEMA.TABLE, or None when the dictionary has no such table or cannot be reached."""
+    try:
+        return _tables().get((schema.upper(), table.upper()))
+    except Exception as e:
+        logger.warning("Data dictionary unavailable: %s", e)
+        return None
```

- [ ] **Step 5: Verify**

Run: `$PY -m pytest -q tests`
Expected: all pass.

Live run recorded during planning: n/a (no dataset added).

- [ ] **Step 6: Commit, open the PR, merge** (PR body: the summary above plus the live-run line).

### Task 4 (PR 5c): chat engine answers through the semantic layer; tool artifacts replace text markers

**Files:** `chat_engine.py`, `lambda_handler.py`, `semantic_layer/prompt.py`, `tests/test_chat_engine.py`, `tests/test_chat_routes.py`, `tests/test_semantic_prompt.py`

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b feat/chat-engine-semantic origin/main
```

- [ ] **Step 2: Write the tests**

```diff
diff --git a/tests/test_chat_engine.py b/tests/test_chat_engine.py
new file mode 100644
index 0000000..18bd47f
--- /dev/null
+++ b/tests/test_chat_engine.py
@@ -0,0 +1,94 @@
+import asyncio
+import json
+import os
+
+import pytest
+
+os.environ.setdefault("SNOWFLAKE_DATABASE", "TESTDB")
+
+import chat_engine  # noqa: E402
+from semantic_layer.chat_tools import ToolResult  # noqa: E402
+
+
+def _text(text):
+    return {"output": {"message": {"role": "assistant", "content": [{"text": text}]}}, "stopReason": "end_turn"}
+
+
+def _tool(name, tool_input, use_id="t1"):
+    return {"output": {"message": {"role": "assistant", "content": [
+        {"toolUse": {"toolUseId": use_id, "name": name, "input": tool_input}}]}}, "stopReason": "tool_use"}
+
+
+class ScriptedBedrock:
+    def __init__(self, *responses):
+        self.responses, self.calls = list(responses), []
+
+    def converse(self, **kwargs):
+        self.calls.append(kwargs)
+        return self.responses.pop(0)
+
+
+class RecordingTools:
+    specs = [{"name": "query_semantic", "description": "d", "inputSchema": {"json": {"type": "object"}}}]
+
+    def __init__(self):
+        self.calls = []
+
+    def dispatch(self, name, tool_input):
+        self.calls.append((name, tool_input))
+        return ToolResult({"rows": [{"N": 3}]}, [{"id": "a1", "type": "table", "data": {"columns": ["N"], "rows": [{"N": 3}]}}])
+
+
+@pytest.fixture
+def tools(monkeypatch):
+    t = RecordingTools()
+    monkeypatch.setattr(chat_engine, "_tools", t)
+    return t
+
+
+def test_tool_results_go_back_to_the_model_and_artifacts_to_the_caller(monkeypatch, tools):
+    bedrock = ScriptedBedrock(_tool("query_semantic", {"metrics": ["metric.x.v1"]}), _text("There are 3."))
+    monkeypatch.setattr(chat_engine, "_bedrock", bedrock)
+    text, messages, artifacts = chat_engine.send_message("how many?", [])
+    assert text == "There are 3."
+    assert tools.calls == [("query_semantic", {"metrics": ["metric.x.v1"]})]
+    # messages: user question, assistant tool call, tool result, final answer
+    tool_result = messages[2]["content"][0]["toolResult"]
+    assert json.loads(tool_result["content"][0]["text"]) == {"rows": [{"N": 3}]}
+    assert [a["id"] for a in artifacts] == ["a1"]
+
+
+def test_system_prompt_and_tools_are_cached(monkeypatch, tools):
+    bedrock = ScriptedBedrock(_text("hi"))
+    monkeypatch.setattr(chat_engine, "_bedrock", bedrock)
+    chat_engine.send_message("hello", [])
+    call = bedrock.calls[0]
+    assert call["system"][-1] == {"cachePoint": {"type": "default"}}
+    assert call["toolConfig"]["tools"][-1] == {"cachePoint": {"type": "default"}}
+    assert call["toolConfig"]["tools"][0]["toolSpec"]["name"] == "query_semantic"
+
+
+def test_running_out_of_rounds_returns_a_message_and_any_artifacts(monkeypatch, tools):
+    bedrock = ScriptedBedrock(*[_tool("query_semantic", {}, f"t{i}") for i in range(chat_engine._MAX_ROUNDS)])
+    monkeypatch.setattr(chat_engine, "_bedrock", bedrock)
+    text, _, artifacts = chat_engine.send_message("loop", [])
+    assert "unable to complete" in text
+    assert len(artifacts) == chat_engine._MAX_ROUNDS
+
+
+def test_streaming_yields_statuses_then_the_answer_with_artifacts(monkeypatch, tools):
+    bedrock = ScriptedBedrock(_tool("query_semantic", {}), _text("Done."))
+    monkeypatch.setattr(chat_engine, "_bedrock", bedrock)
+
+    async def collect():
+        return [e async for e in chat_engine.send_message_streaming("q", [])]
+
+    events = asyncio.run(collect())
+    assert [e["type"] for e in events[:-1]] and all(e["type"] == "status" for e in events[:-1])
+    assert events[-1]["type"] == "raw_complete"
+    assert events[-1]["text"] == "Done." and [a["id"] for a in events[-1]["artifacts"]] == ["a1"]
+
+
+def test_engine_uses_the_semantic_prompt():
+    assert "search_catalog" in chat_engine.SYSTEM_PROMPT
+    assert "metric.active_students.v1" in chat_engine.SYSTEM_PROMPT
diff --git a/tests/test_chat_routes.py b/tests/test_chat_routes.py
new file mode 100644
index 0000000..b541235
--- /dev/null
+++ b/tests/test_chat_routes.py
@@ -0,0 +1,51 @@
+import json
+from datetime import date
+from decimal import Decimal
+
+import pytest
+from fastapi.testclient import TestClient
+
+import lambda_handler
+
+AUTH = {"Authorization": "Bearer test"}
+ARTIFACT = {"id": "a1", "type": "table", "title": "t",
+            "data": {"columns": ["N", "D"], "rows": [{"N": Decimal("0.25"), "D": date(2026, 9, 1)}]},
+            "provenance": {"governed": True}}
+
+
+@pytest.fixture
+def client(monkeypatch):
+    import conversation_store
+    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: {"sub": "u1"} if a else None)
+    monkeypatch.setattr(conversation_store, "load_history", lambda cid, owner: [])
+    monkeypatch.setattr(conversation_store, "save_turn", lambda *a, **k: None)
+    return TestClient(lambda_handler.app)
+
+
+def _events(body: str) -> list[dict]:
+    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
+
+
+def test_stream_completes_with_tool_artifacts_serialised_as_json(client, monkeypatch):
+    import chat_engine
+
+    async def fake_stream(message, history):
+        yield {"type": "status", "message": "Running a governed query..."}
+        yield {"type": "raw_complete", "text": "Answer for jane@example.edu", "messages": [], "artifacts": [ARTIFACT]}
+
+    monkeypatch.setattr(chat_engine, "send_message_streaming", fake_stream)
+    r = client.post("/api/chat/stream", headers=AUTH, json={"message": "q"})
+    complete = _events(r.text)[-1]
+    assert complete["type"] == "complete"
+    assert complete["data"]["artifacts"][0]["data"]["rows"] == [{"N": 0.25, "D": "2026-09-01"}]
+    assert "[EMAIL REDACTED]" in complete["data"]["text"]
+
+
+def test_non_streaming_chat_returns_tool_artifacts(client, monkeypatch):
+    import chat_engine
+
+    monkeypatch.setattr(chat_engine, "send_message", lambda message, history: ("Three.", [], [ARTIFACT]))
+    r = client.post("/api/chat", headers=AUTH, json={"message": "q"})
+    assert r.status_code == 200
+    assert r.json()["artifacts"][0]["id"] == "a1"
+    assert r.json()["artifacts"][0]["data"]["rows"] == [{"N": 0.25, "D": "2026-09-01"}]
diff --git a/tests/test_semantic_prompt.py b/tests/test_semantic_prompt.py
new file mode 100644
index 0000000..2bca2c0
--- /dev/null
+++ b/tests/test_semantic_prompt.py
@@ -0,0 +1,38 @@
+from semantic_layer.catalog import load_catalog
+from semantic_layer.prompt import build_system_prompt
+
+CATALOG = load_catalog()
+PROMPT = build_system_prompt(CATALOG, "PROD_DB")
+
+
+def test_every_public_dataset_and_metric_is_described():
+    for ds in CATALOG.datasets.values():
+        if ds.visibility == "public":
+            assert ds.id in PROMPT and ds.grain in PROMPT
+    for m in CATALOG.metrics.values():
+        assert m.id in PROMPT
+
+
+def test_dimensions_list_their_grains_and_measures_their_names():
+    assert "course_start_week (time: week, month, quarter, year)" in PROMPT
+    assert "active_students" in PROMPT
+
+
+def test_pii_dimensions_are_marked_filter_only():
+    assert "student_email (filter only)" in PROMPT
+
+
+def test_no_dataset_sql_or_raw_schema_dump():
+    assert "base_sql" not in PROMPT and "SELECT" not in PROMPT
+    assert "CDM_LMS.PERSON_COURSE" not in PROMPT
+
+
+def test_rules_put_governed_tools_first_and_require_a_reason_for_freehand_sql():
+    assert PROMPT.index("search_catalog") < PROMPT.index("execute_sql")
+    assert "reason" in PROMPT
+    assert "PROD_DB" in PROMPT
+
+
+def test_no_text_markers_are_requested():
+    for marker in ("[CHART_CONFIG]", "[SQL_QUERY]", "[QUERY_PARAMS]"):
+        assert marker not in PROMPT
```

- [ ] **Step 3: Run them to verify they fail**

Run: `$PY -m pytest -q tests/test_semantic_prompt.py tests/test_chat_engine.py`
Expected: prompt module missing; chat_engine has no _tools.

- [ ] **Step 4: Implement**

```diff
diff --git a/chat_engine.py b/chat_engine.py
index 105a2c6..56eaa09 100644
--- a/chat_engine.py
+++ b/chat_engine.py
@@ -1,522 +1,120 @@
 """
-chat_engine.py — Core Bedrock Converse engine for Illuminate.
+chat_engine.py — Bedrock Converse tool loop over the semantic layer.
 
-Fetches the Blackboard Data Dictionary at import time, compiles it into a
-schema reference, builds a system prompt, and provides sync and async
-interfaces to Claude via the Bedrock Converse API with tool_use.
+The system prompt is built from the semantic catalog; the model answers through the
+semantic tools (search_catalog, query_semantic) and falls back to labelled freehand SQL.
+Tool artifacts are collected for the client alongside the model's text.
 """
 
 import asyncio
 import json
 import logging
 import os
-import urllib.request
-from concurrent.futures import ThreadPoolExecutor
 
 import boto3
 
-import snowflake_client
-from semantic_layer.tool import (
-    TOOL_SPEC as QUERY_METRIC_CATALOG_TOOL,
-    format_catalog_for_prompt,
-    query_metric_catalog,
-)
+from semantic_layer.catalog import default_catalog
+from semantic_layer.chat_tools import ChatTools
+from semantic_layer.prompt import build_system_prompt
 
 logger = logging.getLogger("API-PROXY")
 
-# ---------------------------------------------------------------------------
-# Configuration
-# ---------------------------------------------------------------------------
-
 AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
 MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
 
-_DICT_BASE_URL = os.environ.get(
-    "DATA_DICTIONARY_URL",
-    "https://us.data.api.blackboard.com/api/v1/data/dictionary",
-)
-_DICT_TIMEOUT = int(os.environ.get("DATA_DICTIONARY_TIMEOUT", "15"))
-_PRIORITY_SCHEMAS = {"CDM_LMS", "CDM_SIS", "CDM_ALY"}
-
 _bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
 
-# ---------------------------------------------------------------------------
-# Data Dictionary fetch (runs at module import time)
-# ---------------------------------------------------------------------------
-
-
-def _fetch_json(url):
-    req = urllib.request.Request(url)
-    with urllib.request.urlopen(req, timeout=_DICT_TIMEOUT) as resp:
-        return json.loads(resp.read().decode())
-
-
-def _fetch_data_dictionary():
-    urls = {
-        "submodels": f"{_DICT_BASE_URL}/submodels",
-        "definitions": f"{_DICT_BASE_URL}/definitions",
-        "erd": f"{_DICT_BASE_URL}/erd",
-    }
-    results = {}
-    with ThreadPoolExecutor(max_workers=3) as pool:
-        futures = {name: pool.submit(_fetch_json, url) for name, url in urls.items()}
-        for name, future in futures.items():
-            results[name] = future.result()
-    return results["submodels"], results["definitions"], results["erd"]
-
-
-def _compile_schema_reference(submodels, definitions, erd):
-    def_index = {}
-    for d in definitions:
-        fqn = d.get("name", "")
-        if fqn:
-            def_index[fqn.upper()] = d
-
-    schema_names = {}
-    for s in submodels:
-        schema_names[s.get("schemaId", "").upper()] = s.get(
-            "displayName", s.get("name", "")
-        )
-
-    lines = []
-    for schema_block in erd.get("schemas", []):
-        tables = schema_block.get("tables", [])
-        if not tables:
-            continue
-        first_fqn = tables[0].get("FQN", "")
-        schema_id = (
-            first_fqn.split(".")[0].upper() if "." in first_fqn else "UNKNOWN"
-        )
-        display_name = schema_names.get(schema_id, schema_id)
-        is_priority = schema_id in _PRIORITY_SCHEMAS
 
-        lines.append(f"\n## {schema_id} ({display_name})")
-        if not is_priority:
-            table_names = [t.get("FQN", "").split(".")[-1] for t in tables]
-            lines.append(f"Tables: {', '.join(table_names)}")
-            lines.append(
-                "Use execute_sql with DESCRIBE TABLE for column details if needed."
-            )
-            continue
-
-        for table in tables:
-            table_fqn = table.get("FQN", "")
-            table_name = (
-                table_fqn.split(".")[-1] if "." in table_fqn else table_fqn
-            )
-            table_def = def_index.get(table_fqn.upper(), {})
-            table_desc = table_def.get("text", "")
-            lines.append(f"\n### {table_name}")
-            if table_desc:
-                lines.append(table_desc[:120])
-            for col in table.get("columns", []):
-                col_name = col.get("name", "")
-                data_type = col.get("dataType", "")
-                flags = []
-                if col.get("isPrimaryKey"):
-                    flags.append("PK")
-                if col.get("isForeignKey"):
-                    target = col.get("primaryKeyTableFQN", "").split(".")[-1]
-                    flags.append(f"FK→{target}")
-                col_fqn = f"{table_fqn}.{col_name}".upper()
-                col_def = def_index.get(col_fqn, {})
-                specs = col_def.get("technicalSpecifications", [])
-                if any(s.get("isPii") for s in specs):
-                    flags.append("PII")
-                desc = col_def.get("text", "")
-                desc_short = f" -- {desc[:80]}" if desc else ""
-                flag_str = f" [{','.join(flags)}]" if flags else ""
-                lines.append(
-                    f"  {col_name} {data_type}{flag_str}{desc_short}"
-                )
-
-        fks = schema_block.get("foreignKeys", [])
-        if fks:
-            lines.append(f"\nRelationships in {schema_id}:")
-            for fk in fks:
-                fk_info = fk.get("foreignKey", {})
-                uk_info = fk.get("uniqueKey", {})
-                fk_cols = ".".join(
-                    c["name"] for c in fk_info.get("columns", [])
-                )
-                uk_table = uk_info.get("tableFQN", "")
-                uk_cols = ".".join(
-                    c["name"] for c in uk_info.get("columns", [])
-                )
-                lines.append(
-                    f"  {fk_info.get('tableFQN','')}.{fk_cols} → {uk_table}.{uk_cols}"
-                )
-
-    return "\n".join(lines)
-
-
-# Resolve database name — env var preferred, fall back to Secrets Manager
 def _resolve_database() -> str:
+    """SNOWFLAKE_DATABASE if set, else the database in the Snowflake secret, else ILLUMINATE."""
     db = os.environ.get("SNOWFLAKE_DATABASE", "")
     if db:
         return db
     try:
-        secret_name = os.environ.get(
-            "SNOWFLAKE_SECRET_NAME", "illuminate/dev/snowflake"
-        )
-        region = os.environ.get("AWS_REGION", "us-east-1")
-        sm = boto3.client("secretsmanager", region_name=region)
-        resp = sm.get_secret_value(SecretId=secret_name)
-        creds = json.loads(resp["SecretString"])
+        secret_name = os.environ.get("SNOWFLAKE_SECRET_NAME", "illuminate/dev/snowflake")
+        sm = boto3.client("secretsmanager", region_name=AWS_REGION)
+        creds = json.loads(sm.get_secret_value(SecretId=secret_name)["SecretString"])
         return creds.get("database", "ILLUMINATE")
     except Exception as exc:
         logger.warning("Could not resolve database name from Secrets Manager: %s", exc)
         return "ILLUMINATE"
 
 
-# Run at import time
-try:
-    logger.info("Fetching Blackboard Data Dictionary…")
-    _submodels, _definitions, _erd = _fetch_data_dictionary()
-    _compiled_schema = _compile_schema_reference(_submodels, _definitions, _erd)
-    logger.info(
-        "Data Dictionary compiled (%d chars)", len(_compiled_schema)
-    )
-except Exception as _dict_exc:
-    logger.error("Failed to fetch Data Dictionary: %s", _dict_exc)
-    _compiled_schema = "(Data Dictionary unavailable — use DESCRIBE TABLE to inspect schemas.)"
-
 _database = _resolve_database()
+SYSTEM_PROMPT = build_system_prompt(default_catalog(), _database)
+_tools = ChatTools(default_catalog(), _database)
 
-# Load the canonical metric catalog summary for the system prompt. Failure here
-# is non-fatal — the agent falls back to freehand SQL via execute_sql.
-try:
-    _metric_catalog_summary = format_catalog_for_prompt()
-    logger.info(
-        "Canonical metric catalog loaded (%d chars)",
-        len(_metric_catalog_summary),
-    )
-except Exception as _cat_exc:
-    logger.error("Failed to load canonical metric catalog: %s", _cat_exc)
-    _metric_catalog_summary = (
-        "(Canonical metric catalog unavailable — use execute_sql for all queries.)"
-    )
-
-# ---------------------------------------------------------------------------
-# System prompt
-# ---------------------------------------------------------------------------
-
-SYSTEM_PROMPT = f"""You are Illuminate, an AI assistant for educational data analytics. You help educators, administrators, and analysts understand student outcomes, course performance, and institutional trends by querying a Snowflake data warehouse and presenting insights clearly.
-
-## Database Configuration
-- Snowflake database: `{_database}`
-- All table references must use fully-qualified names: `{_database}.<SCHEMA>.<TABLE>`
-- Available schemas and tables are documented in the Schema Reference below.
-
-## Schema Reference
-{_compiled_schema}
-
-## Canonical Metric Catalog
-
-The following metrics are pre-vetted by Blackboard's data product team. When the user's question matches one of these intents, **prefer calling the `query_metric_catalog` tool** over generating freehand SQL with `execute_sql`. The catalog tool returns aggregated results with full provenance (which definition was used, who owns it) and uses a SQL safety guard that only permits SELECT/CTE statements against the canonical CDM tables.
-
-{_metric_catalog_summary}
-
-Use `execute_sql` only when no metric above answers the question, or when the user explicitly asks to see raw data not covered by a metric.
-
-## SQL Query Guidelines
-- Always use fully-qualified table names: `{_database}.CDM_LMS.COURSE_MAIN`
-- Only reference columns that appear in the Schema Reference above; use `DESCRIBE TABLE` if uncertain.
-- Join tables using documented foreign keys; prefer primary key (ID column) joins.
-- Default result limit is 100 rows unless the user requests more. Never exceed 1000.
-- For student filters: use `COURSE_ROLE = 'S'`; for instructors: `COURSE_ROLE = 'I'`.
-- When filtering by course or user, prefer parameterized queries using `:param_name` syntax.
-- Write efficient queries — avoid full-table scans when filters are available.
-- Prefer CTEs (WITH clauses) for readability when multiple steps are needed.
-
-## FERPA Compliance Rules
-- Never include PII columns (first name, last name, email, SSN, phone, address, date of birth, password) in a SELECT result without aggregation.
-- When reporting on individual students, aggregate data and suppress groups smaller than 5.
-- If a user requests raw PII, explain the FERPA restriction and offer an aggregated alternative.
-
-## Parameterized Queries
-When a query includes user-supplied values (course ID, term, student count threshold, etc.), use Snowflake bind variable syntax `:param_name` and declare the parameters in a `[QUERY_PARAMS]` block at the end of your response:
-
-```
-[QUERY_PARAMS]
-{{"param_name": "value", "other_param": 42}}
-```
-
-## Chart Visualization
-When your results are well-suited to a chart, include a `[CHART_CONFIG]` block after your main response. Use this JSON structure:
-
-```
-[CHART_CONFIG]
-{{"type": "bar"|"line"|"pie"|"scatter", "title": "...", "x_key": "column_name", "y_key": "column_name", "color_key": "optional_column"}}
-```
-
-Only include `[CHART_CONFIG]` when a chart genuinely adds value (trends, distributions, comparisons). Do not include it for single-value or text-only results.
-
-## SQL Transparency
-Always show the SQL you executed in a `[SQL_QUERY]` block:
-
-```
-[SQL_QUERY]
-SELECT ...
-FROM ...
-```
-
-Place this block before your analysis so users can review and trust the query.
-
-## Response Style
-- Be concise and actionable. Lead with the key finding, then provide supporting detail.
-- Use markdown tables for multi-row results.
-- Round percentages to one decimal place.
-- When results are empty, suggest why and offer an alternative query.
-- End each response with 1–2 suggested follow-up questions relevant to the data shown.
-- Do not speculate beyond what the data shows. If uncertain, say so and suggest a clarifying query.
-"""
-
-# ---------------------------------------------------------------------------
-# Tool definitions
-# ---------------------------------------------------------------------------
-
-TOOLS = [
-    QUERY_METRIC_CATALOG_TOOL,
-    {
-        "name": "execute_sql",
-        "description": (
-            "Execute a read-only SQL query against the Snowflake data warehouse. "
-            "Only SELECT, WITH, SHOW, and DESCRIBE statements are allowed. "
-            "The query is validated for safety before execution. "
-            "Returns columns and rows as JSON, or an error message."
-        ),
-        "inputSchema": {
-            "json": {
-                "type": "object",
-                "properties": {
-                    "sql": {
-                        "type": "string",
-                        "description": "The SQL query to execute.",
-                    },
-                    "params": {
-                        "type": "object",
-                        "description": "Optional bind variable values.",
-                        "additionalProperties": True,
-                    },
-                },
-                "required": ["sql"],
-            }
-        },
-    },
-]
-
-# ---------------------------------------------------------------------------
-# Tool dispatch
-# ---------------------------------------------------------------------------
-
-
-def _dispatch_tool(
-    tool_name: str, tool_input: dict, tenant_id: str | None = None
-) -> str:
-    """Execute a tool call and return the result as a JSON string."""
-    if tool_name == "execute_sql":
-        sql = tool_input.get("sql", "")
-        params = tool_input.get("params") or None
-        logger.info(f"execute_sql: {sql[:300]}")
-        result = snowflake_client.validate_and_execute(sql, params)
-        if "error" in result:
-            logger.warning(f"SQL error: {result['error']}")
-        else:
-            logger.info(f"SQL success: {len(result.get('rows', []))} rows")
-        return json.dumps(result, default=str)
-    if tool_name == "query_metric_catalog":
-        result = query_metric_catalog(
-            tool_input, database=_database, tenant_id=tenant_id
-        )
-        if "error" in result:
-            logger.warning(f"query_metric_catalog error: {result['error']}")
-        else:
-            logger.info(
-                "query_metric_catalog success: %s tenant=%s applied=%s (%d rows)",
-                result.get("metric_used", {}).get("id"),
-                tenant_id or "canonical",
-                result.get("metric_used", {}).get("applied_definition"),
-                result.get("row_count", 0),
-            )
-        return json.dumps(result, default=str)
-    return json.dumps({"error": f"Unknown tool: {tool_name}"})
-
-
-# ---------------------------------------------------------------------------
-# Sync interface
-# ---------------------------------------------------------------------------
-
-_MAX_ROUNDS = 5
+_MAX_ROUNDS = 6
 _INFERENCE_CONFIG = {"temperature": 0.0, "maxTokens": 4096}
+_STATUS = {
+    "search_catalog": "Looking up governed metrics...",
+    "query_semantic": "Running a governed query...",
+    "describe_cdm_table": "Reading the data dictionary...",
+    "execute_sql": "Running an ungoverned query...",
+}
+
+
+def _converse(messages: list) -> dict:
+    # cachePoints after the system prompt and tools let Bedrock reuse them across rounds and requests.
+    return _bedrock.converse(
+        modelId=MODEL_ID,
+        system=[{"text": SYSTEM_PROMPT}, {"cachePoint": {"type": "default"}}],
+        messages=messages,
+        toolConfig={"tools": [{"toolSpec": t} for t in _tools.specs] + [{"cachePoint": {"type": "default"}}]},
+        inferenceConfig=_INFERENCE_CONFIG,
+    )
 
 
-def send_message(
-    user_message: str,
-    history: list,
-    tenant_id: str | None = None,
-) -> tuple[str, list]:
-    """Send a user message and return (response_text, updated_messages_list).
-
-    Args:
-        user_message: The user's text input.
-        history: Existing conversation as a list of Bedrock Converse messages.
-        tenant_id: Cognito custom:tenant_id of the requesting user. Threads
-            through to query_metric_catalog so the tenant's overlay applies.
-
-    Returns:
-        Tuple of (response_text, updated_messages_list).
-    """
-    messages = list(history)
-    messages.append({"role": "user", "content": [{"text": user_message}]})
-
-    for _round in range(_MAX_ROUNDS):
-        response = _bedrock.converse(
-            modelId=MODEL_ID,
-            system=[{"text": SYSTEM_PROMPT}],
-            messages=messages,
-            toolConfig={"tools": [{"toolSpec": t} for t in TOOLS]},
-            inferenceConfig=_INFERENCE_CONFIG,
-        )
-
-        output_message = response["output"]["message"]
-        messages.append(output_message)
-
-        stop_reason = response.get("stopReason", "")
-
-        # Check for tool use
-        tool_uses = [
-            block
-            for block in output_message.get("content", [])
-            if "toolUse" in block
-        ]
-
-        if not tool_uses or stop_reason == "end_turn":
-            # Extract text response
-            text_parts = [
-                block["text"]
-                for block in output_message.get("content", [])
-                if "text" in block
-            ]
-            return "\n".join(text_parts), messages
-
-        # Dispatch all tool calls and collect results
-        tool_results = []
-        for block in tool_uses:
-            tool_use = block["toolUse"]
-            result_content = _dispatch_tool(
-                tool_use["name"],
-                tool_use.get("input", {}),
-                tenant_id=tenant_id,
-            )
-            tool_results.append(
-                {
-                    "toolResult": {
-                        "toolUseId": tool_use["toolUseId"],
-                        "content": [{"text": result_content}],
-                    }
-                }
-            )
-
-        messages.append({"role": "user", "content": tool_results})
-
-    # Exceeded max rounds — return whatever text we have
-    last = messages[-1] if messages else {}
-    text_parts = [
-        block["text"]
-        for block in last.get("content", [])
-        if isinstance(block, dict) and "text" in block
-    ]
-    return "\n".join(text_parts) or "I was unable to complete the request.", messages
-
-
-# ---------------------------------------------------------------------------
-# Async streaming interface
-# ---------------------------------------------------------------------------
-
-
-async def send_message_streaming(
-    user_message: str,
-    history: list,
-    tenant_id: str | None = None,
-):
-    """Async generator yielding status and completion events.
-
-    Yields dicts of the form:
-        {"type": "status", "message": "..."}
-        {"type": "raw_complete", "text": "...", "messages": [...]}
-
-    `tenant_id` threads through to the metric-catalog tool so the tenant's
-    overlay applies when matching a canonical metric.
-    """
-    loop = asyncio.get_event_loop()
-    messages = list(history)
-    messages.append({"role": "user", "content": [{"text": user_message}]})
-
-    def _converse_sync():
-        return _bedrock.converse(
-            modelId=MODEL_ID,
-            system=[{"text": SYSTEM_PROMPT}],
-            messages=messages,
-            toolConfig={"tools": [{"toolSpec": t} for t in TOOLS]},
-            inferenceConfig=_INFERENCE_CONFIG,
-        )
-
-    for _round in range(_MAX_ROUNDS):
-        response = await loop.run_in_executor(None, _converse_sync)
-
-        output_message = response["output"]["message"]
-        messages.append(output_message)
-
-        stop_reason = response.get("stopReason", "")
-
-        tool_uses = [
-            block
-            for block in output_message.get("content", [])
-            if "toolUse" in block
-        ]
-
-        if not tool_uses or stop_reason == "end_turn":
-            text_parts = [
-                block["text"]
-                for block in output_message.get("content", [])
-                if "text" in block
-            ]
-            full_text = "\n".join(text_parts)
-            yield {"type": "raw_complete", "text": full_text, "messages": messages}
+def _text_of(message: dict) -> str:
+    return "\n".join(b["text"] for b in message.get("content", []) if isinstance(b, dict) and "text" in b)
+
+
+def _tool_uses(message: dict) -> list[dict]:
+    return [b["toolUse"] for b in message.get("content", []) if "toolUse" in b]
+
+
+def _run_tool(tool_use: dict, artifacts: list) -> dict:
+    result = _tools.dispatch(tool_use["name"], tool_use.get("input", {}))
+    if "error" in result.content:
+        logger.warning("%s error: %s", tool_use["name"], result.content["error"])
+    artifacts.extend(result.artifacts)
+    return {"toolResult": {
+        "toolUseId": tool_use["toolUseId"],
+        "content": [{"text": json.dumps(result.content, default=str)}],
+    }}
+
+
+def send_message(user_message: str, history: list) -> tuple[str, list, list]:
+    """Returns (response_text, updated_messages, artifacts)."""
+    messages = list(history) + [{"role": "user", "content": [{"text": user_message}]}]
+    artifacts: list = []
+    for _ in range(_MAX_ROUNDS):
+        output = _converse(messages)["output"]["message"]
+        messages.append(output)
+        uses = _tool_uses(output)
+        if not uses:
+            return _text_of(output), messages, artifacts
+        messages.append({"role": "user", "content": [_run_tool(u, artifacts) for u in uses]})
+    return "I was unable to complete the request.", messages, artifacts
+
+
+async def send_message_streaming(user_message: str, history: list):
+    """Yields {"type": "status", "message"} events, then {"type": "raw_complete", "text", "messages", "artifacts"}."""
+    loop = asyncio.get_running_loop()
+    messages = list(history) + [{"role": "user", "content": [{"text": user_message}]}]
+    artifacts: list = []
+    for _ in range(_MAX_ROUNDS):
+        response = await loop.run_in_executor(None, _converse, messages)
+        output = response["output"]["message"]
+        messages.append(output)
+        uses = _tool_uses(output)
+        if not uses:
+            yield {"type": "raw_complete", "text": _text_of(output), "messages": messages, "artifacts": artifacts}
             return
-
-        # Dispatch tool calls
-        yield {"type": "status", "message": "Querying Snowflake database..."}
-
-        tool_results = []
-        for block in tool_uses:
-            tool_use = block["toolUse"]
-
-            def _dispatch_sync(name=tool_use["name"], inp=tool_use.get("input", {})):
-                return _dispatch_tool(name, inp, tenant_id=tenant_id)
-
-            result_content = await loop.run_in_executor(None, _dispatch_sync)
-            tool_results.append(
-                {
-                    "toolResult": {
-                        "toolUseId": tool_use["toolUseId"],
-                        "content": [{"text": result_content}],
-                    }
-                }
-            )
-
-        messages.append({"role": "user", "content": tool_results})
-
-        if _round < _MAX_ROUNDS - 1:
-            yield {"type": "status", "message": "Analyzing results..."}
-
-    # Exceeded max rounds
-    last = messages[-1] if messages else {}
-    text_parts = [
-        block["text"]
-        for block in last.get("content", [])
-        if isinstance(block, dict) and "text" in block
-    ]
-    full_text = "\n".join(text_parts) or "I was unable to complete the request."
-    yield {"type": "raw_complete", "text": full_text, "messages": messages}
+        results = []
+        for use in uses:
+            yield {"type": "status", "message": _STATUS.get(use["name"], "Working...")}
+            results.append(await loop.run_in_executor(None, _run_tool, use, artifacts))
+        messages.append({"role": "user", "content": results})
+    yield {"type": "raw_complete", "text": "I was unable to complete the request.",
+           "messages": messages, "artifacts": artifacts}
diff --git a/lambda_handler.py b/lambda_handler.py
index e9faad1..0b58c84 100644
--- a/lambda_handler.py
+++ b/lambda_handler.py
@@ -12,11 +12,11 @@ import os
 import json
 import logging
 import re
-import uuid
 from typing import Optional
 
 from fastapi import FastAPI, HTTPException, Header, Request
 from fastapi.middleware.cors import CORSMiddleware
+from fastapi.encoders import jsonable_encoder
 from fastapi.responses import StreamingResponse
 from pydantic import BaseModel
 
@@ -61,115 +61,6 @@ def _scrub_pii(text: str) -> str:
     return text
 
 
-# =============================================================================
-# Chart extraction from [CHART_CONFIG] text markers
-# =============================================================================
-
-_CHART_PATTERN = re.compile(r'\[CHART_CONFIG\]\s*(.*?)\s*\[/CHART_CONFIG\]', re.DOTALL)
-_SQL_QUERY_PATTERN = re.compile(r'\[SQL_QUERY\]\s*(.*?)\s*\[/SQL_QUERY\]', re.DOTALL)
-_QUERY_PARAMS_PATTERN = re.compile(r'\[QUERY_PARAMS\]\s*(.*?)\s*\[/QUERY_PARAMS\]', re.DOTALL)
-
-
-def extract_chart_configs(text: str) -> tuple[str, list[dict]]:
-    """Extract [CHART_CONFIG]{...}[/CHART_CONFIG] blocks from agent text.
-
-    Returns (cleaned_text, list_of_frontend_chart_artifacts).
-    """
-    matches = list(_CHART_PATTERN.finditer(text))
-    if not matches:
-        return text, []
-
-    charts = []
-    for match in matches:
-        try:
-            config = json.loads(match.group(1))
-            chart_type = config.get("chart_type", "bar")
-            valid_types = ["bar", "line", "pie", "scatter", "histogram"]
-            if chart_type not in valid_types:
-                chart_type = "bar"
-
-            chart_artifact = {
-                "id": str(uuid.uuid4()),
-                "type": "chart",
-                "title": config.get("title", "Chart"),
-                "data": {
-                    "chart_type": chart_type,
-                    "title": config.get("title", "Chart"),
-                    "x_axis": config.get("x_axis", ""),
-                    "y_axis": config.get("y_axis", ""),
-                    "x_label": config.get("x_label", config.get("x_axis", "")),
-                    "y_label": config.get("y_label", config.get("y_axis", "")),
-                    "data": config.get("data", []),
-                },
-            }
-            charts.append(chart_artifact)
-            logger.info(f"Extracted chart: {chart_type} '{config.get('title')}' with {len(config.get('data', []))} points")
-        except (json.JSONDecodeError, TypeError) as e:
-            logger.warning(f"Failed to parse chart config: {e}")
-
-    # Remove markers from text and clean up whitespace
-    cleaned = _CHART_PATTERN.sub('', text).strip()
-    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
-    return cleaned, charts
-
-
-# =============================================================================
-# SQL query extraction from [SQL_QUERY] text markers
-# =============================================================================
-
-def extract_sql_queries(text: str) -> tuple[str, list[dict]]:
-    """Extract [SQL_QUERY] and [QUERY_PARAMS] blocks from agent text.
-
-    Returns (cleaned_text, list_of_frontend_sql_artifacts).
-    Each artifact may include a "parameters" array if the query is parameterized.
-    """
-    # Extract any [QUERY_PARAMS] blocks first (they follow [SQL_QUERY] blocks)
-    param_blocks = []
-    for match in _QUERY_PARAMS_PATTERN.finditer(text):
-        try:
-            params = json.loads(match.group(1))
-            if isinstance(params, list):
-                param_blocks.append(params)
-        except (json.JSONDecodeError, TypeError) as e:
-            logger.warning(f"Failed to parse query params: {e}")
-
-    matches = list(_SQL_QUERY_PATTERN.finditer(text))
-    if not matches:
-        # Still clean up any orphaned param blocks
-        cleaned = _QUERY_PARAMS_PATTERN.sub('', text).strip()
-        return cleaned, []
-
-    sql_artifacts = []
-    for i, match in enumerate(matches):
-        try:
-            config = json.loads(match.group(1))
-            sql_text = config.get("sql", "")
-            title = config.get("title", "SQL Query")
-
-            if sql_text:
-                sql_artifact = {
-                    "id": str(uuid.uuid4()),
-                    "type": "sql",
-                    "title": title,
-                    "data": sql_text,
-                }
-                # Attach parameters if available (params follow their SQL block in order)
-                if i < len(param_blocks):
-                    sql_artifact["parameters"] = param_blocks[i]
-                    logger.info(f"Extracted parameterized SQL query: '{title}' with {len(param_blocks[i])} param(s)")
-                else:
-                    logger.info(f"Extracted SQL query: '{title}'")
-                sql_artifacts.append(sql_artifact)
-        except (json.JSONDecodeError, TypeError) as e:
-            logger.warning(f"Failed to parse SQL query config: {e}")
-
-    # Remove both marker types from text and clean up whitespace
-    cleaned = _SQL_QUERY_PATTERN.sub('', text).strip()
-    cleaned = _QUERY_PARAMS_PATTERN.sub('', cleaned).strip()
-    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
-    return cleaned, sql_artifacts
-
-
 # =============================================================================
 # Configuration from environment variables
 # =============================================================================
@@ -339,7 +230,6 @@ async def send_message(
     message_text: str,
     owner: str,
     context_id: Optional[str] = None,
-    tenant_id: Optional[str] = None,
 ) -> dict:
     """Send a message via chat_engine (non-streaming)."""
     import asyncio
@@ -355,21 +245,20 @@ async def send_message(
         })
 
     loop = asyncio.get_event_loop()
-    response_text, _ = await loop.run_in_executor(
-        None, lambda: engine_send(message_text, bedrock_history, tenant_id=tenant_id)
+    response_text, _, artifacts = await loop.run_in_executor(
+        None, lambda: engine_send(message_text, bedrock_history)
     )
 
     if context_id:
         save_turn(context_id, owner, message_text, response_text)
 
-    return {"text": response_text, "contextId": context_id}
+    return {"text": response_text, "artifacts": artifacts, "contextId": context_id}
 
 
 async def send_message_streaming(
     message_text: str,
     owner: str,
     context_id: Optional[str] = None,
-    tenant_id: Optional[str] = None,
 ):
     """Stream a response via chat_engine, yielding frontend events."""
     from chat_engine import send_message_streaming as engine_stream
@@ -385,13 +274,13 @@ async def send_message_streaming(
             "content": [{"text": msg["content"]}],
         })
 
-    full_text = ""
+    full_text, artifacts = "", []
     try:
-        async for event in engine_stream(message_text, bedrock_history, tenant_id=tenant_id):
+        async for event in engine_stream(message_text, bedrock_history):
             if event["type"] == "status":
                 yield event
             elif event["type"] == "raw_complete":
-                full_text = event["text"]
+                full_text, artifacts = event["text"], event["artifacts"]
                 if context_id:
                     save_turn(context_id, owner, message_text, full_text)
 
@@ -399,16 +288,10 @@ async def send_message_streaming(
             yield {"type": "error", "message": "Empty response"}
             return
 
-        # Process markers
-        cleaned_text, chart_artifacts = extract_chart_configs(full_text)
-        cleaned_text, sql_artifacts = extract_sql_queries(cleaned_text)
-        artifacts = chart_artifacts + sql_artifacts
-        cleaned_text = _scrub_pii(cleaned_text)
-
         yield {
             "type": "complete",
             "data": {
-                "text": cleaned_text,
+                "text": _scrub_pii(full_text),
                 "artifacts": artifacts,
                 "contextId": context_id,
             },
@@ -481,24 +364,11 @@ async def chat(
             message_text=message_text,
             owner=user["sub"],
             context_id=context_id,
-            tenant_id=_tenant_id_from_user(user),
         )
 
-        text = result.get("text", "")
-
-        cleaned_text, chart_artifacts = extract_chart_configs(text)
-        cleaned_text, sql_artifacts = extract_sql_queries(cleaned_text)
-        artifacts = chart_artifacts + sql_artifacts
-        cleaned_text = _scrub_pii(cleaned_text)
-
-        if chart_artifacts:
-            logger.info(f"Injected {len(chart_artifacts)} chart artifact(s) into response")
-        if sql_artifacts:
-            logger.info(f"Injected {len(sql_artifacts)} SQL artifact(s) into response")
-
         return ChatResponse(
-            text=cleaned_text,
-            artifacts=artifacts,
+            text=_scrub_pii(result.get("text", "")),
+            artifacts=jsonable_encoder(result["artifacts"]),
             context_id=result.get("contextId", context_id),
         )
 
@@ -544,7 +414,6 @@ async def chat_stream(
                 message_text=message_text,
                 owner=user["sub"],
                 context_id=context_id,
-                tenant_id=_tenant_id_from_user(user),
             ):
                 # Check if request was cancelled
                 if request_id and request_id in _cancelled_requests:
@@ -557,7 +426,8 @@ async def chat_stream(
                     _cancelled_requests.discard(request_id)
                     break
 
-                event_data = json.dumps(event)
+                # Tool artifacts carry Snowflake Decimal/date values that plain json.dumps rejects.
+                event_data = json.dumps(jsonable_encoder(event))
                 yield f"data: {event_data}\n\n"
 
         except Exception as e:
diff --git a/semantic_layer/prompt.py b/semantic_layer/prompt.py
new file mode 100644
index 0000000..4e08158
--- /dev/null
+++ b/semantic_layer/prompt.py
@@ -0,0 +1,61 @@
+"""The chat system prompt: the public semantic catalog plus the rules for using the tools."""
+
+from __future__ import annotations
+
+from .catalog_view import public_catalog
+from .schema import Catalog
+
+_RULES = """## How to answer
+1. Call `search_catalog` with the user's question to find governed metrics, measures and dimensions.
+2. Answer with `query_semantic` whenever the catalog covers the question. Prefer metrics over raw
+   measures. Use dimension names exactly as listed; time dimensions take a grain suffix
+   (`course_start_week__month`). Filters take plain dimension names. Give the result a short `title`,
+   and ask for a `chart` when a trend, distribution or comparison is clearer as one.
+3. Only when no metric, measure or dimension fits, use `describe_cdm_table` and then `execute_sql` on the
+   `{database}` database's CDM_* schemas. `execute_sql` needs a `reason` saying why the catalog does not
+   cover the question; its results are shown to the user as ungoverned.
+4. The tools return the data, SQL and provenance to the user directly. Do not repeat the SQL or
+   reproduce whole result tables in your answer.
+
+## Privacy (FERPA)
+- Dimensions marked "filter only" are personally identifiable: you may filter on them, never group by them.
+- Never try to return names, emails or other identifiers of individual people. Report counts and rates.
+
+## Style
+- Lead with the finding, then the supporting numbers. Round percentages to one decimal place.
+- Say which metric or measure you used. If results are empty, say what might explain it.
+- End with one or two follow-up questions the catalog can answer.
+"""
+
+
+def _dimension(d: dict) -> str:
+    if not d["selectable"]:
+        return f"{d['name']} (filter only)"
+    if d["type"] == "time":
+        return f"{d['name']} (time: {', '.join(d['grains'])})"
+    return d["name"]
+
+
+def build_system_prompt(catalog: Catalog, database: str) -> str:
+    view = public_catalog(catalog)
+    lines = [
+        "You are Illuminate, an analytics assistant for educational institutions. You answer questions "
+        "about courses, students, engagement and grades from a governed semantic layer.",
+        "",
+        "## Metrics",
+    ]
+    for m in view["metrics"]:
+        lines.append(f"- `{m['id']}`: {m['display_name']}. {m['description']}")
+    lines += ["", "## Datasets"]
+    for ds in view["datasets"]:
+        lines += [
+            "",
+            f"### `{ds['id']}`: {ds['display_name']}",
+            f"Grain: {ds['grain']}.",
+            "Dimensions: " + ", ".join(_dimension(d) for d in ds["dimensions"]),
+            "Measures: " + ", ".join(m["name"] for m in ds["measures"]),
+        ]
+        if ds["filters"]:
+            lines.append("Named filters (used by metrics): " + ", ".join(f["name"] for f in ds["filters"]))
+    lines += ["", _RULES.format(database=database)]
+    return "\n".join(lines)
```

- [ ] **Step 5: Verify**

Run: `$PY -m pytest -q tests`
Expected: all pass.

Live run recorded during planning: n/a (no dataset added).

- [ ] **Step 6: Commit, open the PR, merge** (PR body: the summary above plus the live-run line).

### Task 5 (PR 5f): follow-ups see the previous query contracts; conversation ids issued server-side

**Files:** `conversation_store.py`, `lambda_handler.py`, `tests/test_conversation_context.py`

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b feat/chat-history-queries origin/main
```

- [ ] **Step 2: Write the tests**

```diff
diff --git a/tests/test_conversation_context.py b/tests/test_conversation_context.py
new file mode 100644
index 0000000..e1cebce
--- /dev/null
+++ b/tests/test_conversation_context.py
@@ -0,0 +1,82 @@
+import json
+
+import boto3
+import pytest
+from fastapi.testclient import TestClient
+from moto import mock_aws
+
+import conversation_store
+import lambda_handler
+
+AUTH = {"Authorization": "Bearer test"}
+SQL_ARTIFACT = {"id": "s1", "type": "sql", "title": "Courses by term", "data": "SELECT 1",
+                "query": {"metrics": ["metric.reportable_courses.v1"], "dimensions": ["term_name"]},
+                "provenance": {"governed": True}}
+
+
+@pytest.fixture
+def table(monkeypatch):
+    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
+    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
+    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
+    monkeypatch.delenv("AWS_PROFILE", raising=False)
+    with mock_aws():
+        t = boto3.resource("dynamodb", region_name="us-east-1").create_table(
+            TableName="conversations-test",
+            KeySchema=[{"AttributeName": "context_id", "KeyType": "HASH"}],
+            AttributeDefinitions=[{"AttributeName": "context_id", "AttributeType": "S"}],
+            BillingMode="PAY_PER_REQUEST",
+        )
+        monkeypatch.setattr(conversation_store, "_table", t)
+        yield t
+
+
+@pytest.fixture
+def engine(monkeypatch):
+    import chat_engine
+    seen = []
+
+    async def fake_stream(message, history):
+        seen.append(history)
+        yield {"type": "raw_complete", "text": f"answer to {message}", "messages": [], "artifacts": [SQL_ARTIFACT]}
+
+    monkeypatch.setattr(chat_engine, "send_message_streaming", fake_stream)
+    return seen
+
+
+@pytest.fixture
+def client(table, monkeypatch):
+    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: {"sub": "bob"} if a else None)
+    return TestClient(lambda_handler.app)
+
+
+def _complete(r):
+    return [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")][-1]["data"]
+
+
+def test_turns_keep_the_queries_behind_each_answer(table):
+    conversation_store.save_turn("c1", "bob", "q", "a", queries=[{"title": "t", "query": {"metrics": ["m"]}}])
+    history = conversation_store.load_history("c1", "bob")
+    assert history[1]["queries"] == [{"title": "t", "query": {"metrics": ["m"]}}]
+
+
+def test_follow_up_questions_see_the_previous_query_contract(client, engine):
+    client.post("/api/chat/stream", headers=AUTH, json={"message": "courses by term", "context_id": "c9"})
+    client.post("/api/chat/stream", headers=AUTH, json={"message": "now by month", "context_id": "c9"})
+    previous = engine[1]
+    assistant_text = previous[1]["content"][0]["text"]
+    assert assistant_text.startswith("answer to courses by term")
+    assert '"metric.reportable_courses.v1"' in assistant_text and "Courses by term" in assistant_text
+
+
+def test_a_new_conversation_gets_a_server_generated_id(client, engine):
+    data = _complete(client.post("/api/chat/stream", headers=AUTH, json={"message": "hi"}))
+    assert data["contextId"] and conversation_store.owns(data["contextId"], "bob")
+
+
+def test_someone_elses_conversation_id_is_replaced(client, engine):
+    conversation_store.save_turn("alice-ctx", "alice", "secret q", "secret a")
+    data = _complete(client.post("/api/chat/stream", headers=AUTH, json={"message": "hi", "context_id": "alice-ctx"}))
+    assert data["contextId"] != "alice-ctx"
+    assert engine[0] == []
+    assert [m["content"] for m in conversation_store.load_history("alice-ctx", "alice")] == ["secret q", "secret a"]
```

- [ ] **Step 3: Run them to verify they fail**

Run: `$PY -m pytest -q tests/test_conversation_context.py`
Expected: 4 failures.

- [ ] **Step 4: Implement**

```diff
diff --git a/conversation_store.py b/conversation_store.py
index fdcff25..65950a8 100644
--- a/conversation_store.py
+++ b/conversation_store.py
@@ -36,6 +36,14 @@ def _item(context_id: str) -> Optional[dict]:
     return _get_table().get_item(Key={"context_id": context_id}).get("Item")
 
 
+def exists(context_id: str) -> bool:
+    try:
+        return bool(_item(context_id))
+    except Exception as e:
+        logger.warning(f"Failed to read conversation: {e}")
+        return False
+
+
 def owns(context_id: str, owner: str) -> bool:
     """True when the conversation exists and was created by owner (a Cognito sub)."""
     try:
@@ -64,9 +72,13 @@ def load_history(context_id: str, owner: str) -> list[dict]:
         return []
 
 
-def save_turn(context_id: str, owner: str, user_message: str, assistant_message: str):
+def save_turn(context_id: str, owner: str, user_message: str, assistant_message: str,
+              queries: Optional[list[dict]] = None):
     """Append a turn; never writes over a conversation that belongs to someone else.
 
+    `queries` (title, query contract or SQL, governed) are kept on the assistant message so
+    follow-up questions can build on them.
+
     Items without an owner predate ownership tracking; the first writer claims them.
     """
     if not context_id:
@@ -74,7 +86,10 @@ def save_turn(context_id: str, owner: str, user_message: str, assistant_message:
     try:
         history = load_history(context_id, owner)
         history.append({"role": "user", "content": user_message})
-        history.append({"role": "assistant", "content": assistant_message})
+        assistant = {"role": "assistant", "content": assistant_message}
+        if queries:
+            assistant["queries"] = queries
+        history.append(assistant)
         history = history[-_MAX_MESSAGES:]
 
         _get_table().put_item(
diff --git a/lambda_handler.py b/lambda_handler.py
index 0b58c84..aa1b450 100644
--- a/lambda_handler.py
+++ b/lambda_handler.py
@@ -12,6 +12,7 @@ import os
 import json
 import logging
 import re
+import uuid
 from typing import Optional
 
 from fastapi import FastAPI, HTTPException, Header, Request
@@ -226,6 +227,38 @@ class HealthResponse(BaseModel):
 # Chat Engine Wrappers
 # =============================================================================
 
+
+def _conversation_id(requested: Optional[str], owner: str) -> str:
+    """The caller's conversation id: theirs if they own it or it is unused, otherwise a new one."""
+    from conversation_store import exists, owns
+
+    if requested and (owns(requested, owner) or not exists(requested)):
+        return requested
+    return uuid.uuid4().hex
+
+
+def _queries_from(artifacts: list[dict]) -> list[dict]:
+    """What each answer ran, kept with the turn so follow-ups can modify it."""
+    out = []
+    for a in artifacts:
+        if a.get("type") != "sql":
+            continue
+        governed = a.get("provenance", {}).get("governed", False)
+        entry = {"title": a.get("title"), "governed": governed}
+        entry["query" if governed else "sql"] = a.get("query") if governed else a.get("data")
+        out.append(entry)
+    return out
+
+
+def _model_history(history: list[dict]) -> list[dict]:
+    messages = []
+    for msg in history:
+        text = msg["content"]
+        if msg.get("queries"):
+            text += "\n\nQueries behind this answer (modify these for follow-up questions):\n" + json.dumps(msg["queries"])
+        messages.append({"role": msg["role"], "content": [{"text": text}]})
+    return messages
+
 async def send_message(
     message_text: str,
     owner: str,
@@ -236,21 +269,15 @@ async def send_message(
     from chat_engine import send_message as engine_send
     from conversation_store import load_history, save_turn
 
-    history = load_history(context_id, owner) if context_id else []
-    bedrock_history = []
-    for msg in history:
-        bedrock_history.append({
-            "role": msg["role"],
-            "content": [{"text": msg["content"]}],
-        })
+    context_id = _conversation_id(context_id, owner)
+    bedrock_history = _model_history(load_history(context_id, owner))
 
     loop = asyncio.get_event_loop()
     response_text, _, artifacts = await loop.run_in_executor(
         None, lambda: engine_send(message_text, bedrock_history)
     )
 
-    if context_id:
-        save_turn(context_id, owner, message_text, response_text)
+    save_turn(context_id, owner, message_text, response_text, _queries_from(artifacts))
 
     return {"text": response_text, "artifacts": artifacts, "contextId": context_id}
 
@@ -266,13 +293,8 @@ async def send_message_streaming(
 
     yield {"type": "status", "message": "Processing your question..."}
 
-    history = load_history(context_id, owner) if context_id else []
-    bedrock_history = []
-    for msg in history:
-        bedrock_history.append({
-            "role": msg["role"],
-            "content": [{"text": msg["content"]}],
-        })
+    context_id = _conversation_id(context_id, owner)
+    bedrock_history = _model_history(load_history(context_id, owner))
 
     full_text, artifacts = "", []
     try:
@@ -281,8 +303,7 @@ async def send_message_streaming(
                 yield event
             elif event["type"] == "raw_complete":
                 full_text, artifacts = event["text"], event["artifacts"]
-                if context_id:
-                    save_turn(context_id, owner, message_text, full_text)
+                save_turn(context_id, owner, message_text, full_text, _queries_from(artifacts))
 
         if not full_text:
             yield {"type": "error", "message": "Empty response"}
```

- [ ] **Step 5: Verify**

Run: `$PY -m pytest -q tests`
Expected: all pass.

Live run recorded during planning: n/a (no dataset added).

- [ ] **Step 6: Commit, open the PR, merge** (PR body: the summary above plus the live-run line).

### Task 6 (PR 5g): semantic query errors hide warehouse internals; binary values serialise

**Files:** `lambda_handler.py`, `snowflake_client.py`, `tests/test_semantic_catalog_endpoint.py`, `tests/test_semantic_query_endpoint.py`

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b fix/semantic-query-error-hygiene origin/main
```

- [ ] **Step 2: Write the tests**

```diff
diff --git a/tests/test_semantic_catalog_endpoint.py b/tests/test_semantic_catalog_endpoint.py
index 8643ce0..91a0338 100644
--- a/tests/test_semantic_catalog_endpoint.py
+++ b/tests/test_semantic_catalog_endpoint.py
@@ -29,3 +29,13 @@ def test_matching_etag_is_304(client):
 def test_requires_a_valid_token(client, monkeypatch):
     monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: None)
     assert client.get("/api/v1/semantic/catalog", headers=AUTH).status_code == 401
+
+
+def test_etag_changes_when_the_catalog_changes(client, monkeypatch):
+    from semantic_layer import catalog as catalog_module
+    from tests.semantic_fixtures import catalog
+
+    before = client.get("/api/v1/semantic/catalog", headers=AUTH).headers["etag"]
+    monkeypatch.setattr(catalog_module, "default_catalog", lambda: catalog())
+    after = client.get("/api/v1/semantic/catalog", headers=AUTH).headers["etag"]
+    assert before != after
diff --git a/tests/test_semantic_query_endpoint.py b/tests/test_semantic_query_endpoint.py
index 0ac1d55..267f810 100644
--- a/tests/test_semantic_query_endpoint.py
+++ b/tests/test_semantic_query_endpoint.py
@@ -53,7 +53,7 @@ def test_execution_errors_are_502_with_the_sql(client, monkeypatch):
     monkeypatch.setattr(snowflake_client, "query_sql", boom)
     r = client.post("/api/v1/semantic/query", headers=AUTH, json=CONTRACT)
     assert r.status_code == 502
-    assert "warehouse suspended" in r.json()["detail"]["error"]
+    assert r.json()["detail"]["error"] == "The warehouse could not run this query."
     assert "DS_COURSE_FILTERS_V1" in r.json()["detail"]["sql"]
 
 
@@ -70,3 +70,29 @@ def test_numeric_and_date_results_serialise_as_json_numbers_and_iso_dates(client
                         lambda sql, params=None: {"columns": list(rows[0]), "rows": rows})
     r = client.post("/api/v1/semantic/query", headers=AUTH, json=CONTRACT)
     assert r.json()["rows"] == [{"TERM_NAME": "Fall", "SHARE": 0.25, "COURSES": 12, "START": "2026-08-01"}]
+
+
+def test_guard_rejection_is_a_502_with_the_guard_message(client, monkeypatch):
+    monkeypatch.setattr(snowflake_client, "validate_and_execute",
+                        lambda sql, params=None: {"error": "Schema 'X' is not in the allowed list."})
+    r = client.post("/api/v1/semantic/query", headers=AUTH, json=CONTRACT)
+    assert r.status_code == 502
+    assert "allowed list" in r.json()["detail"]["error"]
+
+
+def test_warehouse_failures_do_not_expose_snowflake_internals(client, monkeypatch):
+    def boom(sql, params=None):
+        raise RuntimeError("002003 (42S02): Object 'PROD_DB.SECRET_SCHEMA.T' does not exist or not authorized, role BBDATA_ROLE")
+
+    monkeypatch.setattr(snowflake_client, "query_sql", boom)
+    r = client.post("/api/v1/semantic/query", headers=AUTH, json=CONTRACT)
+    assert r.status_code == 502
+    assert "BBDATA_ROLE" not in r.text and "SECRET_SCHEMA" not in r.text
+    assert "warehouse" in r.json()["detail"]["error"].lower()
+
+
+def test_binary_values_serialise_as_hex(client, monkeypatch):
+    monkeypatch.setattr(snowflake_client, "query_sql",
+                        lambda sql, params=None: {"columns": ["B"], "rows": [{"B": b"\xff\x00"}]})
+    r = client.post("/api/v1/semantic/query", headers=AUTH, json=CONTRACT)
+    assert r.status_code == 200 and r.json()["rows"] == [{"B": "ff00"}]
```

- [ ] **Step 3: Run them to verify they fail**

Run: `$PY -m pytest -q tests/test_semantic_query_endpoint.py tests/test_semantic_catalog_endpoint.py`
Expected: 2 failures (internals exposed; bytes).

- [ ] **Step 4: Implement**

```diff
diff --git a/lambda_handler.py b/lambda_handler.py
index aa1b450..1dca55a 100644
--- a/lambda_handler.py
+++ b/lambda_handler.py
@@ -830,8 +830,8 @@ async def semantic_query(contract: QueryContract, authorization: Optional[str] =
     result = await loop.run_in_executor(None, lambda: validate_and_execute(compiled.sql))
     if "error" in result:
         logger.error("Semantic query failed: %s", result["error"])
-        raise HTTPException(status_code=502, detail={"error": result["error"], "sql": compiled.sql})
-    from fastapi.encoders import jsonable_encoder
+        message = "The warehouse could not run this query." if result.get("warehouse_error") else result["error"]
+        raise HTTPException(status_code=502, detail={"error": message, "sql": compiled.sql})
     from fastapi.responses import JSONResponse
 
     # jsonable_encoder turns Snowflake's Decimal into numbers; response-model serialisation makes them strings.
@@ -840,7 +840,7 @@ async def semantic_query(contract: QueryContract, authorization: Optional[str] =
         "rows": result["rows"],
         "sql": compiled.sql,
         "provenance": compiled.provenance,
-    }))
+    }, custom_encoder={bytes: bytes.hex}))
 
 
 # =============================================================================
diff --git a/snowflake_client.py b/snowflake_client.py
index ae0604d..0593610 100644
--- a/snowflake_client.py
+++ b/snowflake_client.py
@@ -223,7 +223,8 @@ def validate_and_execute(sql: str, params: dict | None = None) -> dict:
         return query_sql(stripped, params)
     except Exception as exc:
         logger.error("Snowflake execution error: %s", exc)
-        return {"error": str(exc)}
+        # warehouse_error separates Snowflake failures (internal detail) from guard rejections.
+        return {"error": str(exc), "warehouse_error": True}
 
 
 def query_sql(sql: str, params: dict | None = None) -> dict:
```

- [ ] **Step 5: Verify**

Run: `$PY -m pytest -q tests`
Expected: all pass.

Live run recorded during planning: n/a (no dataset added).

- [ ] **Step 6: Commit, open the PR, merge** (PR body: the summary above plus the live-run line).
