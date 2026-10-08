# Semantic Layer Phase 2 (API) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the semantic layer to callers with `POST /api/v1/semantic/query` and `GET /api/v1/semantic/catalog`, and close the execution guard's PII bypass on the way.

**Architecture:** `/semantic/query` compiles a contract with the Phase 1 compiler, then runs it through `snowflake_client.validate_and_execute`, the same guard chat uses. `/semantic/catalog` serves a projection of the catalog with no SQL, column names or internal datasets, behind an ETag.

**Tech Stack:** Python 3.11, FastAPI, sqlglot, pytest.

**Spec:** `docs/superpowers/specs/2026-10-08-semantic-layer-bbd-parity-design.md` (§5.2 step 6, §5.3). Roadmap: `docs/superpowers/plans/2026-10-08-semantic-layer-roadmap.md` (Phase 2).

**Repo:** `illuminate-conversational-intelligence`.

## Global Constraints

- Python 3.11. `$PY` is a 3.11 interpreter with `requirements-lambda.txt` and `requirements-dev.txt` installed.
- One task = one PR from the latest `origin/main`. On this project the executor squash-merges its own PRs once the tests pass (owner's instruction, 2026-10-08).
- PR descriptions start with `**Claude:**`; commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Comments follow the owner's code-comment rules: only non-obvious behaviour; short docstrings; no narrative.
- `/dashboard/query` and `/dashboard/metric` stay until Phase 8.

## Review Focus

1. A query that fails in Snowflake (warehouse suspended, permission error) should return a clear error and the SQL that failed, not a 200 with an `error` key that clients must remember to check (Task 1: `test_execution_errors_are_502_with_the_sql`).
2. A compile error must not reach Snowflake at all (Task 1: `test_compile_errors_are_400_and_nothing_runs`).
3. PII wrapped in an unknown function, such as a UDF, must stay blocked (Task 1: `test_pii_wrapped_in_an_unknown_function_is_blocked`).
4. The catalog must never leak dataset SQL, CDM table names, filter SQL or internal datasets (Task 2: `test_lists_public_datasets_without_sql_or_columns`, `test_internal_datasets_and_their_metrics_are_hidden`).
5. A client that already holds the current catalog gets a 304 with no body (Task 2: `test_matching_etag_is_304`).

---

### Task 1 (PR 2a): `POST /api/v1/semantic/query` and guard PII fix

**Files:**
- Modify: `snowflake_client.py` (aggregate detection)
- Modify: `lambda_handler.py` (shared `_compile_contract`, new route)
- Test: `tests/test_execution_guard.py`, `tests/test_semantic_query_endpoint.py`, `tests/test_semantic_definitions.py`

**Interfaces:**
- Consumes: `compile_query`, `CompileError`, `default_catalog`, `_semantic_database` (Phase 1).
- Produces:
  - `POST /api/v1/semantic/query` (body: `QueryContract`):
    - 200 `{columns, rows, sql, provenance}`
    - 400 compile error
    - 401 bad token
    - 422 malformed contract
    - 502 `{detail: {error, sql}}` when the guard or Snowflake rejects the query
  - `_compile_contract(contract, authorization) -> CompiledQuery`, which raises `HTTPException`. Shared with `/semantic/compile`.

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b feat/semantic-query origin/main
```

- [ ] **Step 2: Write the failing guard tests**

```python
# tests/test_execution_guard.py
import pytest

import snowflake_client


@pytest.fixture
def executed(monkeypatch):
    calls = []
    monkeypatch.setattr(snowflake_client, "query_sql", lambda sql, params=None: calls.append(sql) or {"columns": [], "rows": []})
    return calls


def test_pii_wrapped_in_an_unknown_function_is_blocked(executed):
    result = snowflake_client.validate_and_execute("SELECT MY_UDF(FIRST_NAME) FROM CDM_LMS.PERSON")
    assert "personally identifiable" in result["error"]
    assert executed == []


def test_pii_under_a_real_aggregate_is_allowed(executed):
    result = snowflake_client.validate_and_execute("SELECT COUNT(DISTINCT EMAIL) FROM CDM_LMS.PERSON")
    assert "error" not in result
    assert len(executed) == 1


def test_bare_pii_column_is_blocked(executed):
    result = snowflake_client.validate_and_execute("SELECT EMAIL FROM CDM_LMS.PERSON")
    assert "personally identifiable" in result["error"]
```

Run: `$PY -m pytest -q tests/test_execution_guard.py`
Expected: `1 failed, 2 passed`. The failure is `test_pii_wrapped_in_an_unknown_function_is_blocked`.

- [ ] **Step 3: Fix aggregate detection**

```diff
@@ -190,11 +190,9 @@
     outer_select = stmt.find(exp.Select)
     if outer_select is not None:
         has_group_by = outer_select.args.get("group") is not None
-        # Check if any selected expression uses an aggregate function
-        _AGG_TYPES = (exp.Count, exp.Sum, exp.Avg, exp.Max, exp.Min, exp.Anonymous)
-
+        # Unknown functions (exp.Anonymous, e.g. a UDF) are not aggregates and must not unlock PII.
         def _has_aggregate(node):
-            return any(isinstance(n, _AGG_TYPES) for n in node.walk())
+            return any(isinstance(n, exp.AggFunc) for n in node.walk())
 
         has_aggregation = any(_has_aggregate(sel) for sel in outer_select.expressions)
```

Run: `$PY -m pytest -q tests/test_execution_guard.py`
Expected: `3 passed`.

- [ ] **Step 4: Write the failing endpoint tests**

```python
# tests/test_semantic_query_endpoint.py
import pytest
from fastapi.testclient import TestClient

import lambda_handler
import snowflake_client

AUTH = {"Authorization": "Bearer test"}
CONTRACT = {"metrics": ["metric.reportable_courses.v1"], "dimensions": ["term_name"]}


@pytest.fixture
def executed(monkeypatch):
    calls = []

    def fake_query_sql(sql, params=None):
        calls.append(sql)
        return {"columns": ["TERM_NAME", "REPORTABLE_COURSES"], "rows": [{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3}]}

    monkeypatch.setattr(snowflake_client, "query_sql", fake_query_sql)
    return calls


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_DATABASE", "TESTDB")
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: {"sub": "u1"})
    return TestClient(lambda_handler.app)


def test_runs_the_compiled_sql_through_the_guard(client, executed):
    r = client.post("/api/v1/semantic/query", headers=AUTH, json=CONTRACT)
    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == [{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3}]
    assert body["columns"] == ["TERM_NAME", "REPORTABLE_COURSES"]
    assert executed == [body["sql"]]
    assert body["provenance"]["metrics"] == ["metric.reportable_courses.v1"]


def test_compile_errors_are_400_and_nothing_runs(client, executed):
    r = client.post("/api/v1/semantic/query", headers=AUTH, json={"metrics": ["metric.nope.v1"]})
    assert r.status_code == 400
    assert executed == []


def test_execution_errors_are_502_with_the_sql(client, monkeypatch):
    def boom(sql, params=None):
        raise RuntimeError("warehouse suspended")

    monkeypatch.setattr(snowflake_client, "query_sql", boom)
    r = client.post("/api/v1/semantic/query", headers=AUTH, json=CONTRACT)
    assert r.status_code == 502
    assert "warehouse suspended" in r.json()["detail"]["error"]
    assert "DS_COURSE_FILTERS_V1" in r.json()["detail"]["sql"]


def test_requires_a_valid_token(client, executed, monkeypatch):
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: None)
    r = client.post("/api/v1/semantic/query", headers=AUTH, json=CONTRACT)
    assert r.status_code == 401
    assert executed == []
```

Run: `$PY -m pytest -q tests/test_semantic_query_endpoint.py`
Expected: `4 failed` (404).

- [ ] **Step 5: Add the route**

```diff
@@ -863,9 +863,8 @@
     return _database
 
 
-@app.post("/api/v1/semantic/compile")
-async def semantic_compile(contract: QueryContract, authorization: str = Header(...)) -> dict:
-    """Compile a semantic query contract to SQL without executing it."""
+def _compile_contract(contract: QueryContract, authorization: str):
+    """Authenticate, then compile; raises the HTTP error the caller should return."""
     user = _get_user_from_token(authorization)
     if not user:
         raise HTTPException(status_code=401, detail="Invalid or expired token")
@@ -874,10 +873,31 @@
     from semantic_layer.compiler import CompileError, compile_query
 
     try:
-        compiled = compile_query(contract, default_catalog(), _semantic_database())
+        return compile_query(contract, default_catalog(), _semantic_database())
     except CompileError as e:
         raise HTTPException(status_code=400, detail=str(e))
-    return compiled.model_dump()
+
+
+@app.post("/api/v1/semantic/compile")
+async def semantic_compile(contract: QueryContract, authorization: str = Header(...)) -> dict:
+    """Compile a semantic query contract to SQL without executing it."""
+    return _compile_contract(contract, authorization).model_dump()
+
+
+@app.post("/api/v1/semantic/query")
+async def semantic_query(contract: QueryContract, authorization: str = Header(...)) -> dict:
+    """Compile a semantic query contract and run it through the execution guard."""
+    compiled = _compile_contract(contract, authorization)
+
+    import asyncio
+    from snowflake_client import validate_and_execute
+
+    loop = asyncio.get_event_loop()
+    result = await loop.run_in_executor(None, lambda: validate_and_execute(compiled.sql))
+    if "error" in result:
+        logger.error("Semantic query failed: %s", result["error"])
+        raise HTTPException(status_code=502, detail={"error": result["error"], "sql": compiled.sql})
+    return {**result, **compiled.model_dump()}
 
 
 # =============================================================================
```

Run: `$PY -m pytest -q tests`
Expected: `119 passed`.

- [ ] **Step 6: Pin spec §3.5.4: every compiled metric passes the guard**

Append to `tests/test_semantic_definitions.py`. This is a coverage test for a spec requirement, not a behaviour change, so it passes as soon as it is written:

```python
@pytest.mark.parametrize("metric", CATALOG.metrics.values(), ids=lambda m: m.id)
def test_compiled_metric_passes_the_execution_guard(metric, monkeypatch):
    import snowflake_client

    monkeypatch.setattr(snowflake_client, "query_sql", lambda sql, params=None: {"columns": [], "rows": []})
    compiled = compile_query(QueryContract(metrics=[metric.id]), CATALOG, "DB")
    assert snowflake_client.validate_and_execute(compiled.sql) == {"columns": [], "rows": []}
```

Run: `$PY -m pytest -q tests`
Expected: `121 passed`.

- [ ] **Step 7: Commit, open the PR, merge**

```bash
git add snowflake_client.py lambda_handler.py tests/test_execution_guard.py tests/test_semantic_query_endpoint.py tests/test_semantic_definitions.py
git commit -m "feat: POST /api/v1/semantic/query; guard no longer treats unknown functions as aggregates

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin feat/semantic-query
gh pr create --base main --title "feat: POST /api/v1/semantic/query; guard no longer treats unknown functions as aggregates" --body "$(cat <<'EOF'
**Claude:** Phase 2a (spec §5.2 step 6, §5.3). `/semantic/query` compiles a contract and runs it through the same `validate_and_execute` guard chat uses, returning rows plus the SQL and provenance. A compile error is a 400 and never reaches Snowflake; a guard or warehouse error is a 502 with the SQL that failed. The guard's PII check counted any unknown function (`exp.Anonymous`, e.g. a UDF) as an aggregate, so `SELECT MY_UDF(FIRST_NAME)` passed; only real aggregates (`exp.AggFunc`) now unlock PII columns.

Reviewable surface: 5 files, ~160 lines of code and tests.

Test plan: `pytest tests` passes locally (Python 3.11).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge feat/semantic-query --squash --delete-branch
```

### Task 2 (PR 2b): `GET /api/v1/semantic/catalog`

**Files:**
- Create: `semantic_layer/catalog_view.py`
- Modify: `lambda_handler.py` (new route above `/semantic/query`)
- Test: `tests/test_semantic_catalog_view.py`, `tests/test_semantic_catalog_endpoint.py`

**Interfaces:**
- Consumes: `Catalog`, `default_catalog` (Phase 1); `tests.semantic_fixtures`.
- Produces:
  - `public_catalog(catalog) -> {datasets: [...], metrics: [...]}`.
    - Each dataset carries `id`, `display_name`, `description`, `grain`, `domain`, `dimensions`, `measures` and `filters`.
    - Each dimension carries `name`, `type`, `description`, `grains`, `synonyms` and `selectable`, which is false for PII dimensions.
    - Each measure carries `name`, `agg`, `numerator`, `denominator`, `unit`, `description` and `synonyms`.
    - Each filter carries `name` and `description`.
    - Metrics are serialized as their full model. Phase 6 frontend pickers and Phase 5 prompts build on this shape.
  - `GET /api/v1/semantic/catalog` returns 200 with an `ETag` header. A request whose `If-None-Match` equals that ETag gets 304 with an empty body.

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b feat/semantic-catalog origin/main
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_semantic_catalog_view.py
import json

from semantic_layer.catalog_view import public_catalog
from tests.semantic_fixtures import ENROLLMENTS, STUDENT_ENROLLMENTS, catalog, dataset


def test_lists_public_datasets_without_sql_or_columns():
    view = public_catalog(catalog())
    [ds] = view["datasets"]
    assert ds["id"] == "dataset.enrollments.v1"
    text = json.dumps(view)
    assert "base_sql" not in text and "CDM_LMS" not in text
    assert "COURSE_ROLE = 'S'" not in text
    assert all("column" not in d and "expr" not in d for d in ds["dimensions"] + ds["measures"])


def test_pii_dimensions_are_marked_unselectable():
    dims = {d["name"]: d for d in public_catalog(catalog())["datasets"][0]["dimensions"]}
    assert dims["email"]["selectable"] is False
    assert dims["course_role"]["selectable"] is True


def test_internal_datasets_and_their_metrics_are_hidden():
    hidden = dataset(visibility="internal")
    view = public_catalog(catalog(hidden))
    assert view["datasets"] == [] and view["metrics"] == []


def test_metrics_carry_their_definition_metadata():
    [m] = public_catalog(catalog())["metrics"]
    assert m["id"] == STUDENT_ENROLLMENTS.id
    assert m["measure"] == "dataset.enrollments.v1:enrollments"
    assert m["default_filters"] == ["students"]
    assert m["last_reviewed"] == "2026-10-08"


def test_filters_expose_name_and_description_only():
    [f] = public_catalog(catalog())["datasets"][0]["filters"]
    assert f == {"name": "students", "description": ""}
```

```python
# tests/test_semantic_catalog_endpoint.py
import pytest
from fastapi.testclient import TestClient

import lambda_handler

AUTH = {"Authorization": "Bearer test"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: {"sub": "u1"})
    return TestClient(lambda_handler.app)


def test_returns_the_catalog_with_an_etag(client):
    r = client.get("/api/v1/semantic/catalog", headers=AUTH)
    assert r.status_code == 200
    assert "dataset.course_filters.v1" in [d["id"] for d in r.json()["datasets"]]
    assert r.headers["etag"].startswith('"')


def test_matching_etag_is_304(client):
    etag = client.get("/api/v1/semantic/catalog", headers=AUTH).headers["etag"]
    r = client.get("/api/v1/semantic/catalog", headers=AUTH | {"If-None-Match": etag})
    assert r.status_code == 304
    assert r.content == b""


def test_requires_a_valid_token(client, monkeypatch):
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: None)
    assert client.get("/api/v1/semantic/catalog", headers=AUTH).status_code == 401
```

Run: `$PY -m pytest -q tests/test_semantic_catalog_view.py tests/test_semantic_catalog_endpoint.py`
Expected: collection error, `ModuleNotFoundError: No module named 'semantic_layer.catalog_view'`.

- [ ] **Step 3: Implement the view**

```python
# semantic_layer/catalog_view.py
"""The catalog as callers see it: public datasets and metrics, without SQL or column names."""

from __future__ import annotations

from .schema import Catalog, Dataset


def _dataset_view(ds: Dataset) -> dict:
    pii = set(ds.pii_columns)
    return {
        "id": ds.id,
        "display_name": ds.display_name,
        "description": ds.description,
        "grain": ds.grain,
        "domain": ds.domain,
        "dimensions": [
            d.model_dump(exclude={"column"}) | {"selectable": d.column not in pii}
            for d in ds.dimensions
        ],
        "measures": [m.model_dump(exclude={"expr"}) for m in ds.measures],
        "filters": [{"name": f.name, "description": f.description} for f in ds.filters],
    }


def public_catalog(catalog: Catalog) -> dict:
    public = {i: d for i, d in catalog.datasets.items() if d.visibility == "public"}
    return {
        "datasets": [_dataset_view(d) for d in public.values()],
        "metrics": [
            m.model_dump(mode="json") for m in catalog.metrics.values() if m.dataset_id in public
        ],
    }
```

- [ ] **Step 4: Add the route**

```diff
@@ -884,6 +884,29 @@
     return _compile_contract(contract, authorization).model_dump()
 
 
+@app.get("/api/v1/semantic/catalog")
+async def semantic_catalog(
+    authorization: str = Header(...),
+    if_none_match: Optional[str] = Header(default=None),
+):
+    """Public datasets, dimensions, measures and metrics; supports If-None-Match."""
+    user = _get_user_from_token(authorization)
+    if not user:
+        raise HTTPException(status_code=401, detail="Invalid or expired token")
+
+    import hashlib
+    from fastapi import Response
+    from fastapi.responses import JSONResponse
+    from semantic_layer.catalog import default_catalog
+    from semantic_layer.catalog_view import public_catalog
+
+    body = public_catalog(default_catalog())
+    etag = '"' + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:32] + '"'
+    if if_none_match == etag:
+        return Response(status_code=304, headers={"ETag": etag})
+    return JSONResponse(body, headers={"ETag": etag})
+
+
 @app.post("/api/v1/semantic/query")
 async def semantic_query(contract: QueryContract, authorization: str = Header(...)) -> dict:
     """Compile a semantic query contract and run it through the execution guard."""
```

- [ ] **Step 5: Run the suite**

Run: `$PY -m pytest -q tests`
Expected: `129 passed`.

- [ ] **Step 6: Commit, open the PR, merge**

```bash
git add semantic_layer/catalog_view.py lambda_handler.py tests/test_semantic_catalog_view.py tests/test_semantic_catalog_endpoint.py
git commit -m "feat: GET /api/v1/semantic/catalog

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin feat/semantic-catalog
gh pr create --base main --title "feat: GET /api/v1/semantic/catalog" --body "$(cat <<'EOF'
**Claude:** Phase 2b (spec §5.3). Serves the public catalog that frontend pickers and the chat prompt will be built from: datasets with grain, dimensions (PII ones marked unselectable), measures and filters, plus metrics. No dataset SQL, CDM table or column names, filter SQL, or internal datasets are exposed. Responses carry an ETag, and a matching `If-None-Match` gets a 304.

Reviewable surface: 4 files, ~130 lines of code and tests.

Test plan: `pytest tests` passes locally (Python 3.11).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge feat/semantic-catalog --squash --delete-branch
```
