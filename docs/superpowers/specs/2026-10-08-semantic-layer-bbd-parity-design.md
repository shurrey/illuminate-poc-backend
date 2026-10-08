# Semantic layer: bbd-analytics parity and semantic-first AI

- **Date:** 2026-10-08
- **Repos:** `illuminate-conversational-intelligence` (backend, owns the contract), `illuminate-poc` (frontend)
- **Reference:** `illuminate-code/bbdata-bbd-analytics` (read-only source of business logic)
- **Status:** Draft for review

## 1. Goal

Make the semantic layer the single source of truth for every analytic definition in Illuminate's
conversational and dashboard experiences:

1. Recreate every data product that bbd-analytics builds, as semantic-layer definitions computed
   at query time from the `CDM_*` schemas.
2. Make every AI and non-AI capability (chat, cards, Query Builder, Import, admin) read from the
   semantic layer. No component holds its own copy of a definition, and the frontend never
   authors, stores or executes SQL.
3. Close the security and hygiene defects found during the review of both repos.

### Success criteria

- 37 datasets (§4) compile, pass dictionary column validation, and return rows in a live smoke run
  against the POC's CDM via illuminate-mcp.
- The chat engine answers from `query_semantic` whenever a definition fits. Answers that fall back to
  freehand SQL are visibly labelled ungoverned and cannot be saved as cards.
- Cards, saved queries and chat artifacts are stored as semantic query contracts (§5.1), never SQL.
- "View SQL" anywhere in the UI shows the SQL that was actually executed.
- Every item in §8 Phase 3 and Phase 9 is closed.

### Decisions already made

| Decision | Choice |
|---|---|
| Where products get data | Recompute from `CDM_*` at query time. `BBD_ANALYTICS` is not reachable from the POC account. |
| Validation | Offline: schema, compile, column check against a dictionary snapshot. Live: illuminate-mcp smoke run per dataset. No numeric parity against `BBD_ANALYTICS` (not accessible). |
| AI fallback | Semantic-first. Freehand SQL only when nothing fits, labelled `ungoverned`. |
| Format | Extend the existing custom YAML + Pydantic layer (not Snowflake semantic views, not MetricFlow/Cube). Field names follow MetricFlow/OSI conventions so a later migration is cheap. |
| Existing dashboard cards | Not preserved. They are rebuilt as semantic contracts; old localStorage state is discarded. |

## 2. Findings that shape the design

Verified against the live CDM via illuminate-mcp on 2026-10-08:

- `CDM_LMS.TERM` is keyed by `ID`; `CDM_LMS.PERSON_COURSE` has no `TERM_ID` or `STATUS`. The 12
  chat metrics in `canonical/metrics.yaml` that use those columns are broken. The 6
  `metric.dashboard.*` metrics use the correct names.
- POC CDM tables have **no `tenant_id` column** (the POC's Snowflake is per-tenant). bbd-analytics'
  `tenant_id` joins, clustering and partitioning are dropped in every port.
- Reachable schemas: `CDM_LMS`, `CDM_SIS`, `CDM_CLB`, `CDM_MAP`, `CDM_TLM`, `CDM_META`
  (including `BBD_CALCULATION_DETAIL`), `CDM_ALY`, `CDM_MEDIA`, `CDM_CRM`, `LEARN`.
  `INFORMATION_SCHEMA` is not queryable through the MCP policy.
- Not reachable: bbd-analytics' `CREDIT_MGMT` and `ACTIVITY_RISK` (ML) sources, and its `STAGE`
  schema.

Backend defects fixed as part of this design (not separately):

- `compile_sql` appends `LIMIT 10000`; `validate_and_execute` rejects `LIMIT > 1000`, so most metrics
  fail in chat (`semantic_layer/engine.py:37,209`, `snowflake_client.py:221-232`).
- Prompt marker formats do not match the extractors in `lambda_handler.py`, so chart/SQL artifacts
  are never produced.
- `verified_queries.json` and `canonical/glossary.yaml` are never read at runtime.
- Tenant overlay `measure_sql` is rendered with an unsandboxed Jinja environment (SSTI).
- The table allowlist in `engine.py` ignores database and schema.
- `/api/v1/dashboard/query` executes arbitrary SQL with only a prefix check.
- Conversation history stores text only, so follow-ups lose prior SQL and results.

## 3. Semantic model

### 3.1 Layout (backend repo)

```
canonical/
  datasets/<domain>/<dataset>.yaml   # one file per dataset
  metrics/<domain>.yaml              # governed metrics over dataset measures
  glossary.yaml                      # synonyms → dataset / dimension / measure / metric
semantic_layer/
  schema.py      # Pydantic definitions: datasets, dimensions, measures, metrics
  contract.py    # Pydantic query contract and compile result
  render.py      # sandboxed base_sql rendering ({{ database }}, {{ ref() }} only)
  catalog.py     # load and cache; tenant overlay merging added in Phase 6b
  compiler.py    # contract → SQL
  validate.py    # dictionary column check, PII declarations
  pii.py         # PII column names, shared with the execution guard
  tool.py        # Bedrock tool specs
  (models.py and engine.py serve the legacy metrics until Phase 8 removes them)
tests/fixtures/cdm_dictionary.json   # committed dictionary snapshot + refresh script
```

### 3.2 Dataset schema

```yaml
id: dataset.active_students.v1
display_name: Active students
description: Student enrollments with in-window course activity in courses that have an instructor.
grain: one row per student enrollment (person_course)
domain: enrollment
visibility: public            # public | internal (internal datasets are ref-able, not queryable)
source: bbd-analytics snowflake/migrations/bbd_analytics/repeatable/R__HLP_PROCESS_ACTIVE_STUDENTS.sql
depends_on: [dataset.course_filters.v1]
base_sql: |
  SELECT pc.ID AS PERSON_COURSE_ID, pc.PERSON_ID, pc.COURSE_ID, ...,
         IFF(pc.AVAILABLE_IND AND pc.ENABLED_IND AND pc.ROW_DELETED_TIME IS NULL, 1, 0) AS ACTIVE
  FROM {{ database }}.CDM_LMS.PERSON_COURSE pc
  JOIN {{ ref('dataset.course_filters.v1') }} f ON ...
entities:
  - {name: person_course, column: PERSON_COURSE_ID, type: primary}
  - {name: course, column: COURSE_ID, type: foreign}
  - {name: person, column: PERSON_ID, type: foreign}
dimensions:
  - {name: term_name, column: TERM_NAME, type: categorical, synonyms: [term, semester]}
  - {name: course_start, column: START_DATE, type: time, grains: [day, week, month, quarter, year]}
measures:
  - {name: active_students, agg: count_distinct, expr: "IFF(ACTIVE = 1, PERSON_ID, NULL)",
     unit: students, synonyms: [active learners]}
  - {name: enrolled_students, agg: count_distinct, expr: PERSON_ID, unit: students}
filters:
  - {name: active_only, sql: "ACTIVE = 1", default: false}
pii_columns: [EMAIL, FIRST_NAME, LAST_NAME, ALTERNATIVE_SOURCE_ID]
```

Rules:

- `base_sql` may use only `{{ database }}` and `{{ ref('<dataset id>') }}`. Any other template
  construct is a validation error. Rendering uses Jinja's `SandboxedEnvironment`.
- `ref()` inlines the target dataset as a CTE. The dependency graph must be acyclic; shared
  dependencies are emitted once.
- `entities` declare join keys. Two datasets can be joined only through a shared entity name where at
  least one side is `primary`. The compiler rejects a join that would fan out a measure (joining a
  measure's dataset to a dataset at finer grain on a `foreign` key).
- `measures.agg` is one of `sum`, `count`, `count_distinct`, `avg`, `min`, `max`, `median`, or
  `ratio` (with `numerator` and `denominator` measure names). `expr` is a column expression over the
  dataset's own columns.
- `pii_columns` must list every column the dictionary flags as PII. They can be used in filters and
  counted, but never selected as a dimension.

### 3.3 Metric schema

```yaml
- id: metric.active_students.v1
  display_name: Active students
  description: ...
  owner: Blackboard
  authority: vendor-canonical
  last_reviewed: 2026-10-08
  measure: dataset.active_students.v1:active_students
  default_filters: [active_only]
  default_dimensions: []
  default_time_dimension: course_start
  comparison: {type: period_over_period, grain: term}   # optional; drives card change indicators
  synonyms: [...]
  example_questions: [...]
```

A card is a rendering of one or more metrics with dimensions and filters. Change indicators come
from `comparison`, not from bespoke SQL.

### 3.4 Tenant overlays

Overlays (DynamoDB `illuminate-overlays-{env}`) can override, per tenant:

- a measure's `expr`
- a filter's `sql`, or add a tenant-only filter
- a metric's `default_filters`

They cannot change `base_sql`, entities or grain. Every overlay passes through the same validator
and sandboxed compiler as canonical definitions. Overlays carry a `version` and `updated_by`. The
previous version is kept so a change can be reverted.

### 3.5 Validation

Run offline in pytest for every dataset and metric:

1. Pydantic schema.
2. Dependency graph is acyclic and every `ref()` target exists.
3. Every column referenced in `base_sql`, `expr`, filter SQL and dimension columns exists in the
   dictionary snapshot (parsed with `sqlglot`, qualified through CTE aliases).
4. Compiled SQL parses, references only `CDM_*` schemas, and passes the execution guard.
5. `pii_columns` covers every dictionary-flagged PII column the dataset exposes.

Live smoke run (illuminate-mcp, during development; recorded in each dataset PR description): row
count, one aggregate per measure, and one query per dimension.

## 4. Dataset inventory

40 reporting products in bbd-analytics' `BBD_ANALYTICS` schema produce 37 datasets. `TFV_*` views
are the SQL behind tables and are not modelled separately. `STAGE` tables are ETL plumbing.

Paths are relative to `bbdata-bbd-analytics/`. `R/` = `snowflake/migrations/bbd_analytics/repeatable/`,
`P/` = `procedures/orchestration/`.

| # | Dataset | Ports | Grain | Visibility | Source of logic |
|---|---|---|---|---|---|
| 1 | `course_filters` | FILTERS | course × IH node × course role | public | `R/R__0000_TFV_FILTERS.sql` |
| 2 | `course_filters_ih` | FILTERS_IH | as 1, IH split into levels 1–4 | public | `R/R__0001_TFV_FILTERS_IH.sql` |
| 3 | `course_filters_all` | FILTERS_ALL_COURSES | as 1, includes not-yet-started courses | public | `R/R__TFV_FILTERS_ALL_COURSES.sql` |
| 4 | `course_catalog` | COURSE_FILTER | course × IH node × role description | public | `R/R__TFV_COURSE_FILTER.sql` |
| 5 | `active_students` | ACTIVE_STUDENTS | student enrollment | public | `R/R__HLP_PROCESS_ACTIVE_STUDENTS.sql` |
| 6 | `item_tool` | ITEM_TOOL | course item × enrollment | public | `P/itemTool/processItemTool.ts` |
| 7 | `course_tool_activity` | COURSE_TOOL_ACTIVITY_HOUR | course tool × person × access time | public | `P/courseToolActivityHour/` |
| 8 | `course_tool_use` | COURSE_TOOL_USE | course × tool × event time | public | `R/R__TFV_COURSE_TOOL_USE.sql` |
| 9 | `course_item_tool_activity` | COURSE_ITEM_TOOL_ACTIVITY | course × role × student × type | public | `P/courseItemToolActivity/` |
| 10 | `course_role_activity` | COURSE_ROLE_ACTIVITY | course × course role (S, I) | public | `P/courseRoleActivity/` |
| 11 | `course_student_activity` | COURSE_STUDENT_ACTIVITY | student enrollment | public | `R/R__TFV_COURSE_STUDENT_ACTIVITY.sql` |
| 12 | `collab_session_student_activity` | COLLAB_SESSION_STUDENT_ACTIVITY | active enrollment × Collab session | public | `R/R__COLLAB_SESSION_STUDENT_ACTIVITY.sql` |
| 13 | `student_item_tool_activity` | STUDENT_ITEM_TOOL_ACTIVITY | enrollment × type | public | `R/R__TFV_STUDENT_ITEM_TOOL_ACTIVITY.sql` |
| 14 | `student_course_minutes` | STUDENT_COURSE_MINUTES_PER_CONTENT_ITEMS | student enrollment × IH node | public | `R/R__HLP_STUDENT_COURSE_MINUTES_PER_CONTENT_ITEMS.sql` |
| 15 | `student_assignments` | STUDENT_ASSIGNMENTS | active enrollment × item type × calc flag | public | `R/R__TFV_STUDENT_ASSIGNMENTS.sql` |
| 16 | `student_grade` | STUDENT_GRADE | student enrollment | public | `R/R__TFV_STUDENT_GRADE.sql` |
| 17 | `social_interactions_by_type` | STUDENT_SOCIAL_INTERACT_AND_SUB_BY_TYPE | course × item type × enrollment | public | `R/R__TFV_STUDENT_SOCIAL_INTERACT_AND_SUB_BY_TYPE.sql` |
| 18 | `sis_enrollment_attributes` | SIS_PERSON_COURSE_ATTRIBUTES | LMS enrollment | public | `R/R__TFV_SIS_PERSON_COURSE_ATTRIBUTES.sql` |
| 19 | `lms_sessions_by_slot` | PLATFORM_LMS_SESSION_BY_DAY_OF_WEEK_AND_HOUR_OF_DAY | date × 2-hour slot × IH node | public | `P/lmsSessionByDay/` |
| 20 | `activity_log` | ACTIVITY_LOG | one event, last year | public | `P/activityLog/` (rebuilt from `CDM_LMS.ACTIVITY`, `SUBMISSION`, `CDM_TLM.ULTRA_EVENTS`) |
| 21 | `student_risk_success` | STUDENT_RISK_SUCCESS | student enrollment (graded courses) | public | `R/R__STUDENT_RISK_SUCCESS.sql` |
| 22 | `grade_turnaround_by_item_type` | GRADE_COURSE_ITEM | course × item type | public | `R/R__GRADE_COURSE_ITEM.sql` |
| 23 | `grade_response_time` | GRADES_COURSE_ITEM_RESPONSE_TIME | one grade | public | `R/R__GRADES_COURSE_ITEM_RESPONSE_TIME.sql` |
| 24 | `item_tool_map` | MAP_ITEM_TOOL | item/tool handler | internal | `R/R__MAP_ITEM_TOOL.sql` |
| 25 | `calendar_day` | PLATFORM_DATE | day | internal | `R/R__0002_PLATFORM_DATE.sql` |
| 26 | `calendar_hour` | PLATFORM_DATE_HOUR | hour | internal | `R/R__0003_PLATFORM_DATE_HOUR.sql` |
| 27 | `lms_course_logins` | PLATFORM_LMS_COURSE_ACTIVITY | course login | public | `R/R__PLATFORM_LMS_COURSE_ACTIVITY.sql` |
| 28 | `lms_sessions` | PLATFORM_LMS_SESSION_ACTIVITY | LMS session (duration > 0) | public | `R/R__PLATFORM_LMS_SESSION_ACTIVITY.sql` |
| 29 | `ally_alternative_formats` | PLATFORM_ALLY_ALTERNATIVE_FORMAT | download event | public | `R/R__PLATFORM_ALLY_ALTERNATIVE_FORMAT.sql` |
| 30 | `ally_instructor_feedback` | PLATFORM_ALLY_INSTRUCTOR_FEEDBACK | feedback event | public | `R/R__PLATFORM_ALLY_INSTRUCTOR_FEEDBACK.sql` |
| 31 | `collab_attendance` | PLATFORM_CLB_ATTENDANCE | attendance row | public | `R/R__PLATFORM_CLB_ATTENDANCE.sql` |
| 32 | `collab_attendance_hourly` | PLATFORM_CLB_ATTENDANCE_HOURLY | hour × person × IH node | public | `R/R__PLATFORM_CLB_ATTENDANCE_HOURLY.sql` |
| 33 | `collab_rooms` | PLATFORM_CLB_ROOM | Collab room | public | `R/R__PLATFORM_CLB_ROOM.sql` |
| 34 | `collab_sessions_by_slot` | PLATFORM_CLB_SESSION_BY_DAY_OF_WEEK_AND_HOUR_OF_DAY | date × 2-hour slot × IH node | public | `R/R__PLATFORM_CLB_SESSION_BY_DAY_OF_WEEK_AND_HOUR_OF_DAY.sql` |
| 35 | `collab_storage_cumulative` | PLATFORM_CLB_STORAGE_CUMULATIVE_SUM | day | public | `R/R__PLATFORM_CLB_STORAGE_CUMULATIVE_SUM.sql` |
| 36 | `safeassign_originality_reports` | PLATFORM_SAFEASSIGN_ORIGINALITY_REPORT | event | public | `R/R__PLATFORM_SAFEASSIGN_ORIGINALITY_REPORT.sql` |
| 37 | `safeassign_originality_reports_basic` | PLATFORM_SAFEASSIGN_ORIGINALITY_REPORT_BASIC | event | public | `R/R__PLATFORM_SAFEASSIGN_ORIGINALITY_REPORT_BASIC.sql` |

Excluded, and why:

- `RA_CREDIT_BURNDOWN`: source `CREDIT_MGMT` is not exposed to the POC.
- `ACTIVITY_BASED_RISK_SCORE`: source `ACTIVITY_RISK` (ML) is not exposed to the POC.
- `PLATFORM_DEFAULT_FILTER`: QuickSight group filter defaults, not data.

Porting rules applied to every dataset:

- Drop `tenant_id` from joins, keys, clustering and window partitions.
- Keep bbd-analytics' exclusions: `inferred_ind`, child courses where the source excludes them,
  `%PreviewUser` and `bbsupport%` test users.
- Incremental and hash-diff procedures become plain query-time logic; bookkeeping columns are dropped.
- bbd-analytics is a read-only reference. No change is made to that repo. Where its logic is
  defective, the dataset implements the corrected behaviour, its `description` states the deviation
  in one line, and the deviation is added to §4.1.
- Configuration that bbd-analytics reads from `CDM_META.BBD_CALCULATION_DETAIL` is read from the same
  table, not hard-coded.

### 4.1 Source defects corrected in the port

Each dataset's port is checked for further defects. Any found are fixed in the dataset and added here
in the same PR.

| Source object | Defect | Correction in the dataset |
|---|---|---|
| `TFV_STUDENT_ITEM_TOOL_ACTIVITY` → `STUDENT_ITEM_TOOL_ACTIVITY` | Computes `TERM_NAME` and `COURSE_NAME`, which `INSERT_OVERWRITE_ENTITY` silently drops because the table lacks them | Dataset 13 exposes both as dimensions |
| `COURSE_TOOL_ACTIVITY_HOUR` | Named hourly, but `activity_time` is the raw access time | Dataset 7 is named `course_tool_activity`; `activity_time` is a time dimension with an `hour` grain |
| `COURSE_FILTER` vs `FILTERS` | `course_weeks` computed two ways: `ceil(datediff(day)/7)` vs week-truncated `datediff(week)+1` | Both datasets use the `FILTERS` definition (week-truncated in the tenant timezone) |
| `STUDENT_RISK_SUCCESS` | Window normalisation partitions by `COURSE_ID` only, mixing tenants | Partition by course; the POC is single-tenant per database, and the dataset must not be ported to a shared multi-tenant schema without restoring a tenant key |
| `PLATFORM_LMS_SESSION_ACTIVITY`, `PLATFORM_CLB_SESSION_BY_DAY…`, `STUDENT_COURSE_MINUTES_PER_CONTENT_ITEMS` | Joins on `login_source_id`, `session_id` or `course_id` without `tenant_id` | Same reasoning as above; joins use the complete natural key available in the POC CDM |
| `PLATFORM_CLB_STORAGE_CUMULATIVE_SUM` | Output has no tenant key | Same reasoning as above |
| `MAP_ITEM_TOOL` | Hard-coded copy of flags that `CDM_META.BBD_CALCULATION_DETAIL` also holds; the two can disagree | Dataset 24 derives the flags from `BBD_CALCULATION_DETAIL`; the hard-coded list is not ported |

The existing 18 metrics are re-expressed over these datasets, with their broken column references
fixed. Each is re-added as soon as the dataset it needs exists. The `metric.dashboard.*` IDs are
retired, and new IDs follow `metric.<name>.v1`.

## 5. Backend

### 5.1 Query contract

```json
{
  "metrics": ["metric.active_students.v1"],
  "measures": ["dataset.course_student_activity.v1:total_minutes"],
  "dimensions": ["term_name", "course_start__month"],
  "filters": [{"dimension": "term_name", "op": "in", "values": ["Fall 2026"]}],
  "time_range": {"dimension": "course_start", "start": "2026-08-01", "end": "2026-12-31"},
  "order_by": [{"field": "active_students", "direction": "desc"}],
  "limit": 100
}
```

- At least one of `metrics` or `measures` is required.
- Dimensions are named `<dimension>` or `<time dimension>__<grain>`. A dimension is resolved against
  the datasets the request uses; an ambiguous name must be qualified as
  `dataset.<id>.v1:<dimension>`.
- Filter `op` is one of `eq`, `neq`, `in`, `not_in`, `gt`, `gte`, `lt`, `lte`, `between`,
  `is_null`, `not_null`, `contains`. Values become typed literal nodes in the sqlglot AST of the
  outer query; they are never spliced into SQL text. (Driver-side binding was rejected: sqlglot
  cannot parse the connector's default `%(name)s` placeholders, and pyformat would require
  escaping every `%` in ported `LIKE` patterns.) Filters on time dimensions compare `CAST(... AS DATE)`.
- `limit` defaults to 100 with a maximum of 1000. The compiler emits the limit itself; nothing
  appends a second one.

### 5.2 Compiler

`compiler.compile_query(contract, catalog, database) -> CompiledQuery{sql, provenance}`
(Phase 6b adds the tenant argument.)

1. Resolve metrics and measures to datasets and apply tenant overlays.
2. Plan joins across datasets through declared entities, choosing the shortest path; reject fan-out.
3. Emit `ref()` CTEs once each, in dependency order.
4. Emit `SELECT <dimensions>, <aggregated measures> FROM … WHERE <filters> GROUP BY … ORDER BY … LIMIT`.
   Ratio measures aggregate numerator and denominator separately before dividing.
5. Render with `SandboxedEnvironment`.
6. Run the execution guard in `snowflake_client.validate_and_execute`: `CDM_*` schemas only, PII
   check, single statement, SELECT/WITH only. The guard's PII check is fixed so that unknown
   functions (`exp.Anonymous`) no longer count as aggregates.

`provenance` lists the metric, measure and dataset IDs and versions used, the overlay versions
applied, and `governed: true`.

### 5.3 Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/semantic/catalog` | Public datasets, dimensions, measures, metrics and synonyms, with the caller's tenant overlays merged. ETag-cached. |
| POST | `/api/v1/semantic/query` | Contract in; `{columns, rows, sql, provenance}` out. |
| POST | `/api/v1/semantic/compile` | Contract in; `{sql, provenance}` out, without executing. |
| GET/PUT/DELETE | `/api/v1/admin/overlay/{target}` | Overlay CRUD for measures, filters and metrics (§3.4). Admin role required (§8 Phase 3). |
| GET | `/api/v1/admin/overlay/{target}/history` | Previous overlay versions. |
| POST | `/api/v1/admin/overlay/{target}/revert` | Revert to a previous version. |

Removed in Phase 8: `/api/v1/dashboard/query`, `/api/v1/dashboard/metric`, `/api/v1/admin/metrics`
(replaced by the catalog).

### 5.4 Chat engine

- **System prompt:** a compact rendering of the catalog (datasets with grain, dimensions, measures,
  metrics and synonyms), plus FERPA and response-format rules. The raw data-dictionary dump is
  removed from the prompt. It is still available to the fallback path through a tool.
  The prompt uses Bedrock `cachePoint` because the catalog is stable between requests.
- **Tools:**
  - `search_catalog(question)`: ranks datasets, measures and metrics by glossary and synonym match.
  - `query_semantic(contract)`: compiles and runs a contract. Returns a capped result (at most 200
    rows to the model) plus provenance.
  - `describe_cdm_table(schema, table)`: dictionary lookup, for the fallback path only.
  - `execute_sql(sql, params, reason)`: the fallback. `reason` is required and must say why no
    definition fits. Results carry `provenance: {governed: false, reason}`.
- **Artifacts:** results are returned as structured SSE events, replacing text markers. Each event is
  `{type: table | chart | contract, query: <contract> | null, sql, provenance, chart?}`. The marker
  regexes are removed.
- **History:** each turn stores its contracts, SQL and summarised results, so follow-up questions
  ("now by month") can modify the previous contract.
- The model is configured by environment variable. The default stays `us.anthropic.claude-sonnet-4-6`
  until the model is changed deliberately.

## 6. Frontend (illuminate-poc)

The frontend never authors, stores or executes SQL.

| Surface | Behaviour |
|---|---|
| API client | `services/semanticApi.ts` (catalog, query, compile), with types generated from the backend's Pydantic models and kept in step by a script. |
| Cards (dashboard and custom) | `{id, title, viz, query: <contract>}`. The metric `comparison` field (§3.3) is added to the schema in this unit. Render via `/semantic/query`; "View SQL" via `/semantic/compile`. Change indicators come from metric `comparison`. Storage key is version-bumped and old state discarded. |
| Card builder | Two paths: choose metrics, dimensions and filters from the catalog; or describe the card in natural language, in which case the agent returns a contract for preview. Ungoverned results cannot be saved. |
| Query Builder | A structured builder over the catalog. Natural language fills the builder (the agent returns a contract); "modify" edits the contract. Compiled SQL is read-only. Saved queries are contracts. |
| Import Query | The agent maps pasted SQL to a contract and lists anything that does not map. Unmappable queries are reported and not saved. |
| Chat | Renders structured artifacts. Governed results show provenance chips (metric and dataset names) and a "Pin as card" action. Fallback results show an "Ungoverned query" badge with the stated reason. |
| Developer | A "Semantic layer" tab: datasets, relationships (Mermaid, from entities), measures and metrics. The raw CDM tab remains. |
| Admin definitions | Overlay editor for measures, filters and metric defaults, with history and revert. Hidden from users without the admin role, and enforced server-side. |
| Settings | The Snowflake configuration editor is admin-only, and enforced server-side. |

Removed: inline SQL in `src/data/dashboardCards.ts`, `src/utils/sqlParams.ts`, the prompts in
`src/hooks/useQueryGeneration.ts`, `executeQuery` and `executeMetric` in `src/services/dashboardApi.ts`,
the unused `src/data/illuminate-dictionary.json` and `src/data/illuminate-relationships.json`, and
`components/chat/ParameterizedQuery.tsx`'s raw execution.

## 7. Testing

**Backend (pytest, offline):**

- Compiler: each contract feature, join planning, fan-out rejection, ratio measures, parameter
  binding, limit handling, SSTI attempts in overlays, and non-`CDM_*` references.
- Catalog: every dataset and metric passes §3.5 checks 1–5. This is parametrised, so each new
  dataset is covered automatically.
- Chat engine: tool dispatch and artifact construction with a stubbed Bedrock client. Fallback
  requires `reason` and is tagged ungoverned.
- Endpoints: FastAPI `TestClient` with stubbed Snowflake, covering auth, tenant, admin role and
  ownership checks.

**Backend (live):** an illuminate-mcp smoke run per dataset, recorded in its PR description.

**Frontend:** `npx tsc --noEmit`, `npm run build`, ESLint (restored in Phase 9), and a manual
browser run of the changed flow per PR. Adding a frontend test framework is out of scope.

## 8. Phases

Each unit is one PR, branched from `main` in its repo. Only one PR is open at a time; work stops
for review approval and merge before the next unit starts. Each PR's description states its
reviewable surface. Spec, plan and other documentation land as separate docs PRs.

| Phase | Repo | Units |
|---|---|---|
| **1. Walking skeleton** (five PRs; 1c is the largest) | backend | 1a definition and contract models · 1b sandboxed template rendering and catalog loader · 1c compiler (single dataset, `ref()` CTEs, measures, dimensions, filters, limit) · 1d dictionary snapshot, column validator, shared PII set, dataset 1 (`course_filters`) and two metrics · 1e `POST /semantic/compile` |
| **2. API** | backend | `POST /semantic/query` · `GET /semantic/catalog` |
| **3. Security hardening** | backend, poc | (a) Make `custom:tenant_id` non-writable by users: set the user pool client's `writeAttributes` to exclude it, and add a migration note (attribute mutability cannot change in place) · (b) Admin role: a Cognito `illuminate-admins` group, checked server-side on `/admin/*` and `/config/*` · (c) Conversation ownership: store the owner `sub` on each context and check it on GET/DELETE · (d) Cancel endpoint: require JWT and check ownership of the request id · (e) Keep the Function URL's `authType: NONE` with in-app JWT validation (OAC-signed Lambda URLs would require the browser to send a SHA-256 of every POST body), and remove the unattached WAF and the unused VPC/NAT · (f) Remove `secretsmanager:PutSecretValue` unless `/config/snowflake` PUT stays, in which case keep it admin-only · (g) PII scrubber: stop redacting all 9–10 digit numbers; scrub only patterns tied to PII columns · (h) Frontend: hide admin routes for non-admins |
| **4. Core datasets** | backend | First, compiler cross-dataset joins through declared entities with fan-out rejection. Then one per PR: 2 `course_filters_ih`, 3 `course_filters_all`, 4 `course_catalog`, 5 `active_students`, 10 `course_role_activity`, 11 `course_student_activity`, 16 `student_grade`, then a metrics PR re-expressing the six dashboard metrics and the grade and enrollment metrics |
| **5. Chat grounded in the layer** | backend | `search_catalog` · `query_semantic` · catalog system prompt with caching · `describe_cdm_table` and labelled `execute_sql` fallback · structured artifacts replacing markers · turn history with contracts |
| **6. Frontend** | poc | Semantic API client and types · cards as contracts · card builder · Query Builder · Import mapping · chat artifacts, provenance and pinning · Semantic layer developer tab · admin overlay editor (after the backend overlay extension below) |
| **6b. Overlay extension** | backend | Per-measure, per-filter and metric-default overlays with versioning, history and revert (precedes the Phase 6 admin editor) |
| **7. Remaining datasets** | backend | One per PR, in dependency order: 24–26 internal helpers; 6–9, 13–14, 17 tools and content; 15, 22–23 assignments and grading; 19, 27–28 sessions; 12, 31–35 Collaborate; 18 SIS; 29–30, 36–37 Ally and SafeAssign; 21 risk; 20 activity log. Remaining legacy metrics are re-expressed in the PR that adds the dataset they need. |
| **8. Removal** | both | Remove `/dashboard/query`, `/dashboard/metric`, `/admin/metrics`, `verified_queries.json`, the old Jinja metric templates and `engine.py` paths, and the dead frontend files (§6). |
| **9. Hygiene** | both | Rewrite backend `README.md`, `docs/ARCHITECTURE.md`, `docs/API.md`, `docs/DEVELOPMENT.md` and `docs/DEPLOYMENT.md` for the Lambda + semantic layer architecture (docs PR) · Backend: add pytest config (the new catalog is cached per process from Phase 1) · Frontend: restore linting (ESLint flat config, since `next lint` was removed), fix the `AddCorsOrigin` custom resource so it updates the Lambda's `ALLOWED_ORIGINS` instead of only reading it, add `ts-node` to `infra/` devDependencies, and remove or add the `/privacy` route |

## 9. Risks

- **Query-time cost.** Products that bbd-analytics precomputes (activity aggregations, tool and
  content rollups) now run as CTE chains over raw CDM tables. Mitigations: compile-time pruning of
  unused CTE columns, required time ranges on event-level datasets (`activity_log`,
  `course_tool_activity`), and recording warehouse time in each live smoke run. If a dataset is
  impractically slow, it is flagged in its PR and handled there.
- **Logic drift without numeric parity.** Each dataset PR cites the exact bbd-analytics file it ports,
  so the reviewer can compare logic line by line. This is the parity mechanism.
- **Dictionary coverage.** If the data-dictionary snapshot lacks a schema's columns (for example
  `CDM_TLM` or `CDM_META`), the snapshot is supplemented from illuminate-mcp `describe_entity`
  output, recorded in the fixture.
- **Model behaviour.** The agent may still prefer the fallback. Tool descriptions and the prompt
  require `search_catalog` first, and fallback use is logged with its stated reason so it can be
  reviewed.
