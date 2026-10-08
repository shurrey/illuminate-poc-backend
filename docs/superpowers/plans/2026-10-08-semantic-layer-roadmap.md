# Semantic Layer Roadmap

> **For agentic workers:** This is the ordered list of every unit in the spec. It is not executable on its own. Each phase gets a detailed, step-by-step plan (`2026-10-08-semantic-layer-phase-<n>-<name>.md`) written when the previous phase has merged, against the code that actually landed. Phase 1's detailed plan exists: `2026-10-08-semantic-layer-phase-1-skeleton.md`.

**Spec:** `docs/superpowers/specs/2026-10-08-semantic-layer-bbd-parity-design.md`

**Repos:** `B` = `illuminate-conversational-intelligence` (backend), `F` = `illuminate-poc` (frontend).

**Rules for every unit:**
- One unit is one PR, branched from the latest `main` of its repo.
- Open the PR, then stop until it is approved and merged.
- Spec and plan documents land in separate docs PRs.

"Done when" is the acceptance check, in addition to the test suite passing.

## Phase 1: Walking skeleton (B), complete (#9–#14)

| Unit | Scope | Done when |
|---|---|---|
| 1a | Definition and contract models | Model tests pass |
| 1b | Sandboxed rendering and catalog loader | Template injection tests rejected |
| 1c | Compiler (single dataset) | Compiler tests pass |
| 1d | Validator, dictionary snapshot, PII set, dataset 1, two metrics | Live smoke run recorded |
| 1e | `POST /semantic/compile` | Endpoint tests pass |

## Phase 2: API (B), complete (#16–#18)

| Unit | Scope | Done when |
|---|---|---|
| 2a | `POST /semantic/query`: compile, then `validate_and_execute`. Response is `{columns, rows, sql, provenance}`. Fix the guard's `exp.Anonymous`-as-aggregate PII bypass here, because this is the first new caller. | A stubbed-Snowflake endpoint test passes; a live query returns rows |
| 2b | `GET /semantic/catalog`: public datasets, dimensions, measures, metrics and synonyms, with an ETag | Response contains no internal datasets and no `base_sql` |

## Phase 3: Security hardening (B, then F), complete (#20–#27, frontend #5); deploy steps: `docs/runbooks/deploy-security-hardening.md`

Found while planning: the backend has no `/api/v1/config/snowflake` route, so 3f drops `PutSecretValue` outright and 3h removes the frontend editor that called it.

| Unit | Scope | Done when |
|---|---|---|
| 3a | `custom:tenant_id` not user-writable: the user pool client's `writeAttributes` excludes it, plus a migration note | `cdk synth` shows the client's write attributes without `custom:tenant_id` |
| 3b | `illuminate-admins` Cognito group, checked server-side on `/admin/*` and `/config/*` | A non-admin token gets 403 |
| 3c | Conversations record the owner `sub`; GET and DELETE check it | Another user's `context_id` gets 404 |
| 3d | Cancel requires a JWT and ownership of the request id | Unauthenticated cancel gets 401 |
| 3e | Remove the unattached WAF and the unused VPC/NAT; document why the Function URL stays JWT-only | `cdk diff` shows only removals |
| 3f | IAM least privilege: drop `secretsmanager:PutSecretValue` unless `/config/snowflake` PUT stays admin-only | Policy diff reviewed |
| 3g | PII scrubber: stop redacting every 9- or 10-digit number; key redaction to PII patterns | A count of 123456789 survives; an SSN is still redacted |
| 3h (F) | Hide admin routes and the Settings Snowflake editor for non-admins (reads the `cognito:groups` claim) | Non-admins can't see them in the browser |

## Phase 4: Core datasets (B), detailed plan: `2026-10-08-semantic-layer-phase-4-core-datasets.md`

Decisions from planning:
- Dashboard metrics are re-expressed as the measure they count; the period-over-period comparison arrives with the metric `comparison` field in 6c.
- Instructor grading engagement waits for datasets 22–23 in Phase 7.
- Rolling-window retention can't be expressed as a single measure and has no bbd-analytics product; decide in 6c whether it gets a dedicated dataset or the card is dropped.
- Legacy `course_completion_rate` reads `PERSON_COURSE.STATUS`, which the CDM doesn't have, so it is not re-expressed.
- The dictionary-snapshot supplement now exists (`tests/fixtures/cdm_dictionary_supplement.json`).

| Unit | Scope | Done when |
|---|---|---|
| 4a | Compiler cross-dataset joins through declared entities: shortest path, fan-out rejection, column qualification | Join and fan-out tests pass |
| 4b–4h | One dataset per PR, ported from bbd-analytics with its source file cited: `course_filters_ih`, `course_filters_all`, `course_catalog`, `active_students`, `course_role_activity`, `course_student_activity`, `student_grade` | Parametrised definition tests pass; live smoke run recorded; spec §4.1 updated if a source defect is found |
| 4i | Metrics PR: re-express the six dashboard metrics and the legacy enrollment and grade metrics over these datasets (new `metric.<name>.v1` ids) | Each metric compiles and runs live |

## Phase 5: Chat grounded in the layer (B)

| Unit | Scope | Done when |
|---|---|---|
| 5a | `search_catalog` tool: glossary and synonym ranking | Ranking tests pass |
| 5b | `query_semantic` tool: contract in, at most 200 rows plus provenance out | Tool dispatch test with stubbed Bedrock passes |
| 5c | System prompt built from the catalog, with Bedrock `cachePoint`; dictionary dump removed | Prompt snapshot test; cache hit visible in logs |
| 5d | `describe_cdm_table` plus `execute_sql` requiring `reason`, tagged `governed: false` | Fallback without a reason is rejected |
| 5e | Structured SSE artifacts replace text markers; marker regexes deleted | Artifact contract tests pass |
| 5f | Turn history stores contracts, SQL and result summaries | A follow-up modifies the prior contract in the test |

## Phase 6: Frontend (F), plus 6b (B) before the admin editor

| Unit | Scope | Done when |
|---|---|---|
| 6a | `services/semanticApi.ts` and generated contract types (script in B, output committed in F) | `tsc` passes |
| 6c | Cards as contracts: metric `comparison`, `default_dimensions` and `default_time_dimension` added to the schema (B), cards rendered via `/semantic/query`, View SQL via `/semantic/compile`, storage version bumped | Six built-in cards render live |
| 6d | Card builder: catalog picker plus natural language to contract; ungoverned results can't be saved | Browser check |
| 6e | Query Builder over the catalog; saved queries stored as contracts | Browser check |
| 6f | Import Query maps pasted SQL to a contract and lists what doesn't map | Browser check with one mappable and one unmappable query |
| 6g | Chat artifacts: provenance chips, "Pin as card", Ungoverned badge | Browser check |
| 6h | Developer "Semantic layer" tab (datasets, relationships, metrics) | Browser check |
| 6b (B) | Overlays for measure `expr`, filter `sql` and metric default filters; versioning, history, revert; sandboxed validation. Before overlays ship, also: (1) filter SQL and measure exprs must parse to exactly one statement; (2) the CTE-name check must be scoped per dataset, not global | Overlay tests, including SSTI attempts, multi-statement and cross-scope CTE cases, pass |
| 6i | Admin overlay editor on the 6b API | Browser check |

## Phase 7: Remaining datasets (B), one PR each, in this order

| Group | Datasets (spec §4 #) |
|---|---|
| Internal helpers | 24 `item_tool_map` (from `CDM_META.BBD_CALCULATION_DETAIL`), 25 `calendar_day`, 26 `calendar_hour` |
| Tools and content | 6 `item_tool`, 7 `course_tool_activity`, 8 `course_tool_use`, 9 `course_item_tool_activity`, 13 `student_item_tool_activity`, 14 `student_course_minutes`, 17 `social_interactions_by_type` |
| Assignments and grading | 15 `student_assignments`, 22 `grade_turnaround_by_item_type`, 23 `grade_response_time` |
| Sessions | 19 `lms_sessions_by_slot`, 27 `lms_course_logins`, 28 `lms_sessions` |
| Collaborate | 12 `collab_session_student_activity`, 31 `collab_attendance`, 32 `collab_attendance_hourly`, 33 `collab_rooms`, 34 `collab_sessions_by_slot`, 35 `collab_storage_cumulative` |
| SIS | 18 `sis_enrollment_attributes` |
| Ally and SafeAssign | 29, 30, 36, 37 |
| Risk | 21 `student_risk_success` |
| Activity log | 20 `activity_log` (requires a time range) |

Before the first dataset that reads `CDM_META` or `CDM_TLM` columns missing from the snapshot, add a supplement step to `scripts/build_dictionary_snapshot.py` fed by illuminate-mcp `describe_entity` output. The remaining legacy metrics are re-expressed in the PR that adds the dataset they need.

## Phase 8: Removal (B, F)

| Unit | Scope |
|---|---|
| 8a (F) | Delete inline card SQL, `utils/sqlParams.ts`, `useQueryGeneration` prompts, `executeQuery`/`executeMetric`, the unused dictionary JSON files, and `ParameterizedQuery`'s raw execution |
| 8b (B) | Remove `/dashboard/query`, `/dashboard/metric`, `/admin/metrics`, `verified_queries.json`, legacy `canonical/metrics.yaml`, `semantic_layer/models.py`, `engine.py` and the legacy tool |

## Phase 9: Hygiene (B, F)

| Unit | Scope |
|---|---|
| 9a (B, docs PR) | Rewrite `README.md` and `docs/ARCHITECTURE.md`, `API.md`, `DEVELOPMENT.md`, `DEPLOYMENT.md` |
| 9b (B) | pytest config |
| 9c (F) | Restore linting with an ESLint flat config (`next lint` was removed in Next 16) |
| 9d (F) | Make the `AddCorsOrigin` custom resource update `ALLOWED_ORIGINS`; add `ts-node` to `infra/` devDependencies |
| 9e (F) | Add or remove the `/privacy` route |

## Deferred minors from reviews (fold into the named unit)

| From | Item | Unit |
|---|---|---|
| Phase 1 review | Filter values `Infinity`/`NaN` render as identifiers: use `FiniteFloat` | 4a |
| Phase 1 review | Empty grain suffix (`course_role__`) accepted | 4a |
| Phase 1 review | Grain-suffixed filter error should say suffixes aren't allowed in filters | 4a |
| Phase 1 review | Scoped `SUM` returns NULL rather than 0 when nothing matches: decide and document | 4i |
| Phase 1 review | Empty YAML file raises `TypeError` instead of `CatalogError` | 4b |
| Phase 1 review | Missing `Authorization` header returns 422 rather than 401 | 3b |
| Phase 2 review | ETag weak/list/`*` comparison (RFC 9110); expose `ETag`, allow `If-None-Match` in CORS; `Cache-Control: private, no-cache` | 6a |
| Phase 2 review | 502 detail returns raw Snowflake error text; non-UTF-8 bytes results give 500 | 5b |
| Phase 2 review | Tests for the guard-rejection 502 and for an ETag change when the catalog changes | 5b |
| Phase 3 review | `/api/chat/cancel` cannot reach its stream across Lambda instances: remove it, or back it with DynamoDB | 5f |
| Phase 3 review | A deleted or expired conversation's `context_id` can be claimed by another user: generate `context_id` server-side | 5f |
| Phase 3 review | Same-user concurrent `save_turn` can drop a turn (no version condition) | 5f |
| Phase 3 review | Admin 403 tests should assert the store was never called | 6b |
| Phase 3 review | Frontend `isAdmin` flashes "requires administrator access" before auth loads; recomputed only on mount and login | 6i |
| Phase 3 review | `/docs`, `/redoc` and `/openapi.json` are public; disable them outside dev | 9b |
| Phase 3 review | `_validate_token` accepts both ID and access tokens; check `token_use` | 9b |
| Phase 3 review | Developer page shows an error for non-admins' table preview; hide the button instead | 6h |
