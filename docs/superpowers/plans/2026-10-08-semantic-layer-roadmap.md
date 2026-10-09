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

## Phase 4: Core datasets (B), complete (#30–#39)

Decisions from planning:
- Dashboard metrics are re-expressed as the measure they count; the period-over-period comparison arrives with the metric `comparison` field in 6c.
- Instructor grading engagement waits for datasets 22–23 in Phase 7.
- Rolling-window retention can't be expressed as a single measure and has no bbd-analytics product. Ruled in Phase 7: the retention card is dropped.
- Legacy `course_completion_rate` reads `PERSON_COURSE.STATUS`, which the CDM doesn't have, so it is not re-expressed.
- The dictionary-snapshot supplement now exists (`tests/fixtures/cdm_dictionary_supplement.json`).

| Unit | Scope | Done when |
|---|---|---|
| 4a | Compiler cross-dataset joins through declared entities: shortest path, fan-out rejection, column qualification | Join and fan-out tests pass |
| 4b–4h | One dataset per PR, ported from bbd-analytics with its source file cited: `course_filters_ih`, `course_filters_all`, `course_catalog`, `active_students`, `course_role_activity`, `course_student_activity`, `student_grade` | Parametrised definition tests pass; live smoke run recorded; spec §4.1 updated if a source defect is found |
| 4i | Metrics PR: re-express the six dashboard metrics and the legacy enrollment and grade metrics over these datasets (new `metric.<name>.v1` ids) | Each metric compiles and runs live |

## Phase 5: Chat grounded in the layer (B), detailed plan: `2026-10-08-semantic-layer-phase-5-chat.md`

Units as built: 5a search, 5b query_semantic tool, 5d fallback tools, 5c+5e engine wiring with artifacts (one PR, since removing the markers and adding tool artifacts must land together), 5f history and server-side conversation ids, 5g error hygiene. `/api/chat/cancel` stays per instance; the frontend aborts its own fetch, so a cross-instance cancel returning 404 is harmless.

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
| Phase 4 review | `active_students` and the activity datasets describe the window as ending a week after the end week; it ends with the end week (`END_WEEK + 7` is the next Monday at 00:00); week boundaries use the tenant timezone but access times are compared raw | 7 (activity datasets) |
| Phase 4 review | `course_catalog`/`courses` count deleted courses; say so or filter them in the course metrics | 6c |
| Phase 4 review | `course_filters_ih` description: no-node courses appear as both '-' and 'All Nodes'; multiple SIS sections repeat rows | 7 |
| Phase 4 review | A base dimension silently beats a joined one with the same name; two qualified refs with the same name collide; a grain suffix on `time_range.dimension` is ignored | 6c |
| Phase 4 review | Multi-dataset joins on time dimensions of different types (DATE vs TIMESTAMP_TZ) rely on implicit casts | 6c |
| Phase 4 review | Synonym collisions ("course count" on both `reportable_courses` and `courses`) | 5a follow-up in 6d |
| Phase 5 review | Engine ignores `stopReason`: a `max_tokens` cut-off is returned silently, guardrail stops aren't logged, and text written alongside a tool call is dropped | 9b |
| Phase 5 review | Tool errors don't set `"status": "error"` on `toolResult`; raw warehouse error text reaches the model | 9b |
| Phase 5 review | An odd `CONVERSATION_MAX_MESSAGES` can start history on an assistant turn; trim to an even count | 9b |
| Phase 5 review | Database name is resolved once at import with a silent fallback; a `CatalogError` at import surfaces as an ImportError | 9b |
| Phase 5 review | `describe_table` re-fetches on every failure (`lru_cache` doesn't cache exceptions) | 9b |
| Phase 5 review | `useChat` ignores a different `contextId` from the server | 6g |
| Phase 5 review | Sync DynamoDB calls inside the async chat generator | 9b |
| Phase 5 review | `GET /api/chat/history` returns `queries`, including ungoverned SQL: document it | 9a |
| Phase 5 review | `semantic_layer/tool.py` and `tests/test_semantic_layer.py` are dead | 8 |
| Phase 5 review | Filter-values schema `"items": {}` doesn't tell the model the value types | 6c |
| Phase 5 review | A `cachePoint` after the history messages; verify the system and tools cache points on one live Bedrock call first | first deploy |
| Phase 5 review | Query Builder and card builder run every generated query twice and receive full compiled SQL; `:param` parameter UI is dead | 6d, 6e (replaced) |
| Phase 6 review | Delete has no `expected_version`; deleting a missing overlay returns 200 | 9b |
| Phase 6 review | `provenance.overlays` lists every overlay on any dataset read, used or not; chat artifacts never fill it and the frontend type lacks it | 9b |
| Phase 6 review | Parse errors reach the admin editor with ANSI escape codes | 9b |
| Phase 6 review | A DynamoDB error or malformed row in the overlay list fails every request for that tenant; the 60s cache can show an admin stale state on another instance | 9b |
| Phase 6 review | Chat prompt is built from the canonical catalog, so tenant-only filters and changed metric defaults are invisible to the model | 9b |
| Phase 6 review | `kpiContract` drops every metric after the first without saying so | 9b |
| Phase 6 review | `useDashboardCards` runs each card id once per mount: failed cards never retry, and a changed contract under the same id isn't refetched | 9b |
| Phase 6 review | Mistyped filter values on numeric or time dimensions give an opaque warehouse error rather than a validation message | 9b |
| Phase 6 review | Admin editor swallows overlay-list errors, doesn't reload after a 409, and new tenant filters aren't offered for metric defaults until reload | 9b |
| Phase 6 review | Legacy metric overlay rows still apply on `/dashboard/metric` with no way left to edit them | 8 (removed with the route) |
| Phase 6 review | `_check_tables` matches a quoted table name against a CTE case-insensitively (only canonical `base_sql` can reach it) | 9b |
| Phase 2 review, carried | ETag weak, list and `*` comparison (RFC 9110) | 9b |
| Phase 4 review, carried | Synonym collision ("course count" on `reportable_courses` and `courses`) | 9b |
| Phase 5 review, carried | The card builder's preview re-runs a query the agent already ran | 9b |
| Phase 7 review | `required_time_range` compares only the dimension name, not that it resolved on the base dataset; any start date passes; a metric can't be built on such a dataset because overlay validation and the metric test compile without a time range | 9b |
| Phase 7 review | `pii_exempt` is keyed on the output column, not the source lineage it was reviewed for | 9b |
| Phase 7 review | `activity_log` submission rows put `item type: name` in `tool` (carried from the source) | 9b |
| Phase 7 review | `students_at_risk` thresholds are new and undocumented as such; rows include ungraded enrollments | 9b |
| Phase 7 review | Two "grading turnaround" measures (response days vs hours to grade) share the synonym; per-grade rounding up adds about half a day | 9b |
| Phase 7 review | `courses_graded_this_week` keeps the "instructor engagement" synonym, which overstates it | 9b |
| Phase 7 review | `collab_attendance_hourly.attendee_hours` counts a person twice in an hour when two sessions carry different IH lists | 9b |
| Phase 7 review | `lms_sessions` IH nodes include child courses; the slot source excludes them | 9b |
| Phase 7 review | `safeassign_originality_reports_basic` is always empty (the event type does not occur) | 9b |
| Phase 7 review | `item_tool.FIRST_ACCESSED_DATE` (session timezone date) is compared with tenant-timezone course weeks, about a day off at each edge | 9b |
| Phase 7 review | `TABLE(GENERATOR(ROWCOUNT => n))` has no size cap (canonical YAML only) | 9b |
| Phase 7 review | Preview and support users stay in `social_interactions_by_type` and non-student `course_item_tool_activity` denominators (as in the source) | 9b |
| Phase 7 review | Enrollment metrics over `course_role_activity` include deleted courses (about 2%), like the other course_filters-based metrics | 9b |

## Phase 9 close-out

9a to 9e are complete: backend #98, frontend #16 to #19.

Rewriting the docs turned up these findings. All are fixed except where marked deferred.

| Finding | Outcome |
|---|---|
| `sqlglot>=26.0.0` would install 30.x on deploy. From 27.x, `TABLE(fn(...))` parses as `TableFromRows`, so `_check_tables` stops seeing it (fails open). From 28.x, `with` is renamed `with_` and CTE scope breaks (186 tests fail). | Pinned `sqlglot==26.0.0`, with a test that the installed version matches (#94) |
| Freehand `TABLE(RESULT_SCAN(...))` and `TABLE(INFORMATION_SCHEMA.QUERY_HISTORY())` passed the schema whitelist on every version | The guard rejects every table function except a bounded `GENERATOR` (#94) |
| Chat replies were scrubbed for the client but stored unscrubbed | Scrubbed before storage (#95) |
| The Snowflake password was in the CloudFormation template, and the secret was overwritten on every base deploy | Secret created with a placeholder; `scripts/set-snowflake-secret.sh` writes it (#96) |
| The frontend `AddCorsOrigin` resource never re-ran after an API deploy reset `ALLOWED_ORIGINS` | Carries a per-synth timestamp (frontend #19) |
| Stale `/health` version, unused `ACCOUNT_ID`, `ARTIFACTS_BUCKET`, S3 grant and `frontendOrigin`, AgentCore leftovers, stale comments | Removed or updated (#97) |
| The 1000-row `truncated` flag is dropped by the chat tools | No change: the tools' own 200-row model limit already reports truncation whenever the 1000 cap is hit |
| Both DynamoDB tables use `RemovalPolicy.DESTROY` | No change, documented: deliberate for a POC teardown |
| The initial Cognito user's password is in the template (`AwsCustomResource` parameters) | Deferred: replace it with a post-deploy script, as for the Snowflake secret |
| `DATA_DICTIONARY_URL` affects only `describe_cdm_table`; the `/api/v1/dictionary/*` routes use a fixed URL | Deferred |
| The dev default origin `https://dm5zbussw00dg.cloudfront.net` is hard-coded and may no longer be live | Deferred: drop it once the frontend stack has been redeployed with #19 |
| `truststore` is not wired in, contrary to the local-machine convention | Deferred: the API makes outbound HTTPS calls only from Lambda; add it if local runs against live services fail on corporate CAs |
| `sqlglot` upgrade path (keyword renames, `TableFromRows`, `Generator` node) | Deferred: port the guards, then move the pin |
