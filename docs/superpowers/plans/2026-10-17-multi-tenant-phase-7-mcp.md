# Multi-tenancy, Phase 7: the MCP. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan
> task-by-task.

**Goal:** An admin can switch on MCP access for their tenant. People in that tenant then connect Claude Desktop, Claude
Code or VS Code to Illuminate, sign in as themselves, and drive Illuminate's tools with exactly their web-app rights.

**Architecture:**
- **The API (this repo) gains:**
  - the MCP setting and its admin routes;
  - acceptance of the MCP client's ID tokens, refused per request when the setting is off;
  - a per-user rate limit and MCP sessions (governed-first, tenant guard);
  - the few routes the tools need that don't exist yet.
- **`illuminate-mcp-remote` is rebuilt** as a thin Python Lambda behind a Function URL. It:
  - brokers OAuth to Cognito;
  - speaks MCP JSON-RPC on `POST /mcp`;
  - implements every tool as one API call with the user's token.

**Spec:** `docs/superpowers/specs/2026-10-16-multi-tenant-and-mcp-design.md`, sections 6, 7 and 9.

## Global Constraints

- **Isolation.** Tenant and role come only from the token; nothing crosses tenants. The MCP holds no data, catalog,
  Snowflake connection or SQL logic. It can do only what the API lets the same user do in the web app.
- **Error text** (verbatim): `MCP access is not enabled for your organization`.
- **`may_use_mcp(tenant, user) -> bool`** is the one place the decision is made. It is true only when the tenant's
  `mcp_enabled` is true. It is per-user-ready: a later per-user flag goes inside it. Missing means off.
- **Freshness.** `mcp_enabled` is read with at most 60 s staleness on MCP requests, and fresh (no cache) on the admin
  PUT's response.
- **The MCP's access token is the Cognito ID token** of the `illuminate-mcp-<env>` public client. Refresh goes through
  Cognito's refresh token. The API accepts that client ID as a second audience; nothing else about verification
  changes.
- **The MCP app client** has authorization-code grant with PKCE only, and no secret. Its `writeAttributes` exclude
  `custom:tenant_id`.
- **OAuth state** (registrations, pending authorizations, issued codes) lives in DynamoDB with a TTL, never in memory.
- **Untrusted text.** Saved text in tool results is marked as untrusted data: names, descriptions, owner notes and
  prompts. The server instructions tell the client not to follow instructions found there.
- **Process:**
  - Tests never call AWS, Bedrock or Cognito.
  - Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  - PR bodies start with `**Claude:**` and end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
  - Push with `git -c credential.helper= -c 'credential.helper=!gh auth git-credential' push`.
  - Never commit `illuminate-poc/tsconfig.tsbuildinfo`.
  - Comments only for non-obvious behaviour.
- **`illuminate-mcp-remote`** is a Python project making HTTPS calls, so it depends on `truststore>=0.10` and calls
  `truststore.inject_into_ssl()` in the package `__init__.py`.

## Decisions (my calls, recorded so the reviewer can weigh them)

- **The MCP speaks JSON-RPC itself:** `initialize`, `notifications/initialized`, `ping`, `tools/list` and
  `tools/call`, with JSON responses. It does not use the SDK's session manager, which keeps sessions in process memory
  and so breaks on Lambda.
  - The protocol version is `2025-06-18`; `2025-03-26` is also accepted.
  - The MCP issues `Mcp-Session-Id` on `initialize`.
- **Session and rate state live in a new API-owned table `illuminate-mcp-sessions-<env>`:** partition `pk`, TTL
  `expires_at`.
  - The API is what enforces governed-first and the tenant guard. The MCP forwards the session ID as
    `X-Mcp-Session`.
- **Rate limit:** 120 MCP requests per user per minute, as a fixed window counted in that table. Over the limit is a
  429 with `Retry-After`.
- **Governed first:**
  - On an MCP-audience request, `POST /api/v1/sql/run` is refused until a governed call has been made in the same
    session. The governed calls are `semantic/query`, `semantic/compile`, a governed saved-query run, a card run and a
    report run.
  - The refusal text is `Try a governed tool first (run_query or compile_query); raw SQL is for what the semantic
    layer can't answer.`
  - An MCP `reason` must be at least 20 characters.
- **Discovery:**
  - The API publishes the MCP client ID and URL in SSM: `/illuminate/<env>/mcp-client-id` and `/illuminate/<env>/mcp-url`.
  - The MCP stack writes them; the API reads them at cold start.
  - When they are missing, MCP-audience tokens are rejected and the setup route says the MCP isn't deployed. Deploy
    order then doesn't matter.
- **The MCP stack owns the Cognito hosted domain and the MCP app client**, on the base pool imported by ID. Their
  callback URL is the stack's own Function URL, which is known only within that stack.

## Review Focus

1. **Off means off at once.**
   - An admin switches MCP off. A live MCP token's next API call is refused with the error text within 60 s. Its
     `/authorize` callback refuses too.
   - Tested in Tasks 2 and 6.
2. **An MCP token from tenant B, with a session ID created by tenant A,** is refused and ends that session. It never
   reads A's session state. Tested in Task 3.
3. **A Viewer through MCP gets exactly the Viewer's web-app view.** That includes the redacted shared items and no PII.
   Tested in Task 7 through the API's existing redaction, with no MCP-side logic.
4. **A forged or altered OAuth `state`, PKCE verifier, code or redirect URI** fails, as does a reused code or an
   expired pending authorization. Tested in Task 6.
5. **Saved text that says "ignore previous instructions…"** reaches the client only inside the untrusted wrapper.
   Tested in Task 7.

---

### Task 1: The MCP setting, admin routes and setup

**Files:** `mcp_access.py` (new, bundled), `tenancy.py`, `lambda_handler.py`, `cdk/lib/api/lambda-proxy.ts`;
tests `tests/test_mcp_access.py` and isolation cases.

**Interfaces:**
- **`mcp_access.may_use_mcp(tenant: Tenant, user: dict) -> bool`** and `MCP_REFUSAL = "MCP access is not enabled
  for your organization"`.
- **`tenancy.set_mcp_enabled(tenant_id: str, enabled: bool) -> Tenant`** is an `update_item` with
  `SET mcp_enabled, updated_at`. It drops that tenant from `_cache`.
- **Routes**, all Admin-only through `_require_admin_tenant`:
  - **`GET /api/v1/admin/settings/mcp`** returns `{enabled}`.
  - **`PUT /api/v1/admin/settings/mcp`** takes `{enabled: bool}`, 400 otherwise, and returns `{enabled}`.
  - **`GET /api/v1/admin/mcp/setup`** returns:
    - `{deployed: false}` when the SSM URL is missing;
    - otherwise `{deployed: true, url, snippets: {claude_desktop, claude_code, vscode}}`. `url` is the MCP's `/mcp`
      endpoint. `claude_code` = `claude mcp add --transport http illuminate <url>`. `vscode` is the
      `.vscode/mcp.json` text `{"servers": {"illuminate": {"type": "http", "url": "<url>"}}}`. `claude_desktop` is the
      URL to paste as a custom connector.
- **`GET /api/v1/me`** gains `mcp_enabled`.
- **The SSM discovery reader** `_mcp_discovery() -> tuple[client_id | None, url | None]` is cached 300 s per instance.
- **IAM:** `dynamodb:UpdateItem` on the tenant table, and `ssm:GetParameter` on `/illuminate/<env>/mcp-*`.
  `mcp_access.py` is added to the bundle list.

**Tests:**
- defaults to off;
- PUT, then GET, round trips, and an Author or Viewer gets 403;
- the PUT writes only the caller's own tenant row (a fake table asserts the key);
- setup with and without discovery;
- `/me` carries the flag;
- isolation `*_never_crosses` for all three routes.

Commit `feat: admins can turn MCP access on for their organization`, then open the PR and merge it.

### Task 2: The API accepts MCP tokens, only while MCP is on, at a bounded rate

**Files:** `lambda_handler.py`, `mcp_access.py`, `cdk/lib/api/lambda-proxy.ts`, and a new table in the API stack;
tests `tests/test_mcp_audience.py` and isolation cases.

**Interfaces:**
- **`_validate_token`** accepts `aud` in `{USER_POOL_CLIENT_ID, mcp_client_id}`, with `mcp_client_id` from
  `_mcp_discovery`. It marks the returned claims with `"_via_mcp": True` when the audience is the MCP client. A
  client-supplied `_via_mcp` claim is ignored and overwritten.
- **For every authenticated route called with `_via_mcp`:**
  - The tenant comes from `tenant_for`.
  - Then `may_use_mcp` is checked: false gives 403 `{"detail": MCP_REFUSAL}`. The tenant cache's 60 s staleness is
    the bound.
  - Then the rate limit is checked.

  This is one dependency or middleware, so no route can skip it. It applies to every route the MCP can reach.
- **Admin routes always refuse an MCP token** with 403, even when the setting is on.
- **Rate limit:** `mcp_access.charge(tenant_id, sub, now) -> int | None` returns `Retry-After` seconds when over.
  - It does an atomic `ADD n :1` on the item `rate#{tenant}#{sub}#{epoch_minute}` with TTL, limit 120.
  - The table is `illuminate-mcp-sessions-<env>` (CDK in the API stack: `pk` string, TTL `expires_at`,
    on-demand), passed as `MCP_SESSIONS_TABLE`. IAM covers GetItem, PutItem, UpdateItem and DeleteItem.

**Tests:**
- An MCP-audience token is accepted when on, and refused with the exact text when off.
- Flipping the setting is effective after the cache expires: monkeypatch the clock and assert refusal at 61 s.
- An unknown audience is refused.
- With discovery missing, an MCP token is refused.
- Admin routes refuse MCP tokens.
- The 121st call in a minute is a 429 with `Retry-After`, and the next minute is allowed.
- Web-app tokens are untouched, with no rate limit.
- Isolation: a tenant B MCP token never sees A, and `rate#` keys are per tenant.

Commit `feat: the API accepts MCP tokens while MCP is on, at a bounded rate`, then open the PR and merge it.

### Task 3: MCP sessions — governed first, reasons, and the tenant guard

**Files:** `mcp_access.py`, `lambda_handler.py`; tests `tests/test_mcp_sessions.py`.

**Interfaces:**
- **Header `X-Mcp-Session`** (1–128 chars of `[A-Za-z0-9_-]`) is required on MCP-audience calls to `/sql/run`, and
  optional elsewhere.
- **The session item** is `session#{id}` = `{tenant_id, sub, governed: bool, expires_at: now+24h}`.
  - `mcp_access.session(id, tenant_id, sub) -> dict` creates it on first sight. When it exists with a different
    tenant or sub, it deletes the item and raises 403 `"This MCP session belongs to someone else; start a new one"`.
  - `mark_governed(id)` sets `governed`.
- **Governed-first:**
  - The governed calls listed in Decisions call `mark_governed` on success.
  - `/sql/run` with `_via_mcp` and an ungoverned session gets a 400 with the governed-first text.
  - `/sql/run` with `_via_mcp` and a `reason` shorter than 20 characters (stripped) gets a 400 `"Give a reason of at
    least 20 characters for running raw SQL"`.
  - `/sql/validate` and `/schema/describe` are not gated.
- **Web-app tokens** are unaffected, and the header is ignored without `_via_mcp`.

**Tests:**
- SQL refused before any governed call, allowed after, in the same session only;
- a short reason is refused;
- a cross-tenant session ID is refused and deleted;
- a same-tenant other-user session ID is refused;
- the header's shape is checked;
- a governed call that fails does not mark the session.

Commit `feat: MCP sessions are governed first and stay with their owner`, then open the PR and merge it.

### Task 4: The routes the tools need, and two carried hardening items

**Files:** `lambda_handler.py`; tests `tests/test_tool_routes.py`, `tests/test_chat_errors.py`.

**Interfaces:**
- **`GET /api/v1/semantic/search?q=&limit=`** (q ≤ 200 characters, limit ≤ 25) runs the chat's
  `semantic_layer.search`. It uses the caller's tenant catalog and returns `{results: [...]}` in the chat tool's
  shape.
- **`GET /api/v1/semantic/entity/{entity_id}`** returns the dataset, metric, measure or dimension description from
  the caller's tenant catalog, in the chat's `describe_entity` shape. An unknown ID is a 404.
- **`POST /api/v1/me/cards/{card_id}/run`** runs a built-in or own card's contract as the caller. It counts as
  governed. A pinned `sh_` ID redirects to the shared run logic, so the masking applies.
- **Hardening:**
  - Chat and SSE error paths send `"Something went wrong; try again."` instead of `str(e)`. The details are logged.
  - `_request_owners` and `_cancelled_requests` are keyed by `(owner, request_id)`.

**Tests:**
- search and entity are per tenant, so an overlay in A is not seen by B;
- the Core LEARN rules are unchanged;
- card run for built-in, own and pinned-shared cards, with a Viewer's run masked;
- a chat exception text is not echoed;
- cancel by another owner with the same request ID does nothing;
- isolation for the three new routes.

Commit `feat: catalog search, entity description and card runs over REST`, then open the PR and merge it.

### Task 5 (web app): the "AI clients (MCP)" admin page

**Files (illuminate-poc):**
- `src/services/adminApi.ts` (`getMcpSetting`, `setMcpSetting`, `getMcpSetup`);
- `src/app/admin/mcp/page.tsx` (new);
- `src/components/NavDrawer.tsx`, with an entry `{ label: "AI clients (MCP)", href: "/admin/mcp", adminOnly: true }`
  and a lucide `Bot` icon;
- `src/lib/mcpSetup.ts` and its test.

**Behaviour:** the page follows the definitions page's admin gate and Tailwind patterns.
- **A switch.** It shows off until the server says otherwise, and is disabled while saving. A server error shows
  inline.
- **The MCP URL**, with a copy button.
- **One paragraph:** "People sign in as themselves in a browser window. What they can see and do through an AI client
  is exactly what they can see and do here, following their role."
- **Three snippets** with copy buttons. When `deployed` is false, the page shows "The MCP server isn't deployed yet"
  instead.
- **The pure helpers** (snippet ordering and labels, copy-text building) are in `src/lib/mcpSetup.ts`.

**Checks:** run `npm test`, `npx tsc --noEmit`, `npm run lint` and `npm run build`.

Commit `feat: an admin page for AI clients (MCP)`, then open the PR and merge it.

### Task 6 (illuminate-mcp-remote): a clean slate and the OAuth broker

This is the walking skeleton, so its PR is larger; say so in the description.

**Files:**
- **Delete** everything under `src/`, `tests/`, `cdk/` and `scripts/`, plus `Dockerfile`, `docker-compose.yml`,
  `dev_tenants*.json` and `SPEC.md`.
- **New `src/illuminate_mcp/` package:**
  - `__init__.py` (truststore);
  - `app.py` (FastAPI);
  - `config.py` (env: `COGNITO_DOMAIN`, `COGNITO_REGION`, `STATE_TABLE`, `API_URL` and `MCP_CLIENT_ID_PARAM`).
    The client ID is read from that SSM parameter once per cold start. `PUBLIC_URL` is `https://<Host header>`
    unless the env sets it: a Function URL is routed by its host, so the header is the function's own domain;
  - `oauth.py`;
  - `state.py`, the DynamoDB store with TTL, with an in-memory fake for tests;
- `tests/test_oauth.py`;
- `pyproject.toml`, with Python 3.12 and the dependencies fastapi, httpx, pyjwt[crypto], boto3 and truststore;
- `README.md`.

**Endpoints:**
- **`GET /.well-known/oauth-protected-resource`** returns `{resource: PUBLIC_URL + "/mcp", authorization_servers:
  [PUBLIC_URL]}`.
- **`GET /.well-known/oauth-authorization-server`** returns `issuer`, `authorization_endpoint`, `token_endpoint` and
  `registration_endpoint`, plus:
  - `code_challenge_methods_supported: ["S256"]`;
  - `grant_types_supported: ["authorization_code", "refresh_token"]`;
  - `token_endpoint_auth_methods_supported: ["none"]`.
- **`POST /register`** stores `{redirect_uris}` under `reg#{client_id}` and returns a generated `client_id`. The MCP
  uses one Cognito client underneath.
  - Redirect URIs must be `https://` or `http://localhost|127.0.0.1:<port>/…`.
  - At most 5 are accepted, each up to 500 characters.
- **`GET /authorize`:**
  - It requires `client_id` (registered), `redirect_uri` (registered for it), `response_type=code`,
    `code_challenge` (S256) and `state`.
  - It stores `pending#{nonce}` with a 10-minute TTL and redirects to Cognito `/oauth2/authorize`, with the MCP's
    own callback, its own PKCE pair, and `state=nonce`.
- **`GET /callback`:**
  - It exchanges Cognito's code with the MCP's own verifier.
  - It calls the API's `GET /api/v1/me` with the ID token. A 403 carrying the MCP refusal, or `mcp_enabled` false,
    renders an HTML page with the error text, and no code is issued.
  - Otherwise it issues its own one-time code `code#{random}` with a 5-minute TTL, holding the Cognito tokens and the
    client's challenge.
  - It redirects to the client's `redirect_uri` with `code` and the client's `state`.
- **`POST /token`:**
  - `authorization_code` checks `client_id`, `redirect_uri` and the S256 verifier. The code is deleted on use (a
    conditional delete, so a replay fails). It returns `{access_token: id_token, refresh_token, token_type: "Bearer",
    expires_in}`.
  - `refresh_token` calls Cognito's token endpoint and returns the new ID token as the access token.
- **401s from `/mcp`** carry `WWW-Authenticate: Bearer resource_metadata="<PUBLIC_URL>/.well-known/oauth-protected-resource"`.

**Tests:**
- metadata;
- registration validation;
- authorize parameter checks;
- callback refusal renders the error and stores nothing;
- the happy path end to end with a faked Cognito and API;
- a wrong verifier, wrong redirect, reused code, expired pending and unknown state all fail;
- refresh.

Commit `feat: rebuild as a thin MCP — OAuth broker over Cognito`, then open the PR and merge it.

### Task 7 (illuminate-mcp-remote): MCP JSON-RPC and the tools

**Files:** `src/illuminate_mcp/mcp.py` (JSON-RPC), `tools.py`, `api_client.py`, `instructions.md`; tests
`tests/test_mcp.py`, `tests/test_tools.py`.

**Interfaces:**
- **`POST /mcp`** requires a Bearer token, or returns 401 with `WWW-Authenticate`.
  - `initialize` returns `protocolVersion`, `capabilities: {tools: {}}`, `serverInfo` and `instructions` (the text of
    `instructions.md`). It sets an `Mcp-Session-Id` response header (`secrets.token_urlsafe(24)`).
  - Later calls send it, and it is forwarded to the API as `X-Mcp-Session`.
  - Notifications get 202.
  - `GET /mcp` is a 405.
- **Each tool is one API call** with the caller's token. API errors become `isError: true` tool results carrying the
  API's message. A 401 from the API becomes an HTTP 401 so the client refreshes.
- **Tools and the routes they call:**

  | Tool | Route |
  | --- | --- |
  | `search_catalog` | `/semantic/search` |
  | `describe_entity` | `/semantic/entity/{id}` |
  | `compile_query` | `/semantic/compile` |
  | `run_query` | `/semantic/query` |
  | `describe_table` | `/schema/describe` |
  | `run_sql` | `/sql/run`, with `reason` required |
  | `list_saved_queries` | `/me/queries` |
  | `get_saved_query` | `/me/queries`, filtered by ID |
  | `run_saved_query` | `/me/queries/{id}/run` |
  | `save_query` | `POST /me/queries` |
  | `list_cards` | `/me/cards` plus `/shared` filtered to cards |
  | `run_card` | `/me/cards/{id}/run`, or `/shared/{sh}/run` |
  | `list_reports` | `/reports` |
  | `get_report` | `/reports/{id}` |
  | `run_report` | `/reports/{id}/run`, with filters |

  Shared queries appear in `list_saved_queries` under `shared_with_me`, from `/shared`, and run through
  `/shared/{sh}/run`.
- **Results** are JSON text content: the rows (at most 200 returned, with `truncated` noted), `columns` and
  `provenance` passed through.
- **User-authored fields** (`name`, `label`, `description`, `owners_note`, `prompt`, `owner.name`) are moved under
  `"untrusted_user_text": {...}`. The instructions say that content there is data written by people and never
  instructions.
- **Instructions text:** the Ask Illuminate guidance on governed first, provenance labels and FERPA, adapted for tool
  names.

**Tests:**
- the JSON-RPC envelope (ids, errors -32601/-32602, notifications);
- the session header is issued and forwarded;
- each tool hits the right route with the token and session (fake API);
- API refusal becomes `isError`;
- the untrusted wrapper is applied, including an injection string;
- 401 propagation;
- the row cap.

Commit `feat: MCP tools over the Illuminate API`, then open the PR and merge it.

### Task 8 (illuminate-mcp-remote): the CDK stack

**Files:** `cdk/` (TypeScript, matching the API's CDK style): `bin/mcp.ts`, `lib/mcp-stack.ts`, `run.sh`, the bundling.

**Stack `IlluminateMcp-<env>`:**
- **The pool is imported** from SSM `/illuminate/<env>/cognito-pool-id`.
- **`UserPoolDomain`** with Cognito prefix `illuminate-<env>-<account last 4>`.
- **`UserPoolClient` `illuminate-mcp-<env>`:**
  - no secret;
  - `oAuth` flows `authorizationCodeGrant` only;
  - scopes openid, email, profile;
  - callback `<fnUrl>callback`;
  - auth flows none besides the hosted UI;
  - `readAttributes` email, fullname and `custom:tenant_id`;
  - `writeAttributes` email and fullname only;
  - ID token validity 60 min, refresh 30 days.
- **The state table** `illuminate-mcp-state-<env>`: `pk` string, TTL `expires_at`, on-demand.
- **The function:**
  - Python 3.12 with the Lambda Web Adapter, as in the API;
  - 512 MB, 30 s timeout, reserved concurrency 10;
  - env `COGNITO_DOMAIN` (the prefix string), `STATE_TABLE`, `MCP_CLIENT_ID_PARAM`, and `API_URL` from SSM
    `/illuminate/<env>/api-url`.
- **The Function URL** has auth NONE and buffered responses.
- **No dependency cycle:** the client depends on the URL (its callback), so the function must not depend on the
  client. It reads the client ID from SSM at runtime (`ssm:GetParameter` on that one parameter), and no `PUBLIC_URL`
  is set.
- **SSM outputs:** `/illuminate/<env>/mcp-client-id` and `/illuminate/<env>/mcp-url` (`<fnUrl>mcp`).

**Tests:** a CDK assertion test checks:
- the client's flows and write attributes;
- the TTL;
- the SSM parameters;
- reserved concurrency;
- that no Fargate, ElastiCache or VPC resources exist.

Commit `feat: deploy the MCP as a Lambda Function URL`, then open the PR and merge it.

### Task 9: Deploy and live checks

Deploy the API, then `IlluminateMcp-dev`, then the API again (it picks up discovery at cold start; a redeploy forces
one), then the web app.

**Live checks:**
- **Protocol:** the metadata endpoints answer, and `/mcp` without a token is 401 with `WWW-Authenticate`.
- **MCP off:** as the Northbridge admin, PUT the setting on and off through the API. While off, a browser sign-in
  (Claude in Chrome through `/authorize`) shows the error page.
- **MCP on, through a real client:**
  - Sign in through the browser.
  - Then use JSON-RPC with the issued token: `initialize`, `tools/list`, `run_query`, then `run_sql` before and after a
    governed call, and `list_cards` and `run_card`.
  - The Northbridge viewer gets redacted shared items and is refused PII.
  - A Ridgeview token with a Northbridge session ID is refused.
- **Off again:** the existing token is refused within 60 s.
- **Clean up:** leave Northbridge MCP **on**, so Scott can try it, and Ridgeview off. Report the URL and the Claude
  Code command.
