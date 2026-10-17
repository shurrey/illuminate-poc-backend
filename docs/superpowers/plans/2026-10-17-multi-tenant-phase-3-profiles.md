# Multi-tenancy, Phase 3: User profiles. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan
> task-by-task.

**Goal:** each user's home-page cards, card layout, saved queries and preferences (favorites, recent reports, read
notifications, visible widgets) live on the server, keyed by tenant and user. The browser keeps none of them. A first
sign-in shows the 6 default cards. Items saved under the per-user browser keys are imported once.

**Architecture:**
- A DynamoDB table, `illuminate-user-items-{env}`:
  - partition key `tenant_id`;
  - sort key `item_key`: `user#{sub}#card#{id}`, `user#{sub}#layout`, `user#{sub}#query#{id}`, `user#{sub}#prefs`,
    `user#{sub}#imported`.
- `user_items_store.py` owns the table. Every read and write takes the tenant id and the sub from the verified token,
  never from the request.
- The default cards move to `semantic_layer/data/default_cards.json`, so the MCP (Phase 7) and the web app share them.
- New `/api/v1/me/*` routes serve the profile. The web app's three stores read and write through them.

**Spec:** `docs/superpowers/specs/2026-10-16-multi-tenant-and-mcp-design.md` section 4. Sharing (section 5) and SQL
saved queries (section 4a) are later phases. Keep the key layout able to hold them: `share#...` items, and a `kind`
field on queries.

## Global Constraints

- **Tenant and user** come only from the verified token: `tenant_for(user)` plus `user["sub"]`. A request body,
  path, header or query string never selects them. No cross-tenant visibility.
- **Every `/api/v1/me/*` route** calls `tenant_for` first. A not-ready tenant gets 403 `Your organization is not set
  up yet`. Each route is added to `NO_SECRET_ROUTES` in `tests/test_tenant_isolation.py`, with a
  `*_never_crosses` test.
- **Contracts** on cards and queries are validated as `QueryContract` on write (400 with `{"errors": [...]}` when
  invalid). They are returned as stored.
- **Limits:**
  - at most 100 custom cards and 500 saved queries per user;
  - names and labels ≤ 200 characters;
  - prompts and descriptions ≤ 4,000 characters;
  - favorites and read notifications ≤ 200 ids each;
  - recent reports ≤ 5;
  - visible widgets are limited to the known set.
  - Going over a limit is a 400 with a clear message.
- **CORS** allows GET, POST, PUT and DELETE only (no PATCH).
- **Concurrency:** layout and prefs writes use optimistic concurrency (`expected_version`, 409 on conflict), as the
  overlay store does.
- **Process:**
  - Tests never call AWS or Bedrock; use moto.
  - Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  - PR bodies start with `**Claude:**` and end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
  - Never commit `illuminate-poc/tsconfig.tsbuildinfo`.
- **Comments** only for non-obvious behaviour.

## Review Focus

1. **Same `sub` in two tenants:** profiles never cross. `TENANT_A` and `TENANT_B` with `sub` `u1` both exist in the
   isolation suite. Tested in Tasks 1 to 3.
2. **The import runs twice** (two tabs, or a retry): nothing is duplicated, and the second call is a no-op reporting
   `already_imported`. Tested in Task 3.
3. **A stale or invalid stored contract** (from an older browser) is skipped and reported on import, never a 500.
   Tested in Task 3.
4. **A new built-in card added to `default_cards.json` later** appears for existing users. A built-in the user hid
   stays hidden. Tested in Task 2.
5. **Session ends mid-request** (sign-out while a save is in flight): the previous user's response is discarded and
   never written into the next user's store. Tested in Task 4.

---

### Task 1: `user_items_store` and the table

**Files:**
- `user_items_store.py` (new);
- `cdk/lib/api/user-items-table.ts` (new);
- `cdk/lib/api/index.ts`;
- `cdk/lib/api/lambda-proxy.ts`: env var `USER_ITEMS_TABLE`; IAM `GetItem`, `PutItem`, `DeleteItem`, `Query` on the
  table; add the module to the bundle `cp` list;
- tests: `tests/test_user_items_store.py`.

**Interfaces** (all keyword `tenant_id: str, sub: str`):
- `get(tenant_id, sub, kind, id=None) -> dict | None`;
- `list_kind(tenant_id, sub, kind) -> list[dict]`. It paginates, uses `begins_with("user#{sub}#{kind}#")`, and uses
  ConsistentRead.
- `put(tenant_id, sub, kind, id, body: dict, *, expected_version: int | None = None) -> dict`:
  - it stores `body` plus `version` and `updated_at`;
  - `expected_version=None` means create-only (conditional on not existing);
  - an integer means update when the stored version matches;
  - `Conflict` on mismatch.
- `delete(tenant_id, sub, kind, id) -> bool`;
- `count(tenant_id, sub, kind) -> int`.
- `kind` is one of `card`, `layout`, `query`, `prefs` and `imported`. Singletons (`layout`, `prefs`, `imported`) take
  `id=None` and use the key `user#{sub}#{kind}`.
- `sub` and `id` are rejected if they contain `#`, and `id` must match `[A-Za-z0-9_-]{1,64}`.

**Tests:**
- round trips for each kind;
- the same sub in two tenants is isolated;
- `user#u1` never matches `user#u10` (the prefix ends with `#`);
- `#` in an id is refused;
- a version conflict is raised;
- pagination is covered;
- the CDK compiles with tsc, and `test_lambda_bundle` passes.

Commit `feat: per-user profile store`, then open the PR and merge it.

### Task 2: Cards

**Files:**
- `semantic_layer/data/default_cards.json` (new): the 6 cards from `illuminate-poc/src/data/dashboardCards.ts:51-87`,
  copied exactly (id, label, description, longDescription, prompt, contract, format, reportLink);
- `lambda_handler.py`;
- tests: `tests/test_me_cards.py` and isolation cases.

**Routes:**
- `GET /api/v1/me/cards` returns `{"cards": [card...], "version": int}`, in layout order. Each card has
  `isBuiltIn` and `enabled`.
  - It merges the built-ins with the user's custom cards and layout.
  - The layout is `{order: [ids], disabled: [ids], hidden: [built-in ids]}`.
  - With no layout, it returns every built-in, enabled, in file order.
  - Built-ins missing from `order` are appended (new defaults appear). `hidden` built-ins are omitted.
  - Unknown ids in `order` are ignored.
- `PUT /api/v1/me/cards/layout` takes `{"order", "disabled", "hidden", "expected_version"}` and returns `GET`'s
  shape. Ids must be built-in or the user's own; anything else is a 400.
- `POST /api/v1/me/cards` takes `{label, description, longDescription, prompt, contract, format, reportLink?}`.
  - It creates a custom card with a server-generated id (`c_` plus 12 hex characters) and appends it to the layout.
  - It returns the card.
  - It enforces the 100-card limit.
- `PUT /api/v1/me/cards/{id}` updates the user's own custom card. A built-in id is a 400; another user's id is a 404.
- `DELETE /api/v1/me/cards/{id}`:
  - a custom card is deleted and removed from the layout;
  - a built-in is added to `hidden`.
- `POST /api/v1/me/cards/reset`:
  - it clears `hidden` and `disabled` for built-ins and restores built-in order first;
  - custom cards are kept, after the built-ins.

**Tests:**
- a first user gets the 6 defaults;
- reorder, disable, hide and add;
- a new built-in appears for existing users, and a hidden one stays hidden;
- an invalid contract gives 400;
- the limits are enforced;
- isolation and the 403 for a not-ready tenant.

Commit `feat: home cards live in the user's profile`, then open the PR and merge it.

### Task 3: Saved queries, prefs and the one-time import

**Files:** `lambda_handler.py`; tests `tests/test_me_queries.py`, `tests/test_me_prefs.py`,
`tests/test_me_import.py` and isolation cases.

**Routes:**
- **Saved queries:**
  - `GET /api/v1/me/queries` returns `{"queries": [...]}`, newest first by `createdAt`.
  - `POST /api/v1/me/queries` takes `{name, prompt, contract, description}`.
    - It returns the query with a server id (`q_` plus 12 hex characters), `createdAt`, `lastUsedAt = null` and
      `kind: "governed"`.
    - It enforces the 500-query limit.
  - `PUT /api/v1/me/queries/{id}` updates name, description, prompt and contract.
  - `POST /api/v1/me/queries/{id}/used` sets `lastUsedAt`.
  - `DELETE /api/v1/me/queries/{id}`.
- **Prefs:**
  - `GET /api/v1/me/prefs` returns `{favorites, recentReports, readNotifications, visibleWidgets, version}`.
  - The defaults are `[]`, `[]`, `[]` and `["ai","kpis","feed","quickaccess"]`, with version 0.
  - `PUT /api/v1/me/prefs` takes the four lists plus `expected_version`. It validates the limits and the widget set.
- **Import:** `POST /api/v1/me/import` takes `{"cards": [card...], "queries": [query...], "prefs": {...}|null}`.
  - If `user#{sub}#imported` exists, it returns `{"status": "already_imported"}` and writes nothing.
  - Otherwise it does three things:
    - Imports custom cards (`isBuiltIn` false) as new custom cards, and applies `enabled: false` and order to the
      layout.
    - Imports queries, keeping their `createdAt` and `lastUsedAt` when they are valid ISO dates.
    - Imports prefs only when the user has none yet.
  - Invalid items (bad contract, too long, over a limit) are skipped. The response is
    `{"status": "imported", "cards": n, "queries": n, "skipped": [{"kind", "name", "reason"}]}`.
  - The marker is written last, with a create-only conditional. A concurrent second call sees it, or loses the
    conditional write and returns `already_imported` with no duplicates.
  - My call: items are created with deterministic ids, `hash(sub, source id)`, so a racing duplicate collides
    instead of doubling.

**Tests:**
- CRUD and limits;
- `lastUsedAt`;
- the prefs version conflict;
- the import counts and skipped items;
- an import twice is a no-op;
- a racing import produces no duplicates;
- isolation for every route, including the import.

Commit `feat: saved queries and preferences live in the user's profile`, then open the PR and merge it.

### Task 4 (web app): stores read and write the profile; import once

**Files (illuminate-poc):**
- a new `src/services/profileApi.ts` (following `adminApi.ts`'s `call` helper, with a `ProfileApiError`);
- `src/context/CardStoreContext.tsx`, `QueryBuilderContext.tsx`, `UserContext.tsx`;
- a new `src/lib/profileImport.ts`, with `collectLocalProfile(sub)` and `clearLocalProfile(sub)`;
- `src/data/dashboardCards.ts`: types only, plus a fallback copy used only while loading;
- the `/me`-failure banner in `src/context/AuthContext.tsx` and the layout;
- tests: `src/lib/profileImport.test.ts` and `src/lib/profileStore.test.ts` (pure reducers).

**Behaviour:**
- **Loading.** Each store loads from the API after sign-in, and exposes `loading` and `error`. Its methods keep their
  current names and signatures where they exist (`addCard`, `toggleCard`, `moveCard`, `removeCard`,
  `resetToDefaults`, `saveQuery`, `deleteQuery`, `toggleFavorite`, `addRecentReport`, `setVisibleWidgets`,
  `markNotificationRead`, `markAllNotificationsRead`).
- **Updates.** Updates are optimistic, roll back on error, and show the error through the existing toast or inline
  pattern.
  - A 409 reloads and retries once.
  - Every async result is guarded by `sessionCheck()`, and stores reset on `onSessionEnd`.
- **Import.** After the first successful load, if the per-user browser keys `illuminate-dashboard-cards-v2:<sub>` or
  `illuminate-queries-v2:<sub>` hold items, they are posted once to `/api/v1/me/import`.
  - On `imported` or `already_imported`, those keys are removed.
  - `illuminate-user` is posted as prefs, then removed.
  - The bare keys `illuminate-dashboard-cards-v2` and `illuminate-queries-v2` are deleted without importing. Ruling:
    their owner is unknown, and importing them could move one person's items into another tenant.
- **Storage.** The stores stop writing to localStorage. On sign-out, every `illuminate-` key except `illuminate_auth`
  handling is removed.
- **Banner.** When `/api/v1/me` fails, a banner says "We couldn't load your organization's settings. Some features may
  be unavailable." It carries over from Phase 2.

**Tests:**
- `collectLocalProfile` reads only the `:<sub>` keys and `illuminate-user`, and never the bare keys;
- `clearLocalProfile` removes them;
- the reducers for optimistic updates and rollback;
- a stale-session result is ignored.

Run `npm test` and `npx tsc --noEmit`. Commit `feat: cards, saved queries and preferences are saved to your profile`,
then open the PR and merge it.

### Task 5: Deploy and live checks

- **Deploy:** `IlluminateApi-dev --exclusively`, then the web app.
- **Checks, as the Northbridge admin:**
  - `GET /api/v1/me/cards` returns the 6 defaults;
  - add a custom card, reorder and hide;
  - save a query, set prefs;
  - import twice and get `already_imported`.
- **Isolation check:** Ridgeview admin sees none of Northbridge's items.
- **Clean up** the live-check items afterwards.
