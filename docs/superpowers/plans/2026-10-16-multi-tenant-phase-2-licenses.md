# Multi-tenancy, Phase 2: Tenant script and licenses. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**
- **Licenses enforced on the server.** Core tenants reach CDM only; Premium tenants also reach LEARN.
- **Tenant script.** It creates and maintains tenants, users and connections.
- **The web app** shows LEARN to everyone in the data dictionary, but disables Data Preview on LEARN for Core.
- **A Core tenant** is created live.

**Architecture:**
- **Allowed schemas** come from `tenant.license` (`core` → `CDM_*`; `premium` → `CDM_*` + `LEARN`; anything else →
  refused). The SQL guard, preview, chat tools and prompt all use one function.
- **LEARN metadata and PII** come from the public data dictionary.
  - The snapshot is built into `semantic_layer/data/learn_*.json`.
  - PII is matched per LEARN table.
  - A LEARN column the dictionary doesn't list counts as PII.
- **A new `GET /api/v1/me`** tells the web app the tenant name, license and roles.

**Tech Stack:** Python 3.11 / FastAPI / sqlglot / boto3 / pytest + moto; Next.js / TypeScript / Vitest (`.test.ts`
only); CDK.

**Spec:** `docs/superpowers/specs/2026-10-16-multi-tenant-and-mcp-design.md`, sections 2 (licenses) and 3 (tenant
script).

## Global Constraints

- **Core LEARN refusal:** Core tenants are refused LEARN with exactly `This query uses LEARN data, which is available
  with Illuminate Premium.` (the guard, saved-query import, the describe tool), and data preview with exactly `Data
  preview for LEARN tables is available with Illuminate Premium` (tooltip and API).
- **The data dictionary shows LEARN to Core users** in full. Only Data Preview is disabled.
- **Licenses:** they are `core` or `premium`. A registry row with any other license (or none) is treated as not set up
  (403 `Your organization is not set up yet`).
- **The tenant comes only from the verified token.** No cross-tenant visibility, ever. A user is never moved between
  tenants.
- **Script output:** the tenant script never prints a secret or a password.
- **Tests** never call Bedrock or AWS (moto and fakes). New guard and license routes get cases in
  `tests/test_tenant_isolation.py`.
- **Commits** end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. PR bodies start with `**Claude:**`
  and end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`. Never commit
  `illuminate-poc/tsconfig.tsbuildinfo`.
- **Code comments:** only non-obvious behaviour; ≤5-line module docstrings, ≤4-line function docstrings; no
  narrative.
- **Backend tests:** `$S/venv311/bin/python -m pytest -q` (S = session scratchpad). **Frontend tests:** `npm test`
  and `npx tsc --noEmit` in illuminate-poc.

## Review Focus

1. **A Premium Viewer's raw SQL on a LEARN person table** (through chat) never returns a name, email or id unless
   inside a counting aggregate. This holds including for columns the dictionary doesn't list. Tested in Task 2.
2. **A Core tenant** reaches LEARN through no route: chat SQL, the describe tool, preview, an unqualified table name
   under a `LEARN` schema in a CTE or subquery, quoted `"LEARN"`, or lower-case `learn`. Tested in Tasks 1, 3, 6.
3. **Changing a tenant's license or connection** takes effect without a redeploy: within the 60 s registry cache and
   the 300 s credential TTL. Tested in Task 5.
4. **`add-users` with an email that already exists in another tenant** (or in the ai-first-lms app with no tenant) is
   refused and named. Nothing about that user changes. Tested in Task 5.
5. **The seed user is never re-tenanted** by a CDK deploy, even if the configured seed tenant changes. Tested in
   Task 6.

---

### Task 1: License-aware SQL guard

**Files:** `tenancy.py`, `snowflake_client.py`, a new `licenses.py`; tests `tests/test_licenses.py`,
`tests/test_execution_guard.py`, `tests/test_tenant_isolation.py`.

**Interfaces:**
- Produces, in `licenses.py`:
  - `LICENSES = frozenset({"core", "premium"})`;
  - `LEARN_REFUSAL` (the exact Core text above);
  - `PREVIEW_REFUSAL` (the exact preview text above);
  - `allows_learn(tenant) -> bool`;
  - `schema_allowed(tenant, schema: str) -> bool`. It is case-insensitive: `CDM_*` and `INFORMATION_SCHEMA` always,
    `LEARN` only for premium.
- `tenancy.get_tenant` raises `TenantNotReady` when `license not in LICENSES`.

- [ ] **Step 1: Failing tests.**
  - `test_unknown_or_missing_license_is_not_ready`.
  - `test_core_learn_refused_with_premium_message`. Cover `LEARN.USERS`, `"LEARN"."USERS"`, `learn.users`, inside a
    CTE, a join, an `IN` subquery and a UNION branch. Each returns `{"error": LEARN_REFUSAL}` and makes no connection.
  - `test_premium_learn_allowed`.
  - `test_show_and_describe_are_refused`: the SHOW/DESCRIBE branch is deleted, so they fail the statement-type check.
  - `test_function_in_other_database_refused`: `SELECT OTHER_DB.CDM_LMS.F(1)` and
    `LATERAL FLATTEN(input => OTHER_DB.S.F())`.
  - `test_information_schema_databases_refused`: `INFORMATION_SCHEMA.DATABASES`, qualified or not.
  - An isolation-suite case: Core tenant C2 (same connection fake as B) is refused LEARN through `/api/chat` raw SQL,
    and no connection is made.
- [ ] **Step 2: Run them.** Expected: they fail.
- [ ] **Step 3: Implement.**
  - The guard's schema whitelist calls `schema_allowed`. A disallowed `LEARN` schema returns `LEARN_REFUSAL`; other
    schemas keep the existing message.
  - Remove the SHOW/DESCRIBE branch.
  - Check `exp.Anonymous` / `exp.Func` nodes whose name has a catalog part against `tenant_database`.
  - Refuse an `INFORMATION_SCHEMA` table named `DATABASES`.
- [ ] **Step 4: Run** `pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: the SQL guard enforces the tenant's license`, then open the PR and merge it.

### Task 2: LEARN dictionary snapshot and LEARN PII in the guard

**Files:** `scripts/build_dictionary_snapshot.py`, new `semantic_layer/data/learn_dictionary.json` and
`semantic_layer/data/learn_pii_columns.json`, new `semantic_layer/learn.py`, `snowflake_client.py`; tests
`tests/test_learn_pii.py`.

**Interfaces:**
- **Snapshot:** `build_dictionary_snapshot.py` gains `--learn <definitions.json>`. It writes:
  - `learn_dictionary.json`: `{TABLE: {COLUMN: "TYPE"}}`, taken from `LEARN.T.C` definitions, using
    `technicalSpecifications[0].dataType` when present, else `""`;
  - `learn_pii_columns.json`: a sorted list of `"TABLE.COLUMN"` where `isPii`.
- **`semantic_layer/learn.py`:**
  - `learn_columns(table) -> set[str] | None`;
  - `learn_pii(table, column) -> bool`. True when the column is flagged, and also when the table or the column is
    absent from the snapshot.
- **The guard:** for freehand SQL that references any `LEARN` table, a column counts as PII when `learn_pii(t, col)`
  is true for any referenced LEARN table `t` whose column set contains it, or when it matches no referenced table's
  columns (fail closed). It then gets the existing counting-aggregate rule. `SELECT *` over a LEARN table is refused,
  as for CDM.

- [ ] **Step 1: Generate the snapshot.** Fetch `https://us.data.api.blackboard.com/api/v1/data/dictionary/definitions`
  (truststore), run the builder, and commit the two files. Expected: about 192 tables and 228 PII entries; record the
  real counts.
- [ ] **Step 2: Failing tests.**
  - `test_learn_flagged_column_is_pii`. Use a real flagged column, e.g. `ACTIVITY_ACCUMULATOR.USER_PK1`.
  - `test_learn_unlisted_column_is_pii`.
  - `test_learn_unflagged_column_allowed`.
  - `test_guard_refuses_learn_pii_outside_count`: `SELECT U.EMAIL FROM LEARN.USERS U`.
  - `test_guard_allows_count_of_learn_pii`.
  - `test_guard_learn_alias_and_cte`: the PII column is reached through an alias and a CTE.
- [ ] **Step 3: Implement**, then **Step 4: Run** `pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: LEARN columns flagged or unknown in the dictionary are PII`, then open the PR and
  merge it.

### Task 3: Chat tools, preview and describe follow the license

**Files:** `semantic_layer/chat_tools.py`, `semantic_layer/prompt.py`, `semantic_layer/dictionary.py`,
`lambda_handler.py`; tests `tests/test_chat_tools.py`, `tests/test_semantic_prompt.py`,
`tests/test_raw_data_routes.py`, `tests/test_tenant_isolation.py`.

**Interfaces:**
- `build_system_prompt(catalog, database, *, learn: bool)`. Rule 3 names `CDM_*` schemas, plus "and the `LEARN`
  schema" only when `learn`.
- `ChatTools.specs` is per instance:
  - The describe tool keeps the name `describe_cdm_table`. Its description mentions LEARN only for premium (ruling:
    the name stays, the description changes).
  - `execute_sql` likewise.
- `describe_table(schema, table)` also serves `LEARN` from `learn_dictionary.json`.
- `_tool_describe_cdm_table` returns `{"error": LEARN_REFUSAL}` for LEARN on Core.
- **Preview** (`/api/v1/dictionary/preview`):
  - Premium may preview `LEARN`.
  - Core gets 403 with `PREVIEW_REFUSAL`.
  - Other non-CDM schemas still get 400.
- `_engine_kwargs(tenant)` passes `learn=allows_learn(tenant)`.

- [ ] **Step 1: Failing tests.**
  - `test_core_prompt_never_mentions_learn` and `test_premium_prompt_mentions_learn`.
  - `test_core_tool_specs_never_mention_learn`.
  - `test_core_describe_learn_refused`.
  - `test_premium_describe_learn_lists_columns`.
  - `test_core_preview_learn_403_with_message`.
  - `test_premium_preview_learn_allowed`.
  - An isolation case: Core describe and preview of LEARN through the routes.
- [ ] **Step 2–4: Implement.** Expected: `pytest -q` all passes.
- [ ] **Step 5: Commit** `feat: chat and data preview follow the tenant's license`, then open the PR and merge it.

### Task 4: `GET /api/v1/me`

**Files:** `lambda_handler.py`; test `tests/test_me_route.py`, plus a case in `tests/test_tenant_isolation.py`.

**Interfaces:**
- `GET /api/v1/me` returns `{"tenant": {"name": str, "license": "core"|"premium"}, "roles": [..], "is_admin": bool}`.
  - It calls `tenant_for` first.
  - `roles` are the user's `cognito:groups` mapped to Admin/Author/Developer, or `["Viewer"]` when none (the
    `roles.py` names).
  - It returns no tenant id and no MCP state.

- [ ] **Steps:**
  - Failing tests: the shape for A and for B; no `tenant_id` key; 403 for a not-ready tenant.
  - Implement.
  - `pytest -q`.
  - Commit `feat: /api/v1/me tells the web app its tenant and license`, then open the PR and merge it.

### Task 5: Tenant script `create`, `add-users`, `set-connection`, `set-license`

**Files:** `tenant_admin.py`, `scripts/tenant.py`, `scripts/set-snowflake-secret.sh` (validate the tenant id as a
UUID); tests `tests/test_tenant_admin.py`.

**Interfaces:**
- **`create_tenant`:** `create_tenant(*, dynamodb, secrets, cognito, user_pool_id, env, name, license, connection:
  dict | None = None, copy_connection_from: str | None = None, users: list[tuple[str, str]] = (), send_email: bool =
  True, temporary_password: str | None = None) -> dict`.
  - It returns `{tenant_id, users_created, users_refused: [email]}`.
  - Exactly one of `connection` / `copy_connection_from` is required. `copy_connection_from` copies another tenant's
    secret server-side. My call: it lets the Core tenant reuse the current connection without the credentials
    touching disk.
- **`add_users`:** `add_users(*, cognito, user_pool_id, tenant_id, users, send_email=True, temporary_password=None)
  -> dict`.
  - It refuses an email that exists with any other or no `custom:tenant_id`.
  - It adds an existing same-tenant user's role group if missing.
  - Roles: `Admin` → `illuminate-admins`, `Author` → `illuminate-authors`, `Developer` → `illuminate-developers`,
    `Viewer` → no group.
- **`set_connection`:** `set_connection(*, secrets, tenant_id, connection | copy_connection_from)`. It requires the
  tenant to exist.
- **`set_license`:** `set_license(*, dynamodb, tenant_id, license)`. It validates against `LICENSES` and updates
  `updated_at`.
- **CLI:**
  - `create --name --license --snowflake <json file> | --copy-connection-from <tenant_id> --users <csv>`.
  - `add-users <tenant_id> --users <csv>`.
  - `set-connection <tenant_id> ...` and `set-license <tenant_id> <license>`.
  - `--no-email` uses `MessageAction=SUPPRESS`. The temporary password is read from env
    `ILLUMINATE_TEMP_PASSWORD`, and it is required with `--no-email`.
  - It never prints a password or secret.
  - The CSV has the header `email,role`.

- [ ] **Step 1: Failing moto tests.**
  - Create a tenant with a copied connection and two users; check their claims and groups.
  - `test_add_users_refuses_other_tenant_email`, which also refuses a user with no tenant.
  - `test_add_users_is_idempotent_for_same_tenant`.
  - `test_set_license_rejects_unknown`.
  - `test_set_license_takes_effect_after_registry_cache`: patch monotonic; after 61 s, `get_tenant` shows the new
    license.
  - `test_set_connection_takes_effect_after_credential_ttl`.
  - `test_cli_never_prints_password_or_secret`.
  - `test_set_snowflake_secret_rejects_non_uuid` (shell, via subprocess with a fake `aws` on PATH).
- [ ] **Steps 2–4: Implement.** Expected: `pytest -q` all passes.
- [ ] **Step 5: Commit** `feat: tenant script creates tenants, users, connections and licenses`, then open the PR
  and merge it.

### Task 6: Seed tenant per environment; no re-tenanting by deploy; docs

**Files:** `cdk/lib/base/auth.ts`, `cdk/bin/illuminate.ts`, `cdk/cdk.json`, `cdk/test/check_template.py`,
`docs/DEPLOYMENT.md`.

**Changes:**
- **Context:** `initialTenantIds: {"dev": "<uuid>"}`, keyed by environment. A missing key means no seed resources.
  `INITIAL_TENANT_ID` still overrides it.
- **`InitialUserTenantId`:**
  - it gets `onCreate` only, with a stable physical id (`initial-user-tenant-${email}`), so changing the configured
    tenant never re-stamps an existing user;
  - CloudFormation will replace the resource once, which re-runs `onCreate` with the same id, a no-op.
- **`check_template.py`:** asserts that no seed custom resource has an `Update` call.
- **DEPLOYMENT.md:**
  - fix lines 153–155 (credentials are re-read every 300 s);
  - fix the `npm deploy:api` note (it needs `--exclusively` on upgrade);
  - document the tenant script's subcommands.

- [ ] **Steps:**
  - Write the failing template check.
  - Implement.
  - Run `npx tsc --noEmit` and the template check.
  - Commit `chore: seed tenant per environment; deploys never re-tenant the seed user`, then open the PR and merge
    it.

### Task 7 (web app): license-aware dictionary preview and import

**Files (illuminate-poc):**
- new `src/services/meApi.ts`;
- `src/context/AuthContext.tsx`: expose `tenant: {name, license} | null`;
- new `src/lib/license.ts` with `isLearnSchema(schema)`, `previewState(license, schema, isAdmin) -> {visible: bool,
  enabled: bool, reason?: string}` and `referencesLearn(sql) -> bool`;
- `src/components/schema/EntityDetail.tsx`: for a Core admin on a LEARN table, the Data Preview tab is visible but
  disabled, with `title` = `PREVIEW_REFUSAL`; it never calls preview;
- `src/components/queries/ImportQuery.tsx`: Core plus `referencesLearn` shows `LEARN_REFUSAL` and doesn't convert;
- `src/lib/license.test.ts`.

**Rules:**
- **On sign-out,** `tenant` is cleared.
- **The header shows the tenant name** next to the user's name. My call: it shows which university you are in.

- [ ] **Steps:**
  - Failing Vitest cases for the three helpers. `referencesLearn` matches `LEARN.`, `"LEARN".` and `learn.`
    case-insensitively, and ignores `LEARNING` and `CDM_LMS.LEARN_X`.
  - Implement.
  - Run `npm test` and `npx tsc --noEmit`.
  - Commit `feat: Core tenants see LEARN in the dictionary with preview disabled`, then open the PR and merge it.

### Task 8: Deploy, create the Core tenant, live checks

- [ ] **Deploy.** Deploy `IlluminateApi-dev --exclusively`, then `IlluminateBase-dev --exclusively`, then the web
  app (its existing deploy).
- [ ] **Create the Core tenant.**
  - `scripts/tenant.py create --name "Ridgeview Community College" --license core --copy-connection-from
    c2da348c-96e8-4014-9e72-9ba6b4eaac14 --users <csv> --no-email`.
  - Users: `admin@ridgeview.example.edu` (Admin) and `viewer@ridgeview.example.edu` (Viewer).
  - Set `ILLUMINATE_TEMP_PASSWORD` from a generated value saved only to a local file the user can read
    (`$S/ridgeview-temp-password.txt`, mode 600). Tell the user where it is; never print it.
- [ ] **Live checks:**
  - **Ridgeview:** `/api/v1/me` shows the name and `core`. A chat question needing LEARN gets the Premium message, a
    governed question works, and LEARN preview gets 403.
  - **Northbridge:** `/api/v1/me` shows `premium`. A LEARN count query through chat runs.
  - **Viewer:** `SELECT EMAIL FROM LEARN.USERS` is refused.
  - **Web app:** the dictionary shows LEARN to Ridgeview with preview disabled.
- [ ] **Isolation:** Ridgeview's admin sees none of Northbridge's or Lakeshore's overlays or conversations.
