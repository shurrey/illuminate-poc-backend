# Multi-tenancy, user profiles, sharing and the Illuminate MCP. Design

Date: 2026-10-16. Repos:
- `illuminate-conversational-intelligence` (backend, called "the API" below);
- `illuminate-poc` (web app);
- `illuminate-mcp-remote` (MCP server).

All three deploy to AWS account 045041252655 (us-east-1).

## Intent

Illuminate becomes a multi-tenant product. Each tenant is a made-up university with:
- its own data (its own Snowflake connection);
- its own semantic layer (the shared canonical catalog plus that tenant's overlays);
- its own settings and users.

People sign in once (one Cognito pool) and land in their own tenant, which is fixed by their account.

The MCP server is rebuilt so an AI client (Claude Desktop, Claude Code, VS Code) can do, on the user's behalf and
inside their tenant, what Ask Illuminate does:
- search the semantic layer;
- compile and run governed queries;
- fall back to ungoverned SQL with a stated reason;
- answer schema questions;
- reach the user's saved queries, the cards shared with them, and the standard reports.

Users get a profile that holds their home-page cards and saved queries. Cards and saved queries can be shared within
the tenant.

**The overriding requirement: no user may ever see anything from another tenant.** That covers data, definitions,
saved items, shares, user names, settings, cached results, and error text. Every other goal yields to it.

## Goals

1. **Tenants.** A tenant has:
   - a UUID `tenant_id`;
   - a display name (a made-up university, such as "Northbridge University");
   - a license, `core` or `premium`;
   - its own Snowflake connection;
   - settings, including MCP access.
2. **Tenant from the token only.** Every request's tenant comes only from the verified token's `custom:tenant_id`
   claim. No request parameter, header, path segment or body field ever selects or overrides it.
3. **Licenses.**
   - Core tenants get the CDM schemas only (`CDM_*`). Premium tenants also get the `LEARN` schema.
   - The semantic layer, the 14 standard reports and the 6 default cards are built on CDM, so they are the same for
     both licenses. Each runs against that tenant's data and overlays.
4. **Tenant script.** One script creates and maintains tenants: connection, name, license, and users with roles.
   There is no deployment automation and no in-product tenant setup. A tenant's connection is never visible in the
   product.
5. **User profiles.** Each user has a profile, stored server-side and keyed by tenant and user:
   - home-page card layout;
   - saved queries;
   - favorites and recents (now in browser storage).

   On first sign-in, a user sees the same 6 default cards as today.
6. **Sharing.**
   - A user can share a card or a saved query with specific people in their tenant, with the whole tenant, or both.
   - A share is a live link: the recipient sees the owner's current version and can copy it into their own profile
     to change it.
   - A shared item always runs as the viewer, so the viewer's role rules apply. For example, a Viewer never sees PII
     in a card an Admin shared.
7. **MCP access.**
   - It is controlled per tenant by its Admins, in the admin panel, and defaults to off.
   - When it is off, MCP sign-in and every MCP call are refused with "MCP access is not enabled for your
     organization".
   - The check is one function, so a per-user switch can be added later with user management. Off at either level
     means off; a missing value means off.
8. **MCP client set-up page.** The admin panel's MCP page shows what an Admin needs to connect a client:
   - the MCP URL;
   - how sign-in works;
   - copy-ready configuration for Claude Desktop, Claude Code and VS Code.
9. **Partly governed results.** This applies to Ask Illuminate and the MCP alike, so both share it.
   - Raw SQL may build on a compiled governed query by embedding it, as a named CTE.
   - The result is labelled column by column: governed (with the definition it came from) or ungoverned (with the
     stated reason).
   - A result mixing both is labelled "partly governed".
10. **Fix carried in.** Chat's raw-SQL path must apply the Viewer identity rules (`allow_identity`, `extra_pii`)
    that compiled queries already apply.

## Non-goals

- Real deployment of tenants: no Snowflake provisioning and no account automation.
- User management in the product. Users and roles come from the script. The design leaves room for a later per-user
  MCP switch.
- Sharing across tenants, public links, or links for anyone outside the tenant.
- Different reports, cards or semantic layers by license. Licenses differ only by LEARN.
- Keeping the MCP server's own Snowflake connection, SQL planner, catalog copy, Redis, VPC or Fargate.
- The MCP server's "MCP Apps" (interactive dashboards inside the client). My call: they are dropped in the rebuild.
  Tools return data plus labels, and the client renders them.
- Any mention of QuickSight or bbd-analytics in product text.

## Anti-goals (failure even if it works)

- Any route by which tenant A sees tenant B's anything, including through:
  - a cache key;
  - a share;
  - a user search;
  - an error message;
  - an MCP token;
  - a Lambda container that served tenant B a moment ago.
- A tenant with no connection secret silently using another tenant's connection, or a shared default.
- A Core tenant reaching LEARN data through any route: data preview, Ask Illuminate, the MCP, or a written, pasted or
  imported saved query. LEARN metadata in the data dictionary is not data, and stays visible.
- A label claiming "governed" for something the server did not verify.
- One user's browser state showing up for the next user on the same browser.
- MCP access defaulting to on, or staying usable after an Admin turns it off. A token issued while it was on is
  refused once it is off.

## Constraints

- One Cognito pool: `illuminate-users-dev` in `cdk/lib/base/auth.ts`.
  - `custom:tenant_id` is not writable by users.
  - Roles come from `cognito:groups` (Admin, Author, Viewer, Developer), and are pool-wide names. The tenant comes
    only from the claim.
- The API runs on Lambda through a Function URL with auth type NONE, so the JWT check is the only gate. It must stay
  strict:
  - check the signature, expiry, issuer and audience;
  - the audience list grows to include the MCP app client, and nothing else.
- Viewers see counts only, never PII. Admin, Author and Developer see everything. This holds on every route, including
  shared items and the MCP.
- The Python projects make HTTPS calls, so `truststore>=0.10` is a dependency and `truststore.inject_into_ssl()`
  runs at package import.
- Tests never call Bedrock or AWS. Isolation tests use fakes for DynamoDB, Secrets Manager and Snowflake.
- The MCP error message is in American English, exactly: "MCP access is not enabled for your organization".
- Process (Scott, 2026-10-16: "this is just our project. No PR reviews required"):
  - one PR per unit, branched from `main`;
  - self-merge after tests pass, and keep going;
  - at the end of each phase, a fresh review agent on the most capable model reviews the phase, and fixes land as
    their own PR.

## Decided (by Scott)

- **The current deployment becomes a Premium tenant.**
  - Its users, data and overlays move to a new UUID tenant.
  - Its display name is a made-up university (my call: "Northbridge University").
  - The second tenant, created with the script, is a Core tenant with another made-up name.
- **The tenant script** takes:
  - the Snowflake connection details;
  - the tenant's display name;
  - the license (Core or Premium);
  - users with roles.

  It associates each user with the tenant.
- **Each tenant may use a different Snowflake instance.** The script stores whatever connection it is given.
- **The Core tenant (Scott, 2026-10-16):** "Core will be a separate account, but the only difference is the LEARN
  schema, so you can build against the current connection and just ignore the LEARN schema to test."
  - For this project, the Core tenant's secret holds the same connection details as the Premium tenant's.
  - LEARN is blocked for Core by the license rules in section 2.
  - It is still its own secret, so pointing Core at its own account later is a `set-connection` call with no code
    change.
  - Isolation tests that need different data per tenant use fakes.
- **Core users:**
  - The data dictionary shows LEARN to Core users in full, exactly as Premium users see it. It is a reason to upgrade.
    Only the Data Preview button is disabled for LEARN tables, with a tooltip saying why.
    (Scott, 2026-10-16: "we want to let people use the data dictionary as a carrot to upgrade. We only need to disable
    the Data Preview button.")
  - A Core user importing or opening a query that only works on Premium is told so.
- **MCP sign-in** is OAuth as yourself, through the same Cognito pool. The resulting token is tied to your tenant.
- **MCP tools:** the client AI drives Illuminate's tools (search, compile, run, describe, raw SQL, saved queries,
  shared cards, reports), not one "ask" tool. The MCP follows Ask Illuminate's governed-first rule.
- **MCP control:** per tenant, Admins only, default off, with the set-up information next to the switch. It should
  be designed for a later per-user switch.
- **Partly governed labelling** is in scope.
- **Sharing** is a live link with copy-to-edit, to specific people, the whole tenant, or both.
- **Reuse:** the MCP calls the API instead of duplicating its catalog, compiler, guard or Snowflake client.
- **Admin-panel Snowflake connection settings** are removed if present. Recon found none, so nothing to remove.

## Design

### 1. Tenant registry and connection

- **DynamoDB table `illuminate-tenants-{env}`**, keyed by `tenant_id` (UUID string). Fields:
  - `name`;
  - `license` (`core` | `premium`);
  - `mcp_enabled` (bool, default false);
  - `created_at`, `updated_at`.
- **Snowflake secret `illuminate/tenants/{tenant_id}/snowflake`**, one per tenant. It holds the account, user,
  key/password, role, warehouse and database. It is readable only by the API's role.
- **Tenant context.** `snowflake_client` resolves a connection by tenant and keeps a small per-tenant connection
  cache inside the Lambda container. All of these take a tenant context derived from the token:
  - `validate_and_execute`;
  - `query_preview`;
  - the semantic-layer database resolution (now resolved at import);
  - chat's tool set and system prompt (now built once).
- **Failing closed:**
  - A tenant with no registry row, or no secret, gets a 403 / "Your organization is not set up yet". There is no
    fallback.
  - The global secret `illuminate/dev/snowflake` is retired.
- **Caches.** Every in-process cache that holds tenant-dependent data is keyed by `tenant_id`:
  - catalog-with-overlays;
  - terms;
  - search index;
  - dictionary previews;
  - report option lists.

  The public data dictionary metadata stays global, because it is the same public document for everyone.

### 2. License enforcement (server-side)

- **The schema allow-list in the execution guard is computed from the tenant's license:**
  - Core: `CDM_*` and `INFORMATION_SCHEMA`;
  - Premium: also `LEARN`.

  A Core query naming a LEARN table is refused with "This query uses LEARN data, which is available with Illuminate
  Premium."
- **Compiled SQL** is checked against CDM as today, since the semantic layer is CDM-only.
- **The dictionary endpoints return LEARN unchanged for every tenant.** The response includes the caller's license,
  so the web app knows when to disable preview. The preview endpoint refuses LEARN for Core with the same message.
- **Chat and the MCP.**
  - For Core, the system prompt and `describe_cdm_table` say nothing about LEARN, and the guard is the backstop.
  - For Premium, the raw-SQL tool's description and the describe tool cover LEARN tables. My call: rename the tool's
    user-facing description, not its name.
- **Saved queries** that reference LEARN are refused for Core on save, import or run, with the same message. The
  check runs in the guard, so it applies however the query arrived.
- **In the web app,** for Core users on LEARN tables, the Data Preview button is disabled with the tooltip "Data
  preview for LEARN tables is available with Illuminate Premium". Nothing else in the dictionary changes. This is
  display only; the server is the enforcement.
- **LEARN exists in the current Snowflake.** The current database has a `LEARN` schema with 192 tables, checked
  2026-10-16, so Premium can be tested live.
- **LEARN metadata and PII come from the public data dictionary.** It lists 3,446 LEARN columns, 228 of them flagged
  `isPii`.
  - `build_dictionary_snapshot.py` is extended to write a LEARN snapshot and a LEARN PII list.
  - PII is matched by schema, table and column, not bare column name. `USER_PK1`, for example, is PII on some LEARN
    tables only.
  - The Viewer identity rules apply to LEARN PII exactly as to CDM PII. My call: a LEARN column with no dictionary
    entry is treated as PII for Viewers, so the rule fails closed.
- **Saved queries can hold raw SQL (see section 4a),** so LEARN SQL can be saved on Premium. For Core, the guard
  refuses it on save and on run.

### 3. Tenant script: `scripts/tenant.py` (API repo)

Subcommands; all print the tenant ID, and never print a secret:

- **`create`** takes `--name`, `--license core|premium`, `--snowflake <json file>` and `--users <csv: email,role>`.
  It:
  - generates a UUID;
  - writes the registry row with MCP off;
  - writes the secret;
  - creates each user with `custom:tenant_id` set and their role group. Cognito sends the temporary password.
- **`add-users`** takes a tenant ID and a CSV. A user whose email already exists in the pool under any other tenant is
  refused, and named in the output. A user is never moved between tenants.
- **`set-connection`** and **`set-license`** take a tenant ID.
- **`migrate-current`** moves the existing deployment into a new Premium tenant. It:
  - creates the tenant;
  - copies the current global secret to the tenant's secret;
  - rewrites every existing user's `custom:tenant_id` (using the admin API);
  - moves overlay rows (and their history) from the old `tenant_id` key to the UUID.

  It is idempotent. It has a `--dry-run` mode, and reports counts.
- **`list`** shows the tenants: name, license and MCP state.

### 4. User profiles and stored items

- **DynamoDB table `illuminate-user-items-{env}`**:
  - partition key `tenant_id`;
  - sort key `item_key`: `user#{sub}#card#{id}`, `user#{sub}#query#{id}`, `user#{sub}#layout`,
    `user#{sub}#prefs`, `share#...`.

  With the tenant as the partition key, a query can't return another tenant's rows. Every read and write also
  checks it explicitly.
- **Conversations** (`illuminate-conversations-{env}`) gain `tenant_id`.
  - Every read checks both owner and tenant.
  - Existing rows are tagged with the Premium tenant by the migration.
- **API endpoints** (`/api/v1/me/...`):
  - layout (get/put);
  - cards (CRUD);
  - saved queries (CRUD);
  - prefs (favorites, recents).

  The tenant and user come from the token only.
- **First sign-in.** A user with no layout gets the 6 default cards (`src/data/dashboardCards.ts:57-82`, moved to or
  mirrored by the API, so the MCP and web agree).
- **Browser storage.** The web app moves cards, saved queries and favorites/recents from browser storage to these
  endpoints.
  - On first sign-in after the change, any local items are uploaded once into the signed-in user's profile, then the
    local keys are removed.
  - On sign-out, all `illuminate*` local keys are cleared, including `illuminate_chat_context`.
  - The semantic catalog cache in `semanticApi.ts` is keyed by tenant, and cleared on sign-out.

### 4a. Saved queries of every kind

(Scott, 2026-10-16: "If a customer builds a query that is ungoverned but they are happy with, they should be able to
write or import and save.")

**Kinds**
- **Governed:** a governed query definition (the existing `contract`), as today.
- **SQL:** raw SQL the user wrote, imported, or kept from Ask Illuminate or the MCP. Optionally, it embeds governed
  parts as named CTEs (section 8).
  - Each governed part is stored as its governed query definition, not as compiled text. At every run it is
    recompiled for the runner's tenant and spliced in, so it follows the current definitions.
  - Its column labels are worked out at run time, by the same verification as section 8.

**Ways in**
- **Write:** the query builder gains a SQL mode with an editor, Run and Save.
- **Import:**
  - If the pasted SQL converts fully to a governed query, the user is offered that, as today.
  - Otherwise, the user can save it as SQL, with whatever governed parts were recognized embedded.
- **Keep:** "Save query" on an Ask Illuminate result. In the MCP, a `save_query` tool. My call: the MCP gets it, since
  Ask Illuminate has it.

**Checks**
- **On save,** SQL is parsed and passed through the guard as the saver (license allow-list, and the guard's
  statement rules), with no rows fetched. A query the guard would refuse can't be saved, and the user sees why.
- **On every run,** the guard runs again as the runner, not the saver. The runner's role and license decide what
  comes back, including on shared queries. A Viewer running an Admin's SQL never gets PII.
- **Labels everywhere it appears:** an SQL query carries a governed, partly governed or ungoverned label in lists, on
  open and on results. The "reason" for user-written SQL is the description the user gave on save. It's optional, and
  it defaults to "Written by <owner name>".

**Not in scope (my call):** home-page cards stay governed single-value queries. A card can't be built from an SQL
query in this work.

### 5. Sharing

- **A share** is stored with the item, in the owner's tenant partition. It holds:
  - the item;
  - recipients: a list of user `sub`s and/or `tenant: true`;
  - who shared it, and when.
- **Recipients.** The share picker lists people in the same tenant only:
  - from an API endpoint that searches Cognito with the filter `custom:tenant_id = <caller's tenant>`;
  - returning display name and email only.

  Cognito's user search can't filter on custom attributes, so the API keeps a per-tenant user directory in the
  user-items table, written by the script and on sign-in. My call.
- **The "Shared with me" list** reads only the caller's tenant partition.
- **Opening a shared item** shows the owner's current version, read-only, with "Make a copy". The copy is a new item
  in the recipient's profile with no link back.
- **Running a shared item** runs as the viewer: the viewer's role and the tenant's license. A Viewer gets counts and
  "unavailable" visuals exactly as in their own items.
- **Unsharing or deleting** removes access at once. Copies already made stay.

### 6. MCP access setting and the admin page

- **`may_use_mcp(tenant, user) -> bool`** returns true only if the tenant's `mcp_enabled` is true. A per-user flag
  can be added later inside this one function; missing means off.
- **Where it's checked:**
  - at MCP sign-in, before a token is issued;
  - on every MCP request, read fresh, with at most 60 seconds of caching (my call). So turning it off stops an
    existing session within a minute.
- **Admin endpoints:**
  - `GET` and `PUT /api/v1/admin/settings/mcp` (Admins only, own tenant only). `PUT` takes `{enabled: bool}`.
  - `GET /api/v1/admin/mcp/setup` returns the MCP URL and the client snippets.
- **The web app** has a new admin page, "AI clients (MCP)". It shows:
  - the switch, showing off until changed;
  - the MCP URL with a copy button;
  - one paragraph on how sign-in works (you sign in as yourself in a browser window; what you can see follows your
    role);
  - copy-ready snippets for Claude Desktop (custom connector URL), Claude Code (`claude mcp add --transport http
    ...`) and VS Code (`.vscode/mcp.json`).

  The page is linked from the admin menu.
- **The calls the MCP makes to the API are refused** with the MCP error text when the setting is off. The API checks
  it for requests carrying the MCP client's audience. So a valid MCP token can't be used to reach the API directly
  once MCP is off.

### 7. MCP server rebuild (`illuminate-mcp-remote`)

**Shape**

- A thin Python service hosted as a Lambda Function URL in account 045041252655, in a small CDK stack.
  - It speaks MCP Streamable HTTP: JSON-RPC over `POST /mcp`, with JSON responses. SSE streaming is not needed.
  - It holds no Snowflake connection, no catalog, and no SQL logic. Every tool calls the API with the user's token.
- **Removed:** the old Fargate/ALB/Redis/VPC stack code, the planner, the optimizer, the direct Snowflake client, and
  the catalog copy.
- **The old deployment** in account 856599266077 (its stack, pool and domain) was already torn down on 2026-10-16.
  There is nothing to tear down.
- **Address (Scott, 2026-10-16: "for now, aws address is fine"):** the MCP is reached at its Lambda Function URL,
  which is HTTPS with AWS's certificate. A hurrey.dev name can be added later with CloudFront and a DNS-validated
  ACM certificate. The admin page reads the URL from configuration, so changing it needs no code change.

**Sign-in**

- OAuth 2.1 with PKCE, with Cognito as the identity provider:
  - a Cognito hosted domain is added to the pool;
  - a new public app client, `illuminate-mcp`, uses the authorization-code grant.
- MCP clients expect discovery and Dynamic Client Registration, which Cognito doesn't offer. The MCP serves:
  - the protected-resource metadata, and the authorization-server metadata;
  - a small registration endpoint that hands back the one Cognito public client ID (no per-client secrets);
  - an `/authorize` → Cognito redirect;
  - a `/token` proxy.
- **State.** Transient OAuth state (code challenges, registrations) lives in DynamoDB with a TTL, never in memory.
- **Access token.** The access token handed to the client is the Cognito ID token. Refresh is through Cognito's
  refresh token.
- **The API accepts that token** by adding the MCP client's ID to its audience list. Everything else about the check
  is unchanged.
- **The MCP brokers sign-in** with its own Cognito callback and its own short-lived authorization codes. Clients'
  localhost ports vary, and Cognito needs exact callback URLs.
- **`auth.ts`'s seed-user custom resource** is changed to stamp the new tenant UUID, not `blackboard-dev`. Tokens
  issued before the migration get a 403 until they refresh.
- **`may_use_mcp`** is checked at `/authorize` callback time. A refused user sees an HTML page with the error text,
  and no token is issued.

**Tools** (names are my call)

- **The semantic layer:**
  - `search_catalog`;
  - `describe_entity`;
  - `compile_query` (compile only);
  - `run_query` (governed, compiled);
  - `describe_table` (raw schema);
  - `run_sql` (ungoverned, needs `reason`, may embed compiled CTEs; see section 8).
- **Saved queries:** `list_saved_queries`, `get_saved_query`, `run_saved_query`, `save_query`.
- **Cards:** `list_cards` (mine and shared with me), `run_card`.
- **Reports:** `list_reports`, `get_report`, `run_report` (with filters).

**Behaviour**

- **Governed first.** The API enforces it server-side: a session's raw-SQL call is refused until a governed tool has
  been called in the same session (session = MCP session ID, stored with the tenant and user).
- **Server instructions.** The MCP's instructions text carries the same guidance as Ask Illuminate's prompt.
- **Results** return rows plus the provenance labels, so the client can say what is governed.
- **Tenant guard.** Every call re-checks the token's tenant against the MCP session's stored tenant. A mismatch ends
  the session.

**API changes the MCP needs**

- Endpoints for the raw-SQL path and table description, which today exist only inside chat. They are exposed as
  `/api/v1/sql/execute` and `/api/v1/schema/describe`, sharing `chat_tools.py`'s code.
- A session-scoped governed-first check those endpoints use.

### 8. Partly governed results

- **The compile response** includes a `compiled_id`: an HMAC over (tenant, compiled SQL, catalog version), signed by
  the API.
- **Raw SQL may include a CTE** named in the form `governed_<n>`. The request lists `{cte_name: compiled_id}`.
- **Before running it, the server:**
  1. recompiles each referenced query, or looks it up by ID, for the caller's tenant;
  2. checks that the CTE body is that compiled SQL, by matching normalized sqlglot text;
  3. traces the output columns. With sqlglot lineage, each output column is resolved to its sources.
     - A column whose sources are only governed CTE columns, passed through unchanged or renamed, is governed, and
       names its measure or dimension.
     - Any column that touches a raw table, or computes over governed columns, is ungoverned.
- **Labels.** A CTE that fails verification makes every column ungoverned, and the result says why ("the governed
  part was changed").
- **Provenance:**
  ```
  {governed: "full" | "partial" | "none",
   columns: {name: {governed: bool, definition?: ref, reason?: str}},
   reason}
  ```
- **Displays:**
  - Ask Illuminate shows a "Partly governed" badge and per-column markers in the result table header.
  - The MCP returns the same structure.
- **The Viewer identity rules and the license allow-list** apply to the whole statement, as for any raw SQL.

### 9. Isolation tests (required before any of this ships)

A dedicated test module in the API, `tests/test_tenant_isolation.py`, with two fake tenants. It must cover each of
these as "tenant B's token gets 404 or 403, and no B data appears in the response or logs":

- overlays;
- conversations;
- cards;
- saved queries;
- shares and share-recipient search;
- "Shared with me";
- reports (each tenant's own connection);
- catalog cache, search cache and terms cache;
- dictionary preview;
- the MCP setting;
- the MCP tools;
- `compiled_id` reuse across tenants;
- connection cache;
- a forged or altered `tenant_id` anywhere in a request.

Also:
- a Core tenant hitting LEARN through each route;
- a missing tenant secret failing closed.

## Delivery shape (builder refines)

Each phase is several PRs, merged bottom-up. The phases:

1. Tenant registry, tenant-scoped connection and caches, and the isolation test harness. Migration script, run
   against the dev account.
2. The tenant script (create, users, license), license enforcement, and the Core tenant created.
3. The profiles API and web migration off browser storage; sign-out clearing.
4. Partly governed results, and the chat Viewer-identity fix.
5. SQL saved queries (section 4a), which rely on phase 4's verification.
6. Sharing.
7. The MCP setting, the admin page, the API's MCP endpoints, then the MCP rebuild and its stack.

## Left to the builder

- Exact table key formats, endpoint payloads, and UI layout of the MCP admin page and share dialog. They should follow
  the existing patterns: `adminApi.ts`, the overlay editor's styles.
- How the MCP Lambda is packaged: Web Adapter as in the API, or a plain handler.
- Normalization rules for matching a compiled CTE, so long as a change to the governed SQL is never accepted.
