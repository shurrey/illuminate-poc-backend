# Multi-tenancy, Phase 5: SQL saved queries. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan
> task-by-task.

**Goal:** A saved query can be raw SQL, optionally embedding governed parts, as well as a governed query.
- **Three ways to make one:** write it in the query builder's SQL mode, import pasted SQL that can't be fully
  governed, or keep a result from Ask Illuminate.
- **Checked twice:** at save time it is validated as the saver, without fetching rows. At every run it is run as the
  runner.
- **Labelled:** the result is marked governed, partly governed or ungoverned wherever it appears.

**Spec:** `docs/superpowers/specs/2026-10-16-multi-tenant-and-mcp-design.md`, section 4a. Section 7's
`/api/v1/sql/*` endpoints are built here too, for the MCP in Phase 7.

## Global Constraints

- **Tenant and role** come only from the token. No cross-tenant anything. Viewers never see PII. A Core tenant never
  reaches LEARN.
- **The guard fails closed.** Every saved SQL query passes the guard as the saver, at save time. Every run passes it
  as the runner.
- **Governed parts** are stored as contracts and recompiled for the runner at every run. They are never stored as
  compiled SQL.
- **Labels never overclaim.** The Phase 4 `label` rules apply to every SQL run.
- **Cards stay governed single-value queries** (spec 4a "not in scope").
- **Process:**
  - Tests never call AWS or Bedrock.
  - Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  - PR bodies start with `**Claude:**` and end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
  - Push with `git -c credential.helper= -c 'credential.helper=!gh auth git-credential' push`.
  - Never commit `illuminate-poc/tsconfig.tsbuildinfo`.
  - Comments only for non-obvious behaviour.

## Review Focus

1. **Saver vs runner:** an Admin saves SQL that selects an email. A Viewer in the same tenant runs it (via sharing in
   Phase 6, or simply as their own copy) and is refused. The Admin running it succeeds. Tested in Task 3.
2. **A Core tenant saving or importing LEARN SQL** is refused with the Premium message, both on save and on run.
   Tested in Task 3.
3. **A saved query whose governed part's definition later changes** (via an overlay) runs with the new definition.
   Tested in Task 3.
4. **Validation without fetching:** save-time validation makes no warehouse call, or only a zero-row `LIMIT 0`
   explain. Tested in Task 1.
5. **A stored SQL query edited outside the API** (e.g. a tampered row) is still refused by the guard at run time,
   because the guard re-splices from contracts. Tested in Tasks 1 and 3.

---

### Task 1: Split the guard — `GuardContext`, `validate`, `execute`

**Files:** `snowflake_client.py`, `semantic_layer/governed_sql.py`, all callers (`lambda_handler.py`,
`semantic_layer/chat_tools.py`, report paths, `_current_terms`); tests `tests/test_execution_guard.py`,
`tests/test_guard_split.py`.

**Interfaces:**
- **`GuardContext`:** `@dataclass(frozen=True) class GuardContext: tenant: Tenant; allow_identity: bool; extra_pii:
  frozenset[str]; catalog: Catalog; database: str`. It is built once per request by a helper `guard_context(tenant,
  user)` in lambda_handler, which gets `extra_pii` from the tenant catalog.
- **Modes:**
  - `Freehand()`.
  - `Compiled(compiled: CompiledQuery)`: the SQL must equal `compiled.sql`, AST-normalized.
  - `Spliced(outer_sql: str, governed: dict[str, QueryContract])`: the guard calls `splice(outer_sql, governed,
    ctx.catalog, ctx.database, allow_identity=ctx.allow_identity)` itself and validates its output. It never trusts
    compiled SQL from the caller.
- **`validate(sql: str | None, ctx: GuardContext, mode) -> Validated | Refusal`:**
  - for `Spliced`, `sql` is None and the spliced SQL is produced inside;
  - `Validated` holds `sql`, the parsed `stmt`, the `spliced` result if any, and `mode`;
  - `Refusal` holds `message`.
  - It makes no warehouse call.
- **`execute(v: Validated, ctx) -> Result`:** `Result` holds `columns`, `rows`, `truncated`, and an `error` with
  the fail-closed message from Phase 4. It also logs per Phase 4's rules.
- **Compatibility:** `validate_and_execute(...)` stays as a thin wrapper (validate, then execute, returning the
  existing dict shape) until every caller has moved. Then it is deleted in this task. The final state has no wrapper.

**Tests:**
- every existing guard test passes through `validate` and `execute`;
- `validate` never calls `query_sql` (asserted with a fake);
- `Compiled` mode with SQL that differs from `compiled.sql` is refused;
- `Spliced` mode ignores any caller-supplied compiled SQL;
- a tampered stored contract is still checked.

Commit `refactor: the guard validates and executes in separate steps`, then open the PR and merge it.

### Task 2: `/api/v1/sql/run`, `/api/v1/sql/validate`, `/api/v1/schema/describe`, and one shared service

**Files:**
- `semantic_layer/sql_service.py` (new);
- `semantic_layer/chat_tools.py` (`execute_sql` and `describe_cdm_table` call the service);
- `lambda_handler.py`;
- tests `tests/test_sql_routes.py` and isolation cases.

**Interfaces:**
- **`run_sql(ctx, *, sql, governed: dict[str, dict] | None, reason: str) -> dict`.** It returns
  `{columns, rows, truncated, provenance, sql, executed_sql?, governed_parts?}`. `provenance` is exactly the Phase 4
  chat shape. Errors are `{"error": message}`.
- **`validate_sql(ctx, *, sql, governed) -> dict`.** It returns `{"ok": true, "governance_preview": ...}` or
  `{"ok": false, "error": ...}`. There is no warehouse call.
- **`describe(ctx, schema, table)`** is the chat describe logic, including the LEARN licence rules.
- **Routes**, all calling `tenant_for` first, with ctx from the token:
  - `POST /api/v1/sql/run` with body `{sql, governed?, reason?}`. `reason` defaults to `"Written in the query
    builder"`.
  - `POST /api/v1/sql/validate`, same body.
  - `GET /api/v1/schema/describe?schema=&table=`.
- **No governed-first rule on these routes.** A person writing SQL in the query builder isn't an AI. Phase 7 adds the
  governed-first rule for MCP sessions on top.
- **Chat's `execute_sql` and `describe_cdm_table`** now call the service, with no behaviour change; the chat tests
  still pass.

**Tests:**
- run with and without parts;
- the labels;
- a Viewer is refused PII;
- Core LEARN is refused;
- validate makes no warehouse call;
- describe follows the licence rules;
- isolation `*_never_crosses` for all three routes, and they are added to `NO_SECRET_ROUTES` / `SECRET_ROUTES` as
  appropriate.

Commit `feat: SQL can be run and validated outside chat`, then open the PR and merge it.

### Task 3: Saved SQL queries

**Files:** `lambda_handler.py` (the `/me/queries` routes); tests `tests/test_me_queries.py`, isolation cases.

**Changes:**
- **`POST/PUT /api/v1/me/queries`** accept `query_kind: "governed" | "sql"`, defaulting to governed. An SQL query's
  body is `{name, description, query_kind: "sql", sql, governed_parts: {name: contract}}`.
  - At most 20,000 characters of SQL.
  - The governed parts are validated as contracts.
  - **On save, `validate_sql` runs as the saver**, so a refusal is a 400 with the guard message. This includes the
    Core LEARN refusal.
  - `description` is optional. When it is empty, the run reason defaults to `"Written by <owner name>"`. The owner
    name comes from the token's `name` or `email` claim at save time and is stored as `owner_name`.
- **`POST /api/v1/me/queries/{id}/run`** runs either kind for the caller (the runner):
  - **governed:** compile and execute as `/semantic/query` does;
  - **sql:** `run_sql` with the stored SQL and parts, `reason` = description or the default.
  - It returns the same shape as `/sql/run`, or `/semantic/query`'s shape for governed. It also sets `lastUsedAt`.
- **The list response** carries `query_kind` and a `governance_hint`:
  - `"governed"` for governed queries;
  - for SQL queries, `"partial"` if there are governed parts, otherwise `"ungoverned"`.

  It is a hint only; the real label comes from the run.

**Tests:**
- save and run SQL with and without parts;
- an Admin saves email SQL: the Admin runs it, and a Viewer's run of the same body (a separate user saving the same
  SQL) is refused at save and at run;
- Core LEARN is refused at save;
- an overlay change is reflected in the next run;
- a tampered stored part (direct store write) is still guarded;
- the length limit;
- isolation.

Commit `feat: saved queries can be SQL with governed parts`, then open the PR and merge it.

### Task 4 (web app): SQL mode, save SQL from import and chat, run saved SQL

**Files (illuminate-poc):**
- `src/services/sqlApi.ts` (new);
- `src/components/queries/NewQuery.tsx` (gains an SQL mode);
- `ImportQuery.tsx`;
- `MyQueries.tsx`;
- `src/components/chat/MessageBubble.tsx` ("Save query");
- `src/context/QueryBuilderContext.tsx` (types, `saveQuery` for SQL);
- `src/types/queryBuilder.ts`;
- pure helpers in `src/lib/savedQuery.ts` with tests.

**Behaviour:**
- **Query builder** gains a "SQL" tab:
  - an editor (a plain textarea with a monospace font; no new heavy dependency);
  - Run, which calls `/sql/run`; the result table and provenance bar reuse the chat `DataTable`/`ProvenanceBar`, with
    column markers;
  - Save, with name and optional description, which calls `saveQuery` with `query_kind: "sql"`. Server refusals are
    shown inline.
- **Import:**
  - when the conversion isn't fully governed, offer "Save as SQL". It saves the pasted SQL, plus any governed parts
    the conversion recognised.
  - A Core tenant with LEARN SQL still gets the Premium message, from the server.
- **Ask Illuminate:** "Save query" on any result.
  - A governed result saves its contract (the existing path).
  - An SQL result saves its outer SQL plus `governed_parts`.
- **My Queries** lists both kinds, with a governed / partly governed / ungoverned hint chip.
  - "Run" on an SQL query calls `/me/queries/{id}/run` and shows the result with labels.
  - "Open" loads it into the right tab.

**Checks:** run `npm test`, `npx tsc --noEmit` and `npm run build`.

Commit `feat: write, import and save SQL queries`, then open the PR and merge it.

### Task 5: Deploy and live checks

Deploy the API, then the web app.

**Live checks:**
- **Northbridge admin:**
  - saves an SQL query with a governed part;
  - runs it, and it comes back partly governed;
  - saves email SQL and runs it, and gets emails.
- **Ridgeview viewer:**
  - saves the same email SQL and is refused;
  - saves LEARN SQL, and Core refuses it.
- **Clean up** the test queries afterwards.
