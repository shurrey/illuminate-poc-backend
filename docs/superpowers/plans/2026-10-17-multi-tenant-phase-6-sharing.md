# Multi-tenancy, Phase 6: Sharing. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan
> task-by-task.

**Goal:** A user can share a saved query or a custom home card with specific people in their tenant, with the whole
tenant, or both.
- **Live link:** recipients always see the owner's current version. They can run it, and they can make their own
  copy to edit.
- **Instant revocation:** unsharing or deleting removes access at once.
- **Runner's rights:** a shared item always runs as the viewer, so the viewer's role and licence apply.
- **Home page:** a recipient can pin a shared card to their home page as a live card.

**Spec:** `docs/superpowers/specs/2026-10-16-multi-tenant-and-mcp-design.md`, section 5, plus the Phase 6
prerequisites recorded in the Phase 5 final review.

**Rulings (mine):**
1. **Sharing scope.** Only saved queries and custom cards are shareable. Conversations and built-in cards are not.
   Stored chat text therefore never reaches another user, which resolves the "Admin-era chat text" prerequisite.
2. **Opaque share ids.** Each shared item gets a `share_id` (`sh_` plus 16 random hex). Routes address shares by it,
   so owner subs and item ids aren't exposed to recipients.
3. **Redaction for recipients without identity rights.**
   - When a Viewer (`allow_identity` false) views a shared item, every string literal in the SQL text is replaced
     with `'…'`.
   - Every contract filter value is replaced with `"…"`.
   - Running it is unaffected: the run uses the stored values, and the guard applies the runner's rules to the
     result.
   - Names and descriptions are the owner's own words. They are shown labelled as the owner's, under "Owner's note",
     and are not redacted.
4. **Owner name** is shown from the tenant user directory at read time (display name, or email if the directory has
   no name). It is never taken from the saved row.
5. **The share recipient picker** searches a per-tenant user directory. Directory entries are written on every
   `/api/v1/me` call and by the tenant script, because Cognito can't search by a custom attribute.

## Global Constraints

- **No cross-tenant anything.**
  - Shares, directory entries and share pointers all live in the owner's tenant partition.
  - Every read is scoped by the caller's token tenant.
  - Another tenant's `share_id` is a 404.
- **Viewers never see PII.** Runs use the runner's role. Redaction applies to the views of recipients without
  identity rights.
- **Membership is checked on every read and run.** There is no caching of share membership, so unsharing works
  immediately.
- **Running a shared item never writes the owner's data** (`lastUsedAt`, layout).
- **A copy is saved through the recipient's own create path**, validated as the recipient. A refusal is shown with
  its reason.
- **Process:**
  - Tests never call AWS or Bedrock.
  - Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  - PR bodies start with `**Claude:**` and end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
  - Push with `git -c credential.helper= -c 'credential.helper=!gh auth git-credential' push`.
  - Never commit `illuminate-poc/tsconfig.tsbuildinfo`.
  - Comments only for non-obvious behaviour.

## Review Focus

1. **A recipient in tenant B with the same `sub` as a tenant A recipient** never sees A's shares. Tested in Tasks 2–3.
2. **The owner unshares or deletes while a recipient has the item open:** the next run is a 404. A card pinned to the
   recipient's home disappears quietly. Tested in Tasks 3–4.
3. **A Viewer recipient of an Admin's email query:**
   - the view hides the literals;
   - running it is refused by the guard (PII);
   - copying it is refused with the reason.

   Tested in Task 3.
4. **The owner edits the item:** the recipient's next view and run use the new version. Tested in Task 3.
5. **The directory search** never returns users from another tenant, or users with no tenant. Tested in Task 1.

---

### Task 1: Tenant user directory

**Files:** `user_items_store.py` (new kind `directory`, a singleton per sub); `lambda_handler.py`; `tenant_admin.py`
(write directory entries on create, add-users and migrate); tests `tests/test_directory.py` and isolation cases.

**Interfaces:**
- **Store:** `user_items_store` gains kind `directory`, with item key `directory#{sub}` and body `{sub, name, email}`.
  It also gains `list_directory(tenant_id) -> list[dict]`, which paginates over `begins_with("directory#")`.
- **Upsert on `/api/v1/me`:** each call upserts the caller's entry, from the token's `name` and `email` claims.
  - The write is best-effort: a failure is logged and never fails `/me`.
  - It only writes when the stored entry differs.
- **`GET /api/v1/directory?q=`:**
  - It matches `q` against name and email, case-insensitive substring; an empty `q` lists everyone.
  - It returns at most 25 entries, `{sub, name, email}`, sorted by name, and excludes the caller.
  - It is tenant-scoped through `tenant_for`.
- **The tenant script** writes an entry for each user it creates or moves. The `name` comes from the email local part
  when the user has no name.

**Tests:**
- upsert on `/me`;
- search and its limit;
- excludes self;
- the isolation suite's same `sub` in two tenants;
- the script writes entries;
- a store failure doesn't break `/me`.

Commit `feat: a per-tenant user directory for sharing`, then open the PR and merge it.

### Task 2: Share records

**Files:** `user_items_store.py` (kinds `share`, keyed `share#{owner_sub}#{item_kind}#{item_id}`, and `shareid`,
keyed `shareid#{share_id}`); `lambda_handler.py`; tests `tests/test_sharing.py` and isolation cases.

**Interfaces:**
- **Data:**
  - Share body: `{share_id, owner_sub, item_kind: "query"|"card", item_id, recipients: [sub], tenant: bool,
    shared_at, updated_at}`.
  - Pointer body: `{owner_sub, item_kind, item_id}`.
  - The pair is written in a single DynamoDB transaction, or in a fixed order with the pointer first, so reads never
    see a share without its pointer. Implementer's call; say which.
- **Share and unshare routes:**
  - `PUT /api/v1/me/queries/{id}/share` and `PUT /api/v1/me/cards/{id}/share` take `{recipients: [sub], tenant:
    bool}`. Only the owner can call them.
    - The item must exist and must be a custom card (built-ins can't be shared).
    - Every recipient must be in the tenant directory and can't be the owner. At most 200 recipients.
    - `{recipients: [], tenant: false}` unshares.
    - Re-sharing keeps the same `share_id`.
    - The response is `{share_id, recipients: [{sub, name}], tenant}`.
  - `DELETE` on the same paths unshares.
  - `GET` on the same paths returns the current share, for the owner's dialog.
- **Deletion cascade:** deleting a query or custom card also deletes its share and its pointer.
- **List responses:** the owner's lists (`/me/queries`, `/me/cards`) add `shared: {tenant, count}` per item.

**Tests:**
- share, re-share and unshare;
- a recipient from another tenant is refused;
- a built-in card is refused;
- the delete cascade;
- isolation.

Commit `feat: queries and cards can be shared within the tenant`, then open the PR and merge it.

### Task 3: Shared with me — list, view, run, copy

**Files:** `lambda_handler.py`; a new `semantic_layer/redact.py`; tests `tests/test_shared_with_me.py` and isolation
cases.

**Routes:**
- **`GET /api/v1/shared`** returns items shared with the caller.
  - It scans the caller's tenant partition for `share#` items where `tenant` is true or the caller is a recipient.
    The caller's own items are excluded.
  - It resolves each live item from the owner's rows and returns `[{share_id, item_kind, name|label, owners_note:
    description, owner: {name}, query_kind?, governance_hint?, format?, shared_at}]`.
  - An item that no longer exists is skipped.
- **`GET /api/v1/shared/{share_id}`** returns the full live item for viewing.
  - Redaction applies for callers without identity rights. `redact_sql(sql)` replaces string literals using sqlglot;
    if parsing fails, the SQL text is withheld. `redact_contract(contract)` replaces filter values.
  - The governed parts' filter values are redacted too.
- **`POST /api/v1/shared/{share_id}/run`** runs the live item as the caller.
  - It checks membership on every call.
  - For a query, it goes through the same code as `/me/queries/{id}/run`, parameterised by whose row to read. The run
    reason is `Owner's note: <description>`, or `Shared by <owner name>`.
  - For a card, it runs the contract as the caller does on `/semantic/query`.
  - It never writes the owner's `lastUsedAt`.
- **`POST /api/v1/shared/{share_id}/copy`** saves a copy into the caller's profile through the normal create path,
  validated as the caller.
  - The copy uses the unredacted stored values.
  - A refusal returns 400 with the reason.

  Ruling: a Viewer copying an email query is refused by the guard at save. A copy of a governed query whose filter
  values contain PII is allowed, because filter values never return identity.
- **Any non-member, a wrong tenant, or an unknown `share_id`** is a 404. There's no distinction, so no existence
  oracle.

**Tests:**
- list for a named recipient and for the tenant-wide case;
- the owner's own items aren't listed;
- view redaction for a Viewer, with none for an Admin;
- a run as the runner: a Viewer is refused PII, an Admin succeeds;
- the owner's edit is reflected;
- unshare → 404 on the next run;
- copy as the recipient, plus a copy refused for a Viewer;
- the owner's `lastUsedAt` is untouched;
- isolation, including the same `sub` in another tenant.

Commit `feat: people can open, run and copy what is shared with them`, then open the PR and merge it.

### Task 4: Shared cards on the home page

**Files:** `lambda_handler.py` (the cards view and layout); tests `tests/test_me_cards.py` and isolation cases.

**Changes:**
- **Layout:** `order` may contain `sh_<share_id>` entries for shared cards.
  - `PUT /me/cards/layout` accepts them only when the caller is currently a member of a card share.
  - `POST /api/v1/me/cards/pin-shared {share_id}` appends one, and the layout's `DELETE` path removes it.
- **`GET /me/cards`** resolves each pinned share live:
  - a still-shared card appears as `{...card, id: "sh_…", shared: true, owner: {name}, isBuiltIn: false, enabled}`;
  - a share that is gone is omitted, without an error.
- **Running a pinned shared card** goes through `/shared/{share_id}/run`. The frontend passes the share id.
- **Limits:** at most 50 pinned shared cards.

**Tests:**
- pin, view and run;
- unshare → the card disappears;
- the owner's edit is reflected;
- a non-member can't pin;
- isolation.

Commit `feat: shared cards can sit on your home page`, then open the PR and merge it.

### Task 5 (web app): Share dialog, Shared with me, shared cards

**Files (illuminate-poc):**
- `src/services/sharingApi.ts` (new);
- `src/components/sharing/ShareDialog.tsx` (new);
- `src/components/queries/MyQueries.tsx` (share button, "Shared with me" section);
- `src/components/CustomizePanel.tsx` (share a custom card, list shared cards, "Add to my home");
- `src/hooks/useDashboardCards.ts` and the KPI card (run shared cards through the shared run endpoint, with an owner
  badge);
- a shared-item view (read-only, "Make a copy");
- pure helpers in `src/lib/sharing.ts` with tests.

**Behaviour:**
- **Share dialog:**
  - a people picker searching `/api/v1/directory`, showing chips;
  - a toggle "Everyone at <tenant name>";
  - current recipients loaded from `GET …/share`;
  - Save and Stop sharing.
- **Shared-with-me lists** show the owner's name, the "Owner's note", and the governance hint. Run shows labelled
  results, and Make a copy shows the server's refusal inline.
- **Shared cards on home** show a "Shared by <name>" badge. A card that is no longer shared disappears after the next
  load.
- **New state** follows the session-reset patterns: `sessionCheck`, and the remount per session.

**Checks:** run `npm test`, `npx tsc --noEmit` and `npm run build`.

Commit `feat: share queries and cards with people in your organization`, then open the PR and merge it.

### Task 6: Deploy and live checks

- **Deploy** the API, then the base stack (if changed), then the web app.
- **Add a second Northbridge user:** `scripts/tenant.py add-users c2da348c-… --users viewer@northbridge.example.edu
  (Viewer) --no-email`. Use the same temp-password file approach as Ridgeview, saved to
  `$S/northbridge-viewer-password.txt` (mode 600) and made permanent.
- **Live checks:**
  - **Northbridge admin:**
    - shares a governed query and an email SQL query with the Northbridge viewer;
    - shares a custom card tenant-wide.
  - **Northbridge viewer:**
    - sees all three;
    - the email query's view is redacted;
    - its run is refused;
    - the governed query runs;
    - the card pins and runs;
    - copying the email query is refused.
  - **Owner unshares:** the viewer gets a 404 on the next run.
  - **Ridgeview admin:** sees nothing in `/shared`, and Northbridge `share_id`s are 404.
- **Clean up** the test items afterwards.
