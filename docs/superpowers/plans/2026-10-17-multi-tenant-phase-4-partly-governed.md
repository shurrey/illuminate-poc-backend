# Multi-tenancy, Phase 4: Partly governed results and chat identity. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan
> task-by-task.

**Goal:**
- **Partly governed results.** Ask Illuminate's raw SQL can build on governed parts. Each governed part is a
  semantic query the server compiles itself, for the caller's tenant and role, and splices in as a named CTE. The
  result labels every column as governed (with its definition) or ungoverned (with the reason). Overall it is
  "partly governed".
- **Chat identity follows the role (spec goal 10).** Admins, Authors and Developers see identity in chat, through
  governed queries and raw SQL. Viewers never do.

**Spec:** `docs/superpowers/specs/2026-10-16-multi-tenant-and-mcp-design.md`, section 8 and goal 10.

**Rulings (my calls, see the ledger):**
1. **Governed parts are supplied as contracts, not compiled SQL.**
   - `execute_sql` takes `governed: {"governed_1": <query_semantic input>, ...}`.
   - The server compiles each part and builds the CTE itself, so a governed part can't be edited. That replaces the
     spec's HMAC `compiled_id` and text comparison with verification by construction. It is the same model Phase 5
     uses for saved SQL queries.
   - SQL that defines its own `governed_<n>` CTE, or references an undeclared one, is refused.
2. **Field names.** `provenance.governed` stays boolean: true only when fully governed, so existing consumers keep
   working. The new fields are `provenance.governance` (`"full" | "partial" | "none"`) and `provenance.columns`
   (`{column: {governed: bool, definition?: str, reason?: str}}`).
3. **Freehand SQL and identity.** With `allow_identity=True` (non-Viewer), the PII-column check is lifted. The
   structural checks stay for every role: star, positional, `IDENTIFIER`, alias lists, whole-row, functions, schemas,
   licence, database and `INFORMATION_SCHEMA`.

## Global Constraints

- **No cross-tenant anything.**
  - Governed parts compile against the caller's tenant catalog (overlays) and database only.
  - The tenant comes only from the token.
- **Viewers never see PII.** This holds on the governed path, the freehand path and the spliced path. Admins,
  Authors and Developers may. Roles come from `roles.role_allows_identity(user)`.
- **The guard fails closed.**
  - The whole spliced statement passes every structural and licence check.
  - Governed bodies are checked under the compiled rules with the caller's `allow_identity` and `extra_pii`.
  - The rest is checked under the freehand rules.
- **Column labels never overclaim.**
  - A column is "governed" only when it is a governed part's output passed through unchanged, aliasing allowed.
  - Anything computed, or touching a raw table, is ungoverned.
  - If tracing fails, every column is ungoverned with the reason `Column origin could not be traced`.
- **Process:**
  - Tests never call Bedrock or AWS.
  - Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  - PR bodies start with `**Claude:**` and end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
  - Never commit `illuminate-poc/tsconfig.tsbuildinfo`.
  - Comments only for non-obvious behaviour.

## Review Focus

1. **A Viewer** gets no identity through a governed part, the outer SQL, or the outer SQL selecting from a governed
   part that an Admin would see identity in. Tested in Tasks 1–2.
2. **Two governed parts over the same dataset** have no CTE name collisions after hoisting, and the result is
   correct. Tested in Task 2.
3. **The model writes SQL with a `governed_1` CTE of its own**, or one with a body that differs from the declared
   part. It is refused or ignored, never labelled governed. Tested in Task 2.
4. **An outer `SUM(governed_1.active_students)`, or a join with a raw table**, is labelled ungoverned. A plain
   passthrough of the same column is governed. Tested in Task 3.
5. **A follow-up question** after a partly governed answer can modify it. The stored query keeps the SQL and the
   governed contracts. Tested in Task 4.

---

### Task 1: Chat identity follows the role (goal 10)

**Files:** `snowflake_client.py` (the freehand PII block), `semantic_layer/chat_tools.py`, `lambda_handler.py`
(`_engine_kwargs`, chat routes); tests `tests/test_execution_guard.py`, `tests/test_chat_tools.py`,
`tests/test_tenant_isolation.py`.

**Changes:**
- **Freehand SQL with `allow_identity=True`** skips only the PII-column check (bare names, `extra_pii`, and LEARN and
  CDM dictionary PII). Every structural check stays.
- **`ChatTools(..., allow_identity: bool = False)`**:
  - `query_semantic` compiles with it;
  - `_execute_for_tenant` passes it for both compiled and freehand SQL.
- **`_engine_kwargs(tenant, user)`** passes `role_allows_identity(user)`. It applies to the chat and stream routes.

**Tests:**
- An Admin's freehand `SELECT p.EMAIL FROM CDM_LMS.PERSON p` runs.
- A Viewer's is refused.
- An Admin's `SELECT * FROM CDM_LMS.PERSON` is still refused (the star rule).
- An Admin's LEARN PII column runs on Premium.
- A Viewer's chat query that selects an identity dimension is refused, and an Admin's runs.
- Isolation: a Viewer in tenant B never gets PII through chat.

Commit `feat: chat shows identity to Admins, Authors and Developers, never Viewers`, then open the PR and merge it.

### Task 2: Splicing governed parts and the guard's governed mode

**Files:** new `semantic_layer/governed_sql.py`, `snowflake_client.py`; tests `tests/test_governed_sql.py`,
`tests/test_execution_guard.py`.

**Interfaces:**
- **`splice(outer_sql: str, governed: dict[str, QueryContract], catalog, database, *, allow_identity: bool) ->
  Spliced`**. `Spliced` holds `sql: str` and `parts: dict[str, Part]`. Each `Part` holds `contract`, `compiled`
  (CompiledQuery) and `outputs: dict[str, str]` (output column name → definition ref).
- **Part names** must match `governed_[1-9][0-9]?`. The rest of the input is refused before compiling:
  - more than 5 parts;
  - a declared name the outer SQL never references;
  - outer SQL that defines a CTE named `governed_*`.
- **Each part is compiled with `compile_query`.** Its own CTEs (`DS_*`, `g1..`) are renamed with the prefix
  `<part>__` and hoisted into one top-level `WITH`, ahead of the outer SQL's own CTEs. The part's main query becomes
  `<part> AS (...)`.
  - Rename with sqlglot, not text replace.
  - The outer SQL's `WITH`, if any, is merged after them.
- **Guard:** `validate_and_execute(..., governed_ctes: frozenset[str] = frozenset())`. The names are the part CTEs
  and their hoisted helpers.
  - Inside those CTE bodies, the compiled rules apply, with the caller's `allow_identity` and `extra_pii`, and with
    the part CTE's own projection as the "outermost projection".
  - Outside them, the freehand rules apply.
  - Columns read from a governed part resolve as derived outputs, so they aren't double-penalised. The exception:
    if the caller is a Viewer, a governed output that is an identity dimension can't exist, because the compile
    refused it.
  - The whole-row check ignores governed CTE names.
  - All structural, licence, database and function checks run over the whole statement.

**Tests:**
- Splicing one part and two parts over the same dataset gives no name collisions.
- The spliced SQL parses and every table is qualified.
- A self-defined `governed_1` is refused.
- An undeclared reference is refused.
- More than 5 parts is refused.
- A Viewer whose part asks for an identity dimension gets the compile refusal.
- The guard accepts a spliced statement whose governed bodies use star projections and inner PII, which the compiled
  rules allow and the freehand rules don't.
- The guard still refuses outer freehand PII for a Viewer.
- A Core tenant's outer SQL naming LEARN is refused.

Commit `feat: raw SQL can build on governed parts the server compiles`, then open the PR and merge it.

### Task 3: Column provenance

**Files:** `semantic_layer/governed_sql.py`; tests `tests/test_governed_sql.py`.

**Interfaces:**
- **`label(spliced: Spliced, result_columns: list[str]) -> dict`** returns
  `{"governance": "full"|"partial"|"none", "columns": {col: {...}}}`.
- **Lineage** uses `sqlglot.lineage` on the outer statement for each output column:
  - A column is governed only when its lineage reaches a single column of a single governed part, through plain
    column references and aliases only. `definition` is that part's output ref.
  - Any function, operator, aggregate, `CASE`, or a source that is a raw table or a non-governed CTE makes it
    ungoverned, with `reason` = the `execute_sql` reason.
  - `governance` is `full` if every column is governed, `none` if none is, otherwise `partial`.
  - On any lineage exception, every column is ungoverned with `Column origin could not be traced`.
- **Name matching:** result column names are compared case-insensitively with Snowflake's folded names.

**Tests:**
- passthrough → governed;
- alias → governed;
- `SUM` over a governed column → ungoverned;
- a join with a raw table: the raw column is ungoverned and the governed column stays governed;
- a column from a CTE over a raw table → ungoverned;
- a lineage failure → all ungoverned with the reason;
- `full`, `partial` and `none` cases.

Commit `feat: each column says whether it is governed`, then open the PR and merge it.

### Task 4: Chat wiring, prompt and follow-ups

**Files:** `semantic_layer/chat_tools.py`, `semantic_layer/prompt.py`, `lambda_handler.py` (`_queries_from`,
`_model_turns`); tests `tests/test_chat_tools.py`, `tests/test_chat_engine.py`, `tests/test_tenant_isolation.py`.

**Changes:**
- **`execute_sql` input** gains optional `governed` (an object of name → `query_semantic`-shaped contract).
- **The tool description and prompt rule 3** tell the model:
  - for a mixed question, put the governed part in `governed` as `governed_1`, and write SQL that selects from or
    joins `governed_1`;
  - never re-derive a governed metric by hand.
- **Provenance:**
  - With governed parts: `{governed: governance == "full", governance, columns, reason, metrics, measures,
    datasets}`. The metrics, measures and datasets are the union over the parts.
  - Without: `{governed: False, governance: "none", columns: {...all ungoverned...}, reason}`.
- **Title:** "Partly governed query result" when `governance` is `partial`.
- **`_queries_from`** stores `{"title", "governed": provenance.governed, "governance", "sql": the outer SQL,
  "governed_parts": {name: contract}}` for raw SQL, and keeps the existing shape for `query_semantic`.
  - The check uses `governance` / `governed is True`, not truthiness.
- **The model's `previous_queries`** carries the governed parts, so a follow-up can re-send them.

**Tests:**
- The tool with one governed part: the artifacts, the provenance shape, and the title.
- A scripted-Bedrock chat turn that uses a governed part, then a follow-up that replays it.
- The stored conversation keeps the governed parts.
- Isolation: B's governed part compiles against B's overlays (A has an overlay on the same metric).

Commit `feat: Ask Illuminate builds partly governed answers`, then open the PR and merge it.

### Task 5 (web app): Partly governed badge and column markers

**Files (illuminate-poc):**
- `src/types/semantic.ts` (Provenance gains `governance?` and `columns?`);
- `src/components/chat/MessageBubble.tsx` (ProvenanceBar);
- `src/components/chat/DataTable.tsx` (header markers);
- a new `src/lib/provenance.ts` (`governanceOf(p)`, `columnMark(p, column)`) with `src/lib/provenance.test.ts`;
- check the boolean consumers (`useSemanticGeneration.ts:23`, `cards/new/page.tsx:86`, `NewQuery.tsx:64`,
  `LiveKPICard.tsx:27`, `VisualCard.tsx:36`), which keep working because `governed` stays boolean.

**Behaviour:**
- **The provenance bar** for a partly governed result has the label "Partly governed" and a split icon. It lists the
  governed definitions as chips, plus the reason for the rest.
- **Each column header** has a small marker: a shield for governed, with a tooltip giving the definition, and a
  triangle for ungoverned, with the reason. Matching is case-insensitive.
- **"Pin as card"** is hidden unless fully governed.

Run `npm test`, `npx tsc --noEmit` and `npm run build`. Commit `feat: partly governed results show which columns are
governed`, then open the PR and merge it.

### Task 6: Deploy and live checks

Deploy the API, then the web app.

**Live checks:**
- **Northbridge admin:**
  - A mixed question gets a partly governed answer with column markers. For example: "active students by course,
    with each course's room from the raw CDM course table".
  - An Admin's chat question asking for student names runs.
- **Ridgeview viewer:** the same names question is refused.
- **Core LEARN:** still refused.
