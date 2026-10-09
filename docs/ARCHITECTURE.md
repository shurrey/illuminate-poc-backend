# Architecture

## Components

```
                 Browser / API client
                         |  HTTPS, Authorization: Bearer <Cognito ID token>
                         v
        Lambda Function URL (auth NONE, RESPONSE_STREAM)
                         |
        Lambda Web Adapter -> run.sh -> uvicorn -> FastAPI (lambda_handler.py)
                         |
          +--------------+---------------+---------------------+
          |              |               |                     |
   semantic routes   chat routes     admin routes        dictionary routes
          |              |               |                     |
          |        chat_engine.py        |              Blackboard data
          |        (Bedrock Converse)    |              dictionary API
          |              |               |
          |     semantic_layer/chat_tools.py
          |              |               |
          +------> semantic_layer/ <-----+---- overlay_store.py --> DynamoDB
                   (catalog, overlays,        conversation_store.py --> DynamoDB
                    compiler)
                         |
               snowflake_client.validate_and_execute  (execution guard)
                         |
                     Snowflake (credentials from Secrets Manager)
```

| Module | Role |
|--------|------|
| `lambda_handler.py` | FastAPI app: routes, token validation, tenant catalog cache, overlay admin, response PII scrub |
| `semantic_layer/` | Definitions schema, catalog loader, compiler, offline validator, overlays, chat tools, system prompt |
| `canonical/` | The governed definitions: `datasets/<domain>/*.yaml` and `metrics/*.yaml` |
| `chat_engine.py` | Bedrock Converse tool loop over `ChatTools` |
| `snowflake_client.py` | Snowflake connection (lazy, reused) and the execution guard |
| `overlay_store.py` | Versioned tenant overlays in DynamoDB |
| `conversation_store.py` | Per-user conversation history in DynamoDB |

The Lambda runs one uvicorn process; Lambda Web Adapter (`AWS_LAMBDA_EXEC_WRAPPER=/opt/bootstrap`)
forwards invocations to it and streams responses back, which is what makes SSE on
`/api/chat/stream` arrive incrementally.

## Request flow

1. **Auth.** `_get_user_from_token` reads the bearer token, fetches the user pool's JWKS (cached
   for an hour), and verifies an RS256 signature and issuer. Only ID tokens are accepted
   (`token_use == "id"`, `aud == USER_POOL_CLIENT_ID`), because only ID tokens carry
   `custom:tenant_id` and `cognito:groups`.
2. **Tenant catalog.** `_catalog_for(user)` starts from the canonical catalog (loaded once per
   process from `canonical/`) and applies the tenant's overlays from DynamoDB. The result is cached
   per Lambda instance for 60 seconds; admin writes on that instance clear it. If the overlay store
   is unreachable the canonical catalog is used and nothing is cached. Users without a tenant get
   the canonical catalog.
3. **Compile or chat.**
   - Semantic routes compile the caller's `QueryContract` with `compile_query`.
   - Chat routes run the Bedrock tool loop; the model's `query_semantic` calls compile the same way.
4. **Guard.** All SQL that reaches Snowflake, compiled or freehand, goes through
   `validate_and_execute`.
5. **Snowflake.** `query_sql` runs the statement and fetches at most 1000 rows, reporting
   `truncated` when more were available.
6. **Response.** Values are made JSON-safe (decimals to numbers, bytes to hex, NaN to null). Chat
   text passes through `_scrub_pii` before it is stored or returned.

## Semantic layer

### Definitions

A **dataset** is a named SQL relation over `CDM_*` tables at a declared grain. Fields
(`semantic_layer/schema.py`):

| Field | Meaning |
|-------|---------|
| `id` | `dataset.<name>.v<n>` |
| `display_name`, `description`, `grain`, `domain`, `source` | Documentation; `domain` groups the catalog |
| `visibility` | `public` (default) or `internal`. Internal datasets can be referenced by other datasets but not queried. |
| `complete` | True when every instance of its primary entity has a row. Only complete datasets lend dimensions to other datasets, so joins cannot multiply or drop rows. |
| `base_sql` | The relation. A template allowing only `{{ database }}` and `{{ ref('<dataset id>') }}`; rendered in Jinja's sandbox after an allow-list check of the template AST. |
| `depends_on` | Every dataset `ref()`'d by `base_sql` must be listed here |
| `entities` | Keys (`primary` or `foreign`) used to find joinable datasets |
| `dimensions` | `categorical`, `time` (with allowed `grains`), `boolean` or `numeric` columns |
| `measures` | `sum`, `count`, `count_distinct`, `avg`, `min`, `max`, `median` over an `expr`, or `ratio` of two other measures |
| `filters` | Named SQL predicates, used by metrics' `default_filters` |
| `pii_columns` | Output columns that are personally identifiable |
| `pii_exempt` | Output column -> reason, for outputs traced to a PII-flagged source column that a reviewer judged not personal |
| `required_time_range` | A time dimension every query must bound with a `time_range` start |

A **metric** (`canonical/metrics/*.yaml`) names one dataset measure (`<dataset id>:<measure>`) with
optional `default_filters`, plus ownership (`owner`, `authority`, `last_reviewed`), `synonyms` and
`example_questions`.

`canonical/` currently holds 38 datasets (35 public, 3 internal reference datasets) in 12 domain
folders, and 19 metrics in 4 files.

### Compiler

`compile_query(contract, catalog, database)` in `semantic_layer/compiler.py`:

1. Resolves each metric and measure to its dataset; internal datasets are refused.
2. Builds one aggregate `SELECT` per base dataset. Dimensions may come from the base dataset or from
   complete datasets reachable many-to-one through a shared entity (`LEFT JOIN`). Metric default
   filters are applied inside the aggregate (`COUNT(DISTINCT CASE WHEN <filter> THEN ... END)`);
   contract filters and the time range go in `WHERE`. Time grains are `CAST(DATE_TRUNC(...) AS DATE)`.
3. When measures come from several datasets, the per-dataset results are full-outer-joined on the
   shared dimensions with null-safe equality.
4. Renders every dataset's `base_sql` and dependencies as CTEs (`DS_<NAME>_V<N>`), dependencies first.
5. Checks that every real table in the result is `<database>.CDM_*.<table>`, apart from CTEs and
   bounded `TABLE(GENERATOR(ROWCOUNT => n))` calls.

The outer query is built as a sqlglot AST, so contract values become typed literals and are never
spliced into SQL text. PII dimensions cannot be selected (only filtered on), and output names must
not collide. The result is `{sql, provenance}`.

### Offline validation

`semantic_layer/validate.py` checks definitions against a CDM dictionary snapshot shipped in
`semantic_layer/data/` (`cdm_dictionary.json`, `cdm_dictionary_supplement.json` for tables the
export omits, and `cdm_pii_columns.json`). For each dataset it qualifies the SQL with sqlglot and
checks that:

- every source column exists, and every entity, dimension, measure and filter uses columns the dataset outputs;
- dimension types match the inferred column types;
- every output whose lineage reaches a dictionary-flagged PII column (other than through a count)
  is in `pii_columns` or `pii_exempt`, and well-known PII names cannot be exempted;
- measures over PII columns are `count` or `count_distinct`.

Metrics must point at an existing measure, existing filters, and a public dataset. The test suite
runs these checks over every canonical definition, and the admin routes run them on every overlay.

## Tenant overlays

An overlay (`semantic_layer/overlays.py`) replaces one field for one tenant:

| Target | Replaces |
|--------|----------|
| `measure:<dataset id>:<name>` | the measure's `expr` (not ratios) |
| `filter:<dataset id>:<name>` | the filter's `sql`; a new name adds a tenant-only filter |
| `metric:<metric id>` | the metric's `default_filters` |

Overlays cannot change base SQL, entities, grain or visibility. An expression must be one plain SQL
expression over the dataset's columns: no templates, statements, subqueries, tables, aggregates,
windows, star or parameter references, or functions outside an allow list. Before saving, the
candidate is applied over the tenant's other overlays, the affected dataset or metric is
re-validated with the offline validator, and every metric on that dataset must still compile.

**Storage** (`overlay_store.py`): table `illuminate-overlays-<env>`, partition key `tenant_id`, sort
key `metric_id`. The current overlay is stored under its target; each version is also stored under
`<target>#v<version>` (zero-padded to six digits, e.g. `#v000003`). A save reads the history for the next version number and writes both rows in
one `TransactWriteItems`, conditional on the caller's `expected_version` (a mismatch is a 409).
Delete removes only the current row, conditional on its version. Revert copies an old version's
content forward as a new version.

**Application.** At request time the stored overlays are validated and applied one at a time, in
target order, starting from the canonical catalog. An overlay that no longer validates (for example after a canonical
change) is skipped and logged; the admin list reports it as `skipped` with its problems. Compiled
queries list the overlays that shaped them in `provenance.overlays`. A tenant with overlays also
gets a chat system prompt and tools built from its overlaid catalog.

## Chat

`chat_engine.py` calls Bedrock `converse` (model `BEDROCK_MODEL_ID`, temperature 0, 4096 max
tokens) for up to 6 rounds. The system prompt (`semantic_layer/prompt.py`) lists the public metrics
and datasets, with dimensions, measures, joins, required time ranges and PII "filter only"
markings, followed by usage, privacy and style rules. Cache points after the system prompt and the
tool list let Bedrock reuse them.

Tools (`semantic_layer/chat_tools.py`), all bound to the caller's catalog:

| Tool | Does |
|------|------|
| `search_catalog` | Token-overlap ranking of metrics, measures and non-PII dimensions against the question |
| `query_semantic` | Compiles a contract, runs it through the guard as compiled SQL, returns up to 200 rows to the model and all rows as `table`, `sql` and optional `chart` artifacts |
| `describe_cdm_table` | Column names, types, PII flags and descriptions for a raw CDM table, from the data dictionary API |
| `execute_sql` | Freehand read-only SQL through the guard in strict mode. Refused unless `search_catalog` or `query_semantic` already ran in the same turn, and unless `reason` is at least a sentence (15 characters). Results are labelled ungoverned with the reason. |

Warehouse error messages returned to the model have object and role names removed. If the model
stops for `max_tokens`, the answer is marked as cut off.

## Conversation store

`conversation_store.py` keeps one DynamoDB item per conversation in `illuminate-conversations-<env>`:
`context_id` (key), `owner_sub`, `messages` (JSON), `updated_at` and `ttl` (`CONVERSATION_TTL`,
default 30 days). Writes are conditional on `owner_sub`, so one user cannot write over another's
conversation; a requested `context_id` owned by someone else is replaced with a new id. History is
capped at `CONVERSATION_MAX_MESSAGES` (default 50) and loaded as whole turns, because Converse rejects
a history that starts with an assistant message.

Each assistant message carries `queries`: the title and query contract (governed) or SQL
(ungoverned) of what it ran. On the next turn these are replayed to the model in a
`<previous_queries>` block at the start of the user's message, so follow-ups can modify the earlier
query rather than reconstruct it from prose.

## PII model

PII is handled in layers:

1. **Definitions.** The offline validator traces every dataset output back to source columns and
   requires any output reaching a dictionary-flagged PII column to be declared in `pii_columns` (or
   exempted with a reason). Measures may only count PII columns.
2. **Compile.** A dimension whose column is PII (declared, or a well-known name such as
   `FIRST_NAME`, `EMAIL`, `SSN`) cannot be selected; it can only be filtered on. The public catalog
   marks such dimensions `selectable: false`, and `search_catalog` omits them.
3. **Execution guard** (`validate_and_execute`). Every statement must be a single `SELECT`, `WITH`,
   `UNION`, `SHOW` or `DESCRIBE`, with no DML or DDL anywhere in the tree, reading only `CDM_*` or
   `INFORMATION_SCHEMA`, with no table function other than `TABLE(GENERATOR(ROWCOUNT => n))` for a
   literal n up to 1,000,000, and with no `LIMIT` above 1000. Well-known PII column names may appear in a
   projection only inside `COUNT`, `COUNT_IF`, `APPROX_COUNT_DISTINCT` or HLL, not windowed.
   - Compiled SQL (`compiled=True`) is checked on the outermost projection only, since its
     definitions were PII-checked when they were written.
   - Freehand SQL (strict mode) is checked on every projection, and star projections are refused
     except inside `COUNT(*)`.
4. **Row cap.** At most 1000 rows are fetched per query; results report `truncated`.
5. **Response scrub.** Chat text is passed through regexes that redact SSNs, email addresses, phone
   numbers and card numbers before it is stored in the conversation or returned to the client.
6. **Access.** Raw table preview is limited to `illuminate-admins`. Users cannot change their own
   `custom:tenant_id`: the Cognito app client's writable attributes exclude it.

## Infrastructure

Two CDK stacks (`cdk/`), detailed in [DEPLOYMENT.md](DEPLOYMENT.md):

- `IlluminateBase-<env>`: Cognito user pool, app client and `illuminate-admins` group, an S3
  artifacts bucket (unused by the API), the Snowflake secret (its value is set by
  `scripts/set-snowflake-secret.sh`), and SSM discovery parameters.
- `IlluminateApi-<env>`: the Lambda function and Function URL, the conversation and overlay tables,
  and the `api-url` SSM parameter.
