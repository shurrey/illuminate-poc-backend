# Development Guide

## Setup

Prerequisites: Python 3.11 (the Lambda runtime), plus Node.js and Docker if you will deploy (see
[DEPLOYMENT.md](DEPLOYMENT.md)).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-lambda.txt -r requirements-dev.txt
```

`requirements-lambda.txt` is what the Lambda bundle installs. It omits `boto3`, which the Lambda
runtime provides; locally `moto` (in `requirements-dev.txt`) pulls it in.

`sqlglot` is pinned to an exact version. The compiler and the execution guard walk its parse tree,
whose shape changes between releases, so upgrade it only together with a full test run;
`tests/test_dependency_pins.py` fails if the installed version differs from the pin.

## Tests

```bash
python -m pytest                                   # everything; pytest.ini already adds -q, so no extra -q
python -m pytest tests/test_semantic_definitions.py  # validate and compile every canonical definition
python -m pytest -k overlay                        # a subset by name
```

Tests do not call Bedrock, Snowflake or AWS. DynamoDB is provided by `moto` (`mock_aws`); Bedrock is
replaced by scripted `converse` stubs; Snowflake execution and token validation are monkeypatched;
the data dictionary is read from the snapshot in `semantic_layer/data/`. Tests that import
`chat_engine` set `SNOWFLAKE_DATABASE` so it does not look the database up in Secrets Manager.

`cdk/test/check_template.py` asserts properties of the synthesized CloudFormation templates; see its
docstring for the `cdk synth` command to run first.

## Running locally

The API is a plain FastAPI app:

```bash
python lambda_handler.py          # uvicorn on 0.0.0.0:$PORT (default 8080)
```

`/docs` is available locally because `API_DOCS` defaults to `on`. The Python app does not read
`.env`; set its variables in the shell. To use real AWS resources from a deployed `dev`
environment, export AWS credentials and:

| Variable | Value |
|----------|-------|
| `USER_POOL_ID`, `USER_POOL_CLIENT_ID` | From SSM `/illuminate/dev/cognito-pool-id` and `/illuminate/dev/cognito-client-id`; needed to validate tokens |
| `OVERLAY_TABLE` | `illuminate-overlays-dev`; unset, overlay reads fail and queries fall back to the canonical catalog |
| `CONVERSATION_TABLE` | Defaults to `illuminate-conversations-dev` |
| `SNOWFLAKE_SECRET_NAME` | Defaults to `illuminate/dev/snowflake` |
| `SNOWFLAKE_DATABASE` | Optional; otherwise the `database` field of the Snowflake secret |
| `ALLOWED_ORIGINS` | Defaults to `http://localhost:3000,http://localhost:5173` |

See [DEPLOYMENT.md](DEPLOYMENT.md#environment-variables) for the full list. Every route except
`/health` needs a Cognito ID token from that user pool.

To compile a contract without any AWS access, use the semantic layer directly:

```python
from semantic_layer.catalog import default_catalog
from semantic_layer.compiler import compile_query
from semantic_layer.contract import QueryContract

print(compile_query(QueryContract(metrics=["metric.ongoing_courses.v1"], dimensions=["term_name"]),
                    default_catalog(), "MY_DB").sql)
```

## Code layout

| Path | Contents |
|------|----------|
| `lambda_handler.py` | Routes, auth, tenant catalog cache, overlay admin endpoints |
| `chat_engine.py` | Bedrock Converse loop |
| `snowflake_client.py` | Connection, `validate_and_execute` (guard), `query_sql`, `query_preview` |
| `overlay_store.py`, `conversation_store.py` | DynamoDB access |
| `semantic_layer/schema.py` | Pydantic models for datasets, metrics and the catalog |
| `semantic_layer/contract.py` | `QueryContract`, `CompiledQuery`, `Provenance` |
| `semantic_layer/catalog.py` | Loads `canonical/` (`default_catalog()` is cached per process) |
| `semantic_layer/render.py` | Restricted `base_sql` template rendering |
| `semantic_layer/compiler.py` | Contract -> SQL |
| `semantic_layer/validate.py` | Offline validation against the CDM dictionary snapshot |
| `semantic_layer/overlays.py` | Overlay model, expression checks, apply and validate |
| `semantic_layer/catalog_view.py` | The public catalog view (no SQL, no column names) |
| `semantic_layer/search.py` | `search_catalog` ranking |
| `semantic_layer/chat_tools.py` | Tool specs and handlers for the chat model |
| `semantic_layer/prompt.py` | Chat system prompt |
| `semantic_layer/pii.py` | Well-known PII column names and counting-aggregate check |
| `semantic_layer/dictionary.py` | Live data dictionary lookups for `describe_cdm_table` |
| `semantic_layer/data/` | CDM dictionary snapshot, supplement and PII column list |
| `scripts/build_dictionary_snapshot.py` | Rebuilds `cdm_dictionary.json` and `cdm_pii_columns.json` |
| `scripts/set-snowflake-secret.sh` | Writes the `SNOWFLAKE_*` values from `.env` to the Snowflake secret |

## Adding a dataset

1. Create `canonical/datasets/<domain>/<name>.yaml`. Every `*.yaml` under `canonical/datasets/` is
   loaded. A minimal shape:

   ```yaml
   id: dataset.my_dataset.v1
   display_name: My dataset
   description: What one row is and what it covers.
   grain: one row per course
   domain: course
   source: where the logic comes from
   base_sql: |
     SELECT c.ID AS COURSE_ID, c.NAME AS COURSE_NAME
     FROM {{ database }}.CDM_LMS.COURSE c
   entities:
     - {name: course, column: COURSE_ID, type: foreign}
   dimensions:
     - {name: course_name, column: COURSE_NAME, type: categorical}
   measures:
     - {name: courses, agg: count_distinct, expr: COURSE_ID, unit: courses}
   ```

   Rules the loader and validator enforce:
   - `base_sql` may use only `{{ database }}` and `{{ ref('<dataset id>') }}`; each `ref()` target
     must be in `depends_on`. Tables must be `{{ database }}.CDM_*.<table>`.
   - Dimension, measure and filter names are lowercase `snake_case` and unique within the dataset.
   - Time dimensions list their `grains`; ratio measures name a `numerator` and `denominator` from
     the same dataset.
   - Every output traced to a PII-flagged source column must be in `pii_columns`, or in
     `pii_exempt` with a reason. Measures over PII columns must be `count` or `count_distinct`.
   - Set `complete: true` only if every instance of the primary entity has a row; only complete
     datasets lend their dimensions to other datasets.
   - Set `visibility: internal` for helper datasets that should only be `ref()`'d.
   - Set `required_time_range: <time dimension>` when queries must be bounded by date.
2. If `base_sql` reads a table or column missing from the snapshot, regenerate the snapshot
   (`python scripts/build_dictionary_snapshot.py <catalog.json> <definitions.json>`, see its
   docstring) or, for tables the dictionary export omits, add them to
   `semantic_layer/data/cdm_dictionary_supplement.json`.
3. Run `python -m pytest tests/test_semantic_definitions.py`. It validates the dataset, compiles
   every measure and dimension, and checks the SQL reads only CDM tables and its own CTEs.

## Adding a metric

Append to the `metrics:` list in a file in `canonical/metrics/` (one file per domain):

```yaml
metrics:
  - id: metric.my_metric.v1
    display_name: My metric
    description: What it counts, in one sentence.
    owner: Blackboard
    authority: vendor-canonical
    last_reviewed: 2026-10-08
    measure: dataset.my_dataset.v1:courses
    default_filters: [some_filter]      # filter names on that dataset
    synonyms: [phrases users say]
    example_questions: ["How many ...?"]
```

The measure's dataset must be public. The metric's output column is its short name (`my_metric`).
`synonyms` and `example_questions` feed `search_catalog`, and the metric is listed in the chat
system prompt. Run `python -m pytest tests/test_semantic_definitions.py`; it also checks that no
synonym names two different concepts.

## Adding an overlay

Overlays are per-tenant data, not files. Create one with the admin API as a member of
`illuminate-admins` whose ID token carries `custom:tenant_id`:

```bash
curl -X PUT "$API_URL/api/v1/admin/overlay/measure:dataset.student_grade.v1:average_grade_percentage" \
  -H "Authorization: Bearer $ID_TOKEN" -H "Content-Type: application/json" \
  -d '{"expr": "ROUND(GRADE_PERCENTAGE, 0)", "description": "Whole-number grades", "expected_version": 0}'
```

See [API.md](API.md#admin-tenant-overlays) for targets, versioning, history and revert. In tests,
`tests/test_overlay_routes.py` shows the pattern: a moto DynamoDB table patched into
`overlay_store._table`, and `_get_user_from_token` patched to return an admin user.

## Changing the chat tools

Tool specs and handlers are in `semantic_layer/chat_tools.py` (`SPECS` and `ChatTools._tool_<name>`);
status messages shown while a tool runs are `_STATUS` in `chat_engine.py`; the rules the model
follows are `_RULES` in `semantic_layer/prompt.py`. `tests/test_chat_tools.py` and
`tests/test_chat_engine.py` cover them with stubbed execution and a scripted Bedrock client.
