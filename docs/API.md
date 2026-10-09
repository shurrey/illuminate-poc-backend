# API Reference

All routes are defined in `lambda_handler.py`. The app runs under uvicorn behind a Lambda Function
URL in `RESPONSE_STREAM` mode, so `/api/chat/stream` delivers Server-Sent Events as they are produced.

## Conventions

### Authentication

Every route except `GET /health` needs a Cognito **ID token**:

```
Authorization: Bearer <id_token>
```

The token must be RS256-signed by the configured user pool (`USER_POOL_ID`), have
`token_use == "id"`, and have `aud` equal to `USER_POOL_CLIENT_ID`. Access tokens are rejected. A
missing, malformed or rejected token returns `401 {"detail": "Invalid or expired token"}`.

Claims the API reads:

| Claim | Use |
|-------|-----|
| `sub` | Owner of conversations and in-progress chat requests |
| `custom:tenant_id` | Selects the tenant's overlays. Without it, the caller gets the canonical catalog. |
| `cognito:groups` | Must contain `illuminate-admins` for admin routes and table preview |

### Admin routes

`/api/v1/admin/*` and `/api/v1/dictionary/preview` return `403` when the caller is not in
`illuminate-admins`. Admin routes also return `403` when the token has no `custom:tenant_id`. An
admin can read and change only their own tenant's overlays.

### Errors

Errors use FastAPI's shape, `{"detail": ...}`. `detail` is usually a string; some routes return an
object, described below. A request body or query parameter that fails validation returns `422`
with FastAPI's validation error list.

### CORS

Allowed origins come from `ALLOWED_ORIGINS` (comma-separated). Allowed methods are GET, POST, PUT,
DELETE and OPTIONS; allowed request headers are `Content-Type`, `Authorization` and `If-None-Match`;
`ETag` is exposed.

### Interactive docs

`/docs`, `/redoc` and `/openapi.json` exist only when `API_DOCS` is `on` (the default when unset).
Deployed Lambdas set `API_DOCS=off`, so these return `404` there.

## Route summary

| Method | Path | Auth |
|--------|------|------|
| GET | `/health` | None |
| POST | `/api/chat` | ID token |
| POST | `/api/chat/stream` | ID token |
| POST | `/api/chat/cancel/{request_id}` | ID token |
| GET | `/api/conversations/{context_id}` | ID token |
| DELETE | `/api/conversations/{context_id}` | ID token |
| GET | `/api/v1/semantic/catalog` | ID token |
| POST | `/api/v1/semantic/compile` | ID token |
| POST | `/api/v1/semantic/query` | ID token |
| GET | `/api/v1/dictionary/submodels` | ID token |
| GET | `/api/v1/dictionary/definitions` | ID token |
| GET | `/api/v1/dictionary/erd` | ID token |
| GET | `/api/v1/dictionary/preview` | ID token, admin |
| GET | `/api/v1/admin/overlays` | ID token, admin with tenant |
| GET | `/api/v1/admin/overlay/{target}` | ID token, admin with tenant |
| PUT | `/api/v1/admin/overlay/{target}` | ID token, admin with tenant |
| DELETE | `/api/v1/admin/overlay/{target}` | ID token, admin with tenant |
| GET | `/api/v1/admin/overlay/{target}/history` | ID token, admin with tenant |
| POST | `/api/v1/admin/overlay/{target}/revert` | ID token, admin with tenant |

---

## Health

### GET /health

```json
{"status": "healthy", "version": "0.4.0", "mode": "chat_engine"}
```

---

## Chat

### Request body (both chat routes)

The simple form:

```json
{"message": "How many courses are running right now?", "context_id": "<id>", "request_id": "<id>"}
```

| Field | Required | Description |
|-------|----------|-------------|
| `message` | Yes | The question |
| `context_id` | No | Conversation to continue. Omit to start one. |
| `request_id` | No | Client-chosen id; lets the stream be cancelled. Streaming only. |

The A2A JSON-RPC form is also accepted: the message is the first `"type": "text"` part of
`params.message.parts`, the context is `params.message.contextId`, and the request id is the
top-level `id` or `params.message.messageId`.

A `context_id` the caller owns, or one that does not exist yet, is used as given. A `context_id`
that belongs to another user is replaced with a new id; the response carries the id actually used.

The model sees the conversation's earlier turns. The queries behind each earlier answer
(`queries`, see [GET /api/conversations](#get-apiconversationscontext_id)) are replayed to the model
in a `<previous_queries>` block at the start of the following user message, so follow-ups can modify them.

### POST /api/chat

Runs the tool loop to completion.

**200**

```json
{"text": "...", "artifacts": [ ... ], "context_id": "<id>", "sources": null}
```

`text` has passed through the PII scrub (see [Architecture](ARCHITECTURE.md#pii-model)); the stored conversation holds the same scrubbed text.
`sources` is always `null`.

| Status | When |
|--------|------|
| 400 | No message (`"No message provided"`) |
| 401 | Token missing or rejected |
| 502 | The chat engine raised; `detail` is the error message |

### POST /api/chat/stream

Same body. Responds with `text/event-stream`; each event is `data: <json>\n\n`.

| Event | Shape |
|-------|-------|
| status | `{"type": "status", "message": "Processing your question..."}`, then one per tool call, e.g. `"Running a governed query..."` |
| complete | `{"type": "complete", "data": {"text": "...", "artifacts": [...], "contextId": "<id>"}}` |
| error | `{"type": "error", "message": "..."}`, including `"Empty response"` when the model returned no text |
| cancelled | `{"type": "cancelled", "message": "Request cancelled by user"}` |

`400` (no message) and `401` are returned before the stream starts. Failures after that arrive as
`error` events. The turn is saved to the conversation when the model returns non-empty text.

### Artifacts

Tool calls produce artifacts, returned alongside the text. Every artifact has `id`, `type`, `title`
and `data`.

From `query_semantic` (governed):

| `type` | `data` | Extra fields |
|--------|--------|--------------|
| `table` | `{"columns": [...], "rows": [{...}]}`, all rows | `query`, `sql`, `provenance` |
| `sql` | The compiled SQL string | `query`, `sql`, `provenance` |
| `chart` (when asked for) | `{"chart_type": "bar"\|"line"\|"pie"\|"scatter", "title", "x_axis", "y_axis", "data": rows}` | `query`, `sql`, `provenance` |

`query` is the query contract (re-runnable against `/api/v1/semantic/query`); `provenance` is
`{"governed": true, "datasets", "metrics", "measures", "overlays"}`.

From `execute_sql` (ungoverned): a `table` and a `sql` artifact with `sql` and
`provenance: {"governed": false, "reason": "<why no governed definition fits>"}`.

Values are JSON-safe: Snowflake decimals become numbers, binary values become hex strings, and NaN
or infinite floats become `null`.

### POST /api/chat/cancel/{request_id}

Cancels a stream started with that `request_id` by the same user. The stream ends with a
`cancelled` event at its next event.

**200** `{"success": true, "request_id": "<id>"}`

| Status | When |
|--------|------|
| 401 | Token missing or rejected |
| 404 | No such request in progress for this user. Requests are tracked per Lambda instance, so a cancel that reaches a different instance also returns 404. |

---

## Conversations

Conversations are stored in DynamoDB, keyed by `context_id`, owned by the creating user's `sub`,
and expire 30 days after their last update by default.

### GET /api/conversations/{context_id}

**200**

```json
{
  "messages": [
    {"role": "user", "content": "How many courses are running?"},
    {"role": "assistant", "content": "...", "queries": [
      {"title": "Ongoing courses", "governed": true, "query": {"metrics": ["metric.ongoing_courses.v1"]}},
      {"title": "Ungoverned query result", "governed": false, "sql": "SELECT ..."}
    ]}
  ]
}
```

Messages are oldest first, trimmed to whole turns (at most `CONVERSATION_MAX_MESSAGES`, default 50).
`queries` appears on assistant messages whose answer ran a query: governed entries carry the query
contract in `query`, ungoverned ones carry the freehand SQL in `sql`.

| Status | When |
|--------|------|
| 401 | Token missing or rejected |
| 404 | The conversation does not exist or belongs to someone else |

### DELETE /api/conversations/{context_id}

**200** `{"success": true}`. Returns `404` when the conversation does not exist or belongs to someone else.

---

## Semantic layer

The catalog, compile and query routes use the canonical catalog with the caller's tenant overlays
applied (see [Architecture](ARCHITECTURE.md#tenant-overlays)).

### Query contract

The body of `compile` and `query`:

```json
{
  "metrics": ["metric.ongoing_courses.v1"],
  "measures": ["dataset.course_filters.v1:ended_courses"],
  "dimensions": ["start_date__month", "dataset.courses.v1:design_mode"],
  "filters": [{"dimension": "course_role", "op": "eq", "values": ["S"]}],
  "time_range": {"dimension": "start_date", "start": "2026-01-01", "end": "2026-06-30"},
  "order_by": [{"field": "ongoing_courses", "direction": "desc"}],
  "limit": 100
}
```

| Field | Description |
|-------|-------------|
| `metrics` | Metric ids. At least one metric or measure is required. |
| `measures` | `<dataset id>:<measure name>` |
| `dimensions` | A dimension name, `<name>__<grain>` for a time grain, or `<dataset id>:<name>` to pick a joined dataset's dimension |
| `filters` | `op` is one of `eq`, `neq`, `in`, `not_in`, `gt`, `gte`, `lt`, `lte`, `between`, `is_null`, `not_null`, `contains`. Values are strings, numbers or booleans; `between` takes two, `in`/`not_in` one or more, `is_null`/`not_null` none. Filters take plain dimension names, no grain suffix. |
| `time_range` | A time dimension with `start`, `end` or both (ISO dates, inclusive) |
| `order_by` | `field` must be one of the output columns; `direction` defaults to `desc` |
| `limit` | 1 to 1000, default 100 |

Unknown fields are rejected. Output columns are named after each dimension reference (without a
dataset qualifier) and each metric's short name (`metric.ongoing_courses.v1` -> `ongoing_courses`)
or measure name. Datasets with `required_time_range` reject queries that lack a `time_range` with a
`start` on that dimension.

### GET /api/v1/semantic/catalog

The public catalog as the caller's tenant sees it. Base SQL and column names are not included.

```json
{
  "datasets": [{
    "id": "dataset.course_filters.v1", "display_name": "...", "description": "...",
    "grain": "...", "domain": "course",
    "dimensions": [{"name": "...", "type": "categorical|time|boolean|numeric",
                    "description": "", "grains": [], "synonyms": [], "selectable": true}],
    "measures": [{"name": "courses", "agg": "count_distinct", "numerator": null,
                  "denominator": null, "unit": "courses", "description": "", "synonyms": []}],
    "filters": [{"name": "ongoing", "description": "..."}],
    "joins": ["dataset.courses.v1"],
    "required_time_range": null
  }],
  "metrics": [{"id": "metric.ongoing_courses.v1", "display_name": "...", "description": "...",
               "owner": "...", "authority": "...", "last_reviewed": "2026-10-08",
               "measure": "dataset.course_filters.v1:courses", "default_filters": ["ongoing"],
               "synonyms": [], "example_questions": []}]
}
```

`selectable: false` marks a PII dimension: it can be filtered on but not selected. `joins` lists
datasets whose dimensions queries on this dataset can use.

The response carries an `ETag` and `Cache-Control: private, no-cache`. Sending a matching
`If-None-Match` (or `*`) returns `304` with no body.

| Status | When |
|--------|------|
| 304 | `If-None-Match` matches |
| 401 | Token missing or rejected |

### POST /api/v1/semantic/compile

Compiles a contract to SQL without running it.

**200**

```json
{
  "sql": "WITH DS_COURSE_FILTERS_V1 AS (...), DS_COURSES_V1 AS (...)\nSELECT ...",
  "provenance": {
    "governed": true,
    "datasets": ["dataset.course_filters.v1", "dataset.courses.v1"],
    "metrics": ["metric.ongoing_courses.v1"],
    "measures": ["dataset.course_filters.v1:courses", "dataset.course_filters.v1:ended_courses"],
    "overlays": ["filter:dataset.course_filters.v1:ongoing@v2"]
  }
}
```

`provenance.overlays` lists the tenant overlays, as `<target>@v<version>`, that shaped this query's
metrics, measures and the metrics' default filters.

| Status | When |
|--------|------|
| 400 | The contract does not compile (unknown metric, PII dimension selected, missing required time range, ...); `detail` is the reason |
| 401 | Token missing or rejected |
| 422 | The body is not a valid contract |

### POST /api/v1/semantic/query

Compiles the contract, then runs it through the execution guard against Snowflake.

**200**

```json
{"columns": ["start_date__month", "design_mode", "ongoing_courses", "ended_courses"],
 "rows": [{"start_date__month": "2026-01-01", "design_mode": "...", "ongoing_courses": 412, "ended_courses": 37}],
 "sql": "WITH ...", "provenance": { ... }}
```

Column names are lower case, matching the contract's output names.

| Status | When |
|--------|------|
| 400 | The contract does not compile |
| 401 | Token missing or rejected |
| 422 | The body is not a valid contract |
| 502 | The guard rejected the SQL or Snowflake failed. `detail` is `{"error": "...", "sql": "..."}`; for Snowflake failures `error` is `"The warehouse could not run this query."` |

---

## Data dictionary

### GET /api/v1/dictionary/submodels, /definitions, /erd

Authenticated pass-through of `https://us.data.api.blackboard.com/api/v1/data/dictionary/{submodels|definitions|erd}`.
The upstream JSON is returned unchanged and cached in memory per Lambda instance for one hour.

| Status | When |
|--------|------|
| 401 | Token missing or rejected |
| 502 | `"Data dictionary service unavailable"` |

### GET /api/v1/dictionary/preview

Sample rows from a CDM table. Administrators only, because raw rows include person data.

| Query parameter | Description |
|-----------------|-------------|
| `schema` | Must start with `CDM_`; letters, digits and underscores only |
| `table` | Letters, digits and underscores only |
| `limit` | Default 20, clamped to 1..100 |

**200** `{"columns": ["ID", ...], "rows": [{"ID": 1, ...}], "truncated": false}`

| Status | When |
|--------|------|
| 400 | Invalid identifier, or schema not `CDM_*` |
| 401 | Token missing or rejected |
| 403 | Not in `illuminate-admins` |
| 502 | `"Failed to query sample data"` |

---

## Admin: tenant overlays

An overlay replaces one field of a canonical definition for the caller's tenant. `{target}` is one of:

| Target | Field set in the body |
|--------|-----------------------|
| `measure:<dataset id>:<measure name>` | `expr` (a non-ratio measure's expression) |
| `filter:<dataset id>:<filter name>` | `sql` (a new name adds a tenant-only filter) |
| `metric:<metric id>` | `default_filters` (filter names on the metric's dataset) |

Every save creates the next version and keeps all earlier versions as history. Writes use
optimistic concurrency: send the version you loaded as `expected_version` (`0` when there is no
overlay yet).

Common errors on every admin route: `401` (token), `403` (not an admin, or no tenant on the token),
`400` (malformed target).

### GET /api/v1/admin/overlays

```json
{"tenant_id": "t1", "overlays": [{
  "target": "measure:dataset.student_grade.v1:average_grade_percentage",
  "expr": "ROUND(GRADE_PERCENTAGE, 0)", "sql": null, "default_filters": null,
  "description": "rounded", "version": 2, "updated_by": "<sub>", "updated_at": "2026-10-08T12:00:00+00:00",
  "status": "active", "problems": []
}]}
```

`status` is `skipped` when the overlay no longer validates against the current canonical
definitions; `problems` says why. Skipped overlays are not applied to queries or chat.

### GET /api/v1/admin/overlay/{target}

```json
{"tenant_id": "t1", "target": "<target>", "overlay": { ... } , "canonical": {"expr": "GRADE_PERCENTAGE"}}
```

`overlay` is `null` when the tenant has none. `canonical` is the canonical value of the field the
overlay replaces (`{"expr"}`, `{"sql"}` or `{"default_filters"}`), or `null` when the target has no
canonical definition (for example a tenant-only filter).

### PUT /api/v1/admin/overlay/{target}

```json
{"expr": "ROUND(GRADE_PERCENTAGE, 0)", "description": "rounded", "expected_version": 1}
```

Set exactly the field for the target's kind. The overlay is validated before it is saved:
expressions must be a single SQL expression over the dataset's columns, with no templates, queries,
tables, aggregates, window functions, star or positional references, or unrecognised functions;
the changed dataset must still pass the definition validator (including the PII checks); and every
metric on that dataset must still compile.

**200** `{"tenant_id": "t1", "target": "<target>", "overlay": { ...saved overlay with its new version... }}`

| Status | When |
|--------|------|
| 400 | Validation failed; `detail` is `{"errors": ["..."]}` |
| 409 | `expected_version` is not the current version: `"This overlay changed since you loaded it; reload and try again."` |

### DELETE /api/v1/admin/overlay/{target}?expected_version=N

Removes the current overlay; its history is kept. `expected_version` is required.

**200** `{"tenant_id": "t1", "target": "<target>", "overlay": null}`

| Status | When |
|--------|------|
| 400 | Another overlay depends on it (a metric overlay uses this tenant-only filter); `detail` is `{"errors": [...]}` |
| 404 | The tenant has no overlay on this target |
| 409 | The overlay changed since it was loaded |
| 422 | `expected_version` missing |

### GET /api/v1/admin/overlay/{target}/history

```json
{"tenant_id": "t1", "target": "<target>", "history": [{ ...version 3... }, { ...version 2... }]}
```

Every saved version, newest first, including versions saved before a delete.

### POST /api/v1/admin/overlay/{target}/revert

```json
{"version": 1, "expected_version": 3}
```

Validates version 1's content and saves it as the next version (4 here).

**200** Same shape as PUT.

| Status | When |
|--------|------|
| 400 | The old version no longer validates; `detail` is `{"errors": [...]}` |
| 404 | No such version |
| 409 | `expected_version` is not the current version |

---

## Removed routes

`POST /api/v1/dashboard/query`, `POST /api/v1/dashboard/metric` and the `/api/v1/admin/metrics`
routes were removed with the legacy metric engine. Use `/api/v1/semantic/query` and
`/api/v1/admin/overlay/{target}` instead.
