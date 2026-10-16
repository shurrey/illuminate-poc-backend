# Multi-tenancy, Phase 1: Tenant isolation. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every API request runs against the caller's tenant, which is resolved from the verified token alone:
- its registry row;
- its Snowflake connection and database;
- its overlays, caches and conversations.

The current deployment becomes the first Premium tenant.

**Architecture:**
- **Registry:** a new DynamoDB tenant registry, read through `tenancy.py`, gives a `Tenant`.
- **Explicit tenant:** the `Tenant` is passed as a required keyword argument to every Snowflake call, not held in
  ambient state. A missing tenant is a `TypeError`, never a silent default.
- **Connections:** a connection, its credentials and its database are cached per `tenant_id`.
- **Conversations:** the owner key becomes `{tenant_id}#{sub}`.
- **Migration:** a `migrate-current` script moves the existing users, overlays, conversations and secret to a new
  UUID tenant.

**Tech Stack:** Python 3.11, FastAPI, boto3, snowflake-connector, pytest; CDK (TypeScript).

**Spec:** `docs/superpowers/specs/2026-10-16-multi-tenant-and-mcp-design.md`, sections 1, 3 (`migrate-current`, `list`)
and 9 (the routes that exist after this phase).

**Later phases** each get their own plan, written when the phase starts, in this order:
2. tenant script and licenses;
3. profiles;
4. partly governed results;
5. SQL saved queries;
6. sharing;
7. MCP.

## Global Constraints

- **Tenant source:** the tenant comes only from the verified token's `custom:tenant_id`. No parameter, header, path or
  body field selects it.
- **No usable tenant:**
  - a missing claim, an unknown tenant or a missing secret is a 403 with the detail
    `Your organization is not set up yet`;
  - there is no fallback;
  - the global secret `illuminate/dev/snowflake` is no longer read.
- **Names:**
  - the tenant secret is `illuminate/tenants/{tenant_id}/snowflake`;
  - the registry table is `illuminate-tenants-{env}`, keyed by `tenant_id` (string).
- **Registry row:** `tenant_id`, `name`, `license` (`core`|`premium`), `mcp_enabled` (bool, default false),
  `created_at`, `updated_at` (ISO-8601 UTC).
- **The first tenant:** "Northbridge University", license `premium`, a fresh `uuid4`.
- **Tests:** they never call Bedrock or AWS. Use fakes and monkeypatching, as the existing tests do.
- **Python HTTPS:** the scripts call AWS over HTTPS, so `scripts/tenant.py` calls `truststore.inject_into_ssl()`
  before importing boto3.
- **Process:**
  - one PR per task, from `main`;
  - squash-merge when tests pass;
  - commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`;
  - PR bodies start with `**Claude:**` and end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- **Running tests:** `$S/venv311/bin/python -m pytest -q`, where `S` is the session scratchpad. Recreate the venv with
  `uv venv --python 3.11 $S/venv311 && VIRTUAL_ENV=$S/venv311 uv pip install -r requirements-lambda.txt -r
  requirements-dev.txt "truststore>=0.10"` if it is missing.

## Review Focus

1. **A warm Lambda instance:** it serves tenant A and then tenant B. B must never get A's connection, database,
   overlay catalog, terms or chat tools. Tested in Task 6 by alternating calls in one process.
2. **A conversation id from tenant A, sent by a user of tenant B** (even one with the same `sub` string):
   - it is treated as unused, and B's turn gets a new id;
   - A's history is never returned or overwritten.

   Tested in Task 5.
3. **A token whose `custom:tenant_id` names no registry row:** a 403 on every tenant route, including chat, before
   any Bedrock or Snowflake call. Tested in Task 6.
4. **`migrate-current` run twice, or after a partial failure:**
   - the second run creates no second tenant;
   - it moves only what is left;
   - the counts it prints add up.

   Tested in Task 7.
5. **A redeploy of the base stack after migration** must not re-stamp the old `blackboard-dev` id on the seed user.
   Covered by Task 7's CDK change and checked in Task 8.

---

### Task 1: Tenant registry and `tenant_for`

**Files:**
- Create: `tenancy.py`
- Create: `cdk/lib/api/tenant-table.ts`
- Modify: `cdk/lib/api/index.ts`, `cdk/lib/api/lambda-proxy.ts` (env `TENANT_TABLE`; IAM `dynamodb:GetItem` on the
  table; `secretsmanager:GetSecretValue` on
  `arn:aws:secretsmanager:${region}:${account}:secret:illuminate/tenants/*`)
- Test: `tests/test_tenancy.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Tenant: tenant_id: str; name: str; license: str; mcp_enabled: bool`;
  - `class TenantNotReady(Exception)`;
  - `NOT_READY = "Your organization is not set up yet"`;
  - `get_tenant(tenant_id: str) -> Tenant`. It raises `TenantNotReady` on a missing row, and caches rows per
    instance for 60 s.
  - `tenant_for(user: Optional[dict]) -> Tenant`, in `lambda_handler.py`. It raises
    `HTTPException(403, NOT_READY)`. It uses `_tenant_id_from_user`.
  - `tenancy._table` is a module attribute tests can monkeypatch, matching `overlay_store._table`.

- [ ] **Step 1: Write the failing tests** in `tests/test_tenancy.py`, using a fake table with `get_item`.
  - `test_get_tenant_returns_row`: the fields map, and `mcp_enabled` defaults to False when absent.
  - `test_get_tenant_missing_raises`.
  - `test_get_tenant_caches_for_60_seconds`: patch `time.monotonic`. A second call within 60 s doesn't hit the
    table, and one at 61 s does.
  - `test_tenant_for_without_claim_is_403`: the detail equals `NOT_READY`.
  - `test_tenant_for_unknown_tenant_is_403`.
- [ ] **Step 2: Run** `pytest tests/test_tenancy.py -q`. Expected: it fails with `ModuleNotFoundError: tenancy`.
- [ ] **Step 3: Implement** `tenancy.py` and `tenant_for`, and add the CDK table: PAY_PER_REQUEST, DESTROY, a
  docstring listing the fields, as in `overlay-table.ts`.
- [ ] **Step 4: Run** `pytest -q` and `cd cdk && npx tsc --noEmit`. Expected: all pass, with no type errors.
- [ ] **Step 5: Commit** `feat: tenant registry and tenant_for`, then open the PR and merge it.

### Task 2: Per-tenant Snowflake connections

**Files:**
- Modify: `snowflake_client.py`
- Test: `tests/test_snowflake_tenant.py`; update existing tests that call `query_sql` / `validate_and_execute` /
  `query_preview`.

**Interfaces:**
- Consumes: `tenancy.Tenant`, `tenancy.TenantNotReady`.
- Produces:
  - `validate_and_execute(sql, params=None, *, tenant: Tenant, compiled=False, allow_identity=False,
    extra_pii=frozenset())`;
  - `query_sql(sql, params=None, *, tenant: Tenant)`;
  - `query_preview(schema, table, limit=20, *, tenant: Tenant)`;
  - `tenant_database(tenant: Tenant) -> str`, the secret's `database`. It raises `TenantNotReady` when the secret or
    its database is missing;
  - `invalidate(tenant_id: str)`.
- Every existing monkeypatched fake must accept `tenant=` (`lambda sql, params=None, **k: ...`).

- [ ] **Step 1: Write the failing tests** in `tests/test_snowflake_tenant.py`:
  - **`test_credentials_read_from_tenant_secret`:** a fake Secrets Manager records `SecretId`; it equals
    `illuminate/tenants/T1/snowflake`.
  - **`test_connections_are_cached_per_tenant`:** a fake `snowflake.connector.connect` returns a distinct object per
    call. Calls for T1, T2, T1 open 2 connections, and the T1 calls share one.
  - **`test_missing_secret_raises_tenant_not_ready`:** a `ResourceNotFoundException` becomes `TenantNotReady`, and no
    other secret is tried.
  - **`test_tenant_database_from_secret`.**
  - **`test_session_expiry_reconnects_same_tenant_only`:** reconnecting T1 leaves T2's cached connection open.
  - **`test_validate_and_execute_requires_tenant`:** calling without `tenant=` raises `TypeError`.
- [ ] **Step 2: Run** `pytest tests/test_snowflake_tenant.py -q`. Expected: it fails, because `tenant` is an
  unexpected keyword.
- [ ] **Step 3: Implement** the change.
  - Replace the module globals with `_connections: dict[str, conn]` and `_credentials: dict[str, dict]`.
  - `_run` and `_drop_connection` take the `tenant_id`.
  - Drop `SNOWFLAKE_SECRET_NAME`.
  - Keep the guard logic unchanged.
- [ ] **Step 4: Run** `pytest -q`. Expected: all pass, including the updated fakes in existing tests.
- [ ] **Step 5: Commit** `feat: Snowflake connections are per tenant`, then open the PR and merge it.

### Task 3: Tenant through the semantic, report and preview routes

**Files:**
- Modify: `lambda_handler.py`:
  - `_semantic_database` becomes `_database_for(tenant)`;
  - `_catalog_for(user)` becomes `_catalog_for(tenant)`;
  - `_compile_for`, `_execute`, `_current_terms`;
  - the routes `/api/v1/semantic/*`, `/api/v1/reports/*`, `/api/v1/dictionary/preview`, `/api/v1/admin/*`.
- Test: update `tests/test_semantic_*_endpoint.py`, `test_report_endpoints.py`, `test_raw_data_routes.py`,
  `test_overlay_routes.py`, `test_admin_authz.py`; new cases in `tests/test_tenant_routes.py`.

**Interfaces:**
- Consumes: `tenant_for`, `snowflake_client.tenant_database`, the `tenant=` keyword.
- Produces:
  - `_catalog_for(tenant: Tenant) -> tuple[Catalog, list[Overlay]]`;
  - `_database_for(tenant: Tenant) -> str`, which wraps `tenant_database`;
  - `_current_terms(tenant: Tenant) -> list[dict]`, cached in `_terms_cache[tenant.tenant_id]`;
  - `_require_admin_tenant(user) -> Tenant`.
- A shared test helper, `tests/tenant_fakes.py`, holds:
  - `TENANT_A` and `TENANT_B` (Premium, different names);
  - `install_tenants(monkeypatch, *tenants)`, which patches `tenancy.get_tenant`;
  - `user_of(tenant, *groups, sub="u1")`.

  `TenantNotReady` raised inside a route becomes the 403 through an exception handler registered on `app`.

- [ ] **Step 1: Write the failing tests** in `tests/test_tenant_routes.py`:
  - **`test_semantic_query_uses_callers_tenant`:** a fake `validate_and_execute` records `tenant`; a user of B gets
    `tenant.tenant_id == B`.
  - **`test_compiled_sql_uses_tenant_database`:** two tenants whose databases differ (`DB_A`, `DB_B`, through a
    patched `tenant_database`); B's compiled SQL contains `DB_B.` and never `DB_A.`.
  - **`test_terms_cache_is_per_tenant`:** A's terms rows are never returned to B.
  - **`test_preview_without_tenant_is_403`.**
  - **`test_report_run_unknown_tenant_is_403_before_snowflake`:** the fake execute is never called.
  - **`test_admin_overlay_routes_use_registry_tenant`.**
  - **`test_tenant_not_ready_inside_route_is_403`:** `tenant_database` raises, and the response is 403 with
    `NOT_READY`.
- [ ] **Step 2: Run** `pytest tests/test_tenant_routes.py -q`. Expected: it fails, because the fake receives no
  `tenant`.
- [ ] **Step 3: Implement** the change.
  - Each listed route calls `tenant_for(user)` straight after authentication.
  - The `SNOWFLAKE_DATABASE` env override stays for tests only (`conftest.py`). Rename it `TEST_SNOWFLAKE_DATABASE`
    so no deployed Lambda can set it. My call.
- [ ] **Step 4: Run** `pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: semantic, report, preview and admin routes run in the caller's tenant`, then open the
  PR and merge it.

### Task 4: Chat runs in the caller's tenant

**Files:**
- Modify: `chat_engine.py`:
  - remove `_resolve_database`, `_database`, `SYSTEM_PROMPT` and `_tools` at import;
  - `send_message` and `send_message_streaming` require `tools: ChatTools, system_prompt: str`.
- Modify: `semantic_layer/chat_tools.py`. `ChatTools.__init__(catalog, database, *, tenant: Tenant, execute=...,
  describe=..., overlays=())`; the default execute passes `tenant=self.tenant`.
- Modify: `lambda_handler.py`. `_engine_kwargs(tenant)` always returns tools and the prompt for the tenant's catalog
  and database. The chat routes call `tenant_for` before streaming starts.
- Test: update `tests/test_chat_engine.py`, `test_chat_tools.py`, `test_chat_routes.py`,
  `test_database_resolution.py` (delete the import-time cases, which are now gone); new cases in
  `tests/test_tenant_routes.py`.

**Interfaces:**
- Consumes: `_catalog_for(tenant)`, `_database_for(tenant)`.
- Produces: `_engine_kwargs(tenant: Tenant) -> dict` with keys `tools`, `system_prompt`.

- [ ] **Step 1: Write the failing tests:**
  - **`test_chat_tools_execute_with_their_tenant`:** a `ChatTools` for B runs `execute_sql`, and the fake
    `validate_and_execute` sees B.
  - **`test_chat_stream_unknown_tenant_403_before_bedrock`:** the fake `_bedrock` is never called.
  - **`test_engine_kwargs_prompt_names_tenant_database`:** B's prompt contains `DB_B`.
  - **`test_chat_engine_import_reads_no_secret`:** importing `chat_engine` with a Secrets Manager fake that raises
    succeeds.
- [ ] **Step 2: Run** `pytest tests/test_chat_tools.py tests/test_tenant_routes.py -q`. Expected: it fails on the
  missing `tenant` keyword.
- [ ] **Step 3: Implement** the change.
- [ ] **Step 4: Run** `pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: chat runs in the caller's tenant`, then open the PR and merge it.

### Task 5: Conversations keyed by tenant and user

**Files:**
- Modify: `lambda_handler.py`:
  - a new `_owner(user, tenant) -> str` returns `f"{tenant.tenant_id}#{user['sub']}"`;
  - every conversation call and `_request_owners` use it (lines ~435, 477, 484, 535, 552, 567, 584).
- Test: `tests/test_conversation_ownership.py`, `test_cancel_authz.py`, `test_conversation_list.py`; new cases in
  `tests/test_tenant_routes.py`.

**Interfaces:**
- Produces: `_owner(user: dict, tenant: Tenant) -> str`. Task 8 writes the same format when migrating.
- Ruling: the design's "conversations gain `tenant_id`" is met by a composite owner key, instead of a new attribute
  and index.
  - The existing ownership checks then isolate tenants with no new code paths.
  - Cost if wrong: a later per-tenant conversation listing needs a GSI.

- [ ] **Step 1: Write the failing tests:**
  - **`test_same_sub_other_tenant_cannot_read_conversation`:** users `{sub: "u1"}` of A and of B. B's
    `GET /api/conversations/{A's id}` is 404.
  - **`test_same_sub_other_tenant_gets_new_context_id`:** B posting with A's `context_id` gets a different id, and
    A's item is unchanged.
  - **`test_conversation_list_is_per_tenant`.**
  - **`test_cancel_from_other_tenant_is_404`.**
- [ ] **Step 2: Run** `pytest tests/test_tenant_routes.py -q`. Expected: it fails, because B reads A's history.
- [ ] **Step 3: Implement** the change.
- [ ] **Step 4: Run** `pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: conversations are owned per tenant`, then open the PR and merge it.

### Task 6: Cross-tenant isolation suite

**Files:**
- Create: `tests/test_tenant_isolation.py`, using `tests/tenant_fakes.py`.

This module is the standing guard named in spec section 9. Later phases add their routes here. It drives the app
through `TestClient` with fakes for the registry, the overlay table (the in-memory fake from
`test_overlay_store.py`), the conversation table, Secrets Manager and `snowflake.connector`. Tenant A's fake rows and
secret values contain the marker `ALPHA-ONLY`.

- [ ] **Step 1: Write the tests.** Each one asserts `ALPHA-ONLY` appears nowhere in B's response body.
  - `test_overlays_never_cross` (list, get, history, revert, delete of A's target by B's admin).
  - `test_semantic_catalog_never_crosses`: A has an overlay, and B's `/api/v1/semantic/catalog` shows the canonical
    definition.
  - `test_semantic_query_never_crosses`.
  - `test_reports_never_cross`.
  - `test_preview_never_crosses`.
  - `test_conversations_never_cross`.
  - `test_alternating_requests_one_process`:
    - interleave A, B, A, B calls to query, report run, chat (scripted Bedrock) and catalog;
    - each call's fake connection is its own tenant's;
    - the caches hold one entry per tenant.
  - `test_forged_tenant_fields_ignored`: send `tenant_id`, `X-Tenant-Id` and `?tenant_id=` set to A on B's requests.
    Every result is B's.
  - `test_missing_secret_fails_closed`: tenant C has a registry row but no secret, and gets a 403 on every route
    above.
  - `test_unknown_tenant_403_everywhere`: a parametrized list of every tenant route.
- [ ] **Step 2: Run** `pytest tests/test_tenant_isolation.py -q`. Expected: all pass, since Tasks 1–5 built the
  behaviour.
  - Break-check: temporarily make `_catalog_for` ignore its tenant and confirm `test_semantic_catalog_never_crosses`
    fails.
  - Revert the break.
- [ ] **Step 3: Commit** `test: cross-tenant isolation suite`, then open the PR and merge it.

### Task 7: Tenant script, `migrate-current` and `list`

**Files:**
- Create: `scripts/tenant.py` (CLI; argparse subcommands `migrate-current`, `list`).
- Create: `tenant_admin.py`. Its logic takes boto clients as parameters, so tests pass fakes.
- Modify: `cdk/lib/base/auth.ts`, `cdk/bin/illuminate.ts`:
  - `initialTenantId` comes from context or env `INITIAL_TENANT_ID`, with no default;
  - the seed-user custom resources are created only when it is set.
- Test: `tests/test_tenant_admin.py`.

**Interfaces:**
- Produces: `migrate_current(*, dynamodb, secrets, cognito, user_pool_id, env, old_tenant_id="blackboard-dev",
  name="Northbridge University", license="premium", dry_run=False) -> dict`. It returns counts:
  `{tenant_id, created, secret_copied, users, overlays, conversations}`.

**Steps, each idempotent:**
1. **Find or create the tenant.** Find a registry row whose `migrated_from == old_tenant_id`, or create one with
   `uuid4` and `mcp_enabled=False`.
2. **Copy the secret.** Copy `illuminate/dev/snowflake` to `illuminate/tenants/{id}/snowflake` if that secret is
   missing.
3. **Move the users.** Every Cognito user whose `custom:tenant_id == old_tenant_id` gets the new id.
4. **Move the overlays.** Every overlay item (including `#v` history) under `old_tenant_id` is copied to the new
   partition, then deleted from the old one.
5. **Re-key the conversations.** Every conversation whose `owner_sub` has no `#` is rewritten to `"{id}#{sub}"`, using
   the user's tenant. A sub that isn't in Cognito is left as is and counted as `orphaned`.

`list(*, dynamodb, env) -> list[dict]` returns the registry rows, without secrets.

- [ ] **Step 1: Write the failing tests** in `tests/test_tenant_admin.py`, with in-memory fakes:
  - `test_migrate_creates_one_premium_tenant`: its name is Northbridge University and `mcp_enabled` is False.
  - `test_migrate_copies_secret_once`.
  - `test_migrate_moves_users_overlays_history_and_conversations`.
  - `test_migrate_twice_is_a_no_op_second_time`: the same `tenant_id`, with all counts 0.
  - `test_migrate_resumes_after_partial_failure`: the overlay fake raises halfway; a rerun finishes, and there are no
    duplicates.
  - `test_dry_run_writes_nothing`.
  - `test_cli_never_prints_secret_values`: run `main(["migrate-current", "--dry-run"])` with fakes, then search
    `capsys` for the fake password.
- [ ] **Step 2: Run** `pytest tests/test_tenant_admin.py -q`. Expected: it fails with
  `ModuleNotFoundError: tenant_admin`.
- [ ] **Step 3: Implement** it. `scripts/tenant.py` does `truststore.inject_into_ssl()`, builds the boto clients from
  `AWS_PROFILE`, and reads the pool id from the base stack's SSM output, as `scripts/set-snowflake-secret.sh` does.
- [ ] **Step 4: Run** `pytest -q` and `cd cdk && npx tsc --noEmit`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: tenant script with migrate-current and list`, then open the PR and merge it.

### Task 8: Deploy and migrate the dev account

There are no code changes, so no PR.

- [ ] **Step 1: Dry run first.** Run `scripts/tenant.py migrate-current --dry-run` with `AWS_PROFILE=App_Dashbaoard_Developer`.
  Expected: 1 tenant to create, the user count matching `aws cognito-idp list-users`, and the overlay and
  conversation counts printed.
- [ ] **Step 2: Deploy the API stack** (`cdk deploy IlluminateApi-dev`, using the export-credentials workaround in
  memory).
  - Expected: the table and IAM policy are created, and the Lambda is updated.
  - Tenant routes now return 403 until Step 3; that brief outage is acceptable in dev.
- [ ] **Step 3: Migrate.** Run `scripts/tenant.py migrate-current`, then `scripts/tenant.py list`.
  Expected: one row, "Northbridge University", premium, MCP off.
- [ ] **Step 4: Set the seed tenant.** Set `initialTenantId` to the new id in `cdk.json` context, commit
  `chore: seed user belongs to the Northbridge tenant`, open the PR and merge it. Deploy the base stack.
  Expected: the seed user's `custom:tenant_id` is unchanged (the new id).
- [ ] **Step 5: Live checks**, signed in after re-login:
  - the catalog loads, with the overlays present;
  - a report runs;
  - Ask Illuminate answers a governed question;
  - an earlier conversation is listed;
  - a token with `custom:tenant_id=blackboard-dev` (an old ID token, before refresh) gets a 403 with `NOT_READY`.
- [ ] **Step 6: Retire the old secret.** Delete `illuminate/dev/snowflake` from the base stack's storage construct.
  Commit `chore: retire the global Snowflake secret`, open the PR and merge it. Deploy the base stack.
  Expected: queries still run.
