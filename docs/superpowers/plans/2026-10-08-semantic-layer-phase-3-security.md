# Semantic Layer Phase 3 (Security Hardening) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the security defects found during the review: user-writable tenant IDs, missing admin authorization, conversation and cancel ownership, unused infrastructure, IAM over-grant, and an over-eager PII scrubber.

**Architecture:** The backend changes are FastAPI route checks plus a DynamoDB-conditioned conversation store. Infrastructure changes are CDK edits verified by assertions over the synthesized templates. The frontend reads admin membership from the same ID-token claim the API enforces.

**Tech Stack:** Python 3.11, FastAPI, boto3 / moto, AWS CDK (TypeScript), Next.js 16.

**Spec:** `docs/superpowers/specs/2026-10-08-semantic-layer-bbd-parity-design.md` §8 Phase 3. Roadmap: Phase 3.

## Global Constraints

- Backend units (3a–3g) are in `illuminate-conversational-intelligence`. 3h is in `illuminate-poc`.
- One unit = one PR from the latest `origin/main`. The executor squash-merges after verification (owner's instruction).
- `$PY` is a Python 3.11 interpreter with `requirements-lambda.txt` and `requirements-dev.txt` installed.
- CDK checks use `--exclusively IlluminateBase-dev`, which skips Docker asset bundling; it still writes both stack templates. `initialUserPassword` is a synth-only placeholder so the initial-user resources render.
- Nothing in this phase deploys. The PR descriptions carry the deploy notes.
- Never commit `illuminate-poc/tsconfig.tsbuildinfo`: it carries the owner's local changes.

## Review Focus

1. A user with a tenant but no admin group calling any `/admin/*` route gets 403, never a partial write (3b `test_admin_routes_reject_non_admins`).
2. A request with no `Authorization` header gets 401 on every token-taking route (3b `test_missing_authorization_header_is_401`; the syntax-tree check in the 3b PR text).
3. Two users who use the same `context_id` never see or overwrite each other's turns (3c `test_another_user_cannot_append_to_or_overwrite_a_conversation`).
4. A cancel from a different user, or for an unknown request, changes nothing (3d `test_cannot_cancel_someone_elses_request`, `test_unknown_request_is_404`).
5. A 9- or 10-digit count in an answer survives the scrubber (3g `test_plain_large_numbers_survive`).

---

### Task 1 (PR 3a): users can no longer write custom:tenant_id

**Repo:** `illuminate-conversational-intelligence`

**Files:** `cdk/test/check_template.py`, `cdk/lib/base/auth.ts`

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b feat/tenant-attribute-admin-only origin/main
```

- [ ] **Step 2: Write the failing checks**

```diff
diff --git a/cdk/test/check_template.py b/cdk/test/check_template.py
new file mode 100644
index 0000000..49efe06
--- /dev/null
+++ b/cdk/test/check_template.py
@@ -0,0 +1,35 @@
+"""Assertions over the synthesized CloudFormation templates.
+
+Run after `npx cdk synth -q -c environment=dev --exclusively IlluminateBase-dev`
+(--exclusively skips Lambda asset bundling, which needs Docker).
+"""
+
+import json
+import sys
+from pathlib import Path
+
+OUT = Path(__file__).resolve().parent.parent / "cdk.out"
+
+
+def resources(stack: str, kind: str) -> list[dict]:
+    template = json.loads((OUT / f"{stack}.template.json").read_text())
+    return [r for r in template["Resources"].values() if r["Type"] == kind]
+
+
+def check(name: str, ok: bool) -> bool:
+    print(("PASS " if ok else "FAIL ") + name)
+    return ok
+
+
+def main() -> int:
+    [client] = resources("IlluminateBase-dev", "AWS::Cognito::UserPoolClient")
+    writable = client["Properties"].get("WriteAttributes")
+    results = [
+        check("user pool client declares its writable attributes", writable is not None),
+        check("users cannot write custom:tenant_id", writable is not None and "custom:tenant_id" not in writable),
+    ]
+    return 0 if all(results) else 1
+
+
+if __name__ == "__main__":
+    sys.exit(main())
```

- [ ] **Step 3: Run them to verify they fail**

Run: `(cd cdk && npx cdk synth -q -c environment=dev -c initialUserPassword=Synth-Only-Passw0rd! --exclusively IlluminateBase-dev > /dev/null && $PY test/check_template.py)`
Expected: `FAIL user pool client declares its writable attributes` and `FAIL users cannot write custom:tenant_id`.

- [ ] **Step 4: Implement**

```diff
diff --git a/cdk/lib/base/auth.ts b/cdk/lib/base/auth.ts
index 4a9c92f..dc039ed 100644
--- a/cdk/lib/base/auth.ts
+++ b/cdk/lib/base/auth.ts
@@ -60,6 +60,9 @@ export class Auth extends Construct {
         userPassword: true,
         userSrp: true,
       },
+      // Without an explicit list every attribute is user-writable, which would let a user
+      // re-tenant themselves via UpdateUserAttributes. custom:tenant_id is admin-only.
+      writeAttributes: new cognito.ClientAttributes().withStandardAttributes({ fullname: true }),
     });
 
     // Create initial admin user on first deploy (idempotent — ignores if user exists)
```

- [ ] **Step 5: Verify**

Run: `(cd cdk && npx cdk synth -q -c environment=dev -c initialUserPassword=Synth-Only-Passw0rd! --exclusively IlluminateBase-dev > /dev/null && $PY test/check_template.py)`
Expected: all `PASS`, exit 0.

- [ ] **Step 6: Verify**

Run: `cd cdk && npx tsc --noEmit`
Expected: no output.

- [ ] **Step 7: Commit, open the PR, merge**

```bash
git add cdk/test/check_template.py cdk/lib/base/auth.ts
git commit -m "fix: users can no longer write custom:tenant_id

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin feat/tenant-attribute-admin-only
gh pr create --base main --title "fix: users can no longer write custom:tenant_id" --body "$(cat <<'EOF'
**Claude:** Phase 3a. Without an explicit `writeAttributes` list, a Cognito app client lets users write every mutable attribute. So any user could call `UpdateUserAttributes` and set their own `custom:tenant_id`, then read another tenant's overlays. The client now allows users to write only `name`. `custom:tenant_id` stays mutable through admin APIs. Adds `cdk/test/check_template.py`, which checks the synthesized template; the stack has no CDK test framework. Deploy note: this updates the existing app client in place. Nothing is deployed by this PR.

Reviewable surface: 2 files, 38 lines.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge feat/tenant-attribute-admin-only --squash --delete-branch
```

### Task 2 (PR 3b): illuminate-admins group required for /admin endpoints; missing token is 401

**Repo:** `illuminate-conversational-intelligence`

**Files:** `tests/test_admin_authz.py`, `cdk/test/check_template.py`, `lambda_handler.py`, `cdk/lib/base/auth.ts`

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b feat/admin-group origin/main
```

- [ ] **Step 2: Write the failing checks**

```diff
diff --git a/cdk/test/check_template.py b/cdk/test/check_template.py
index 49efe06..17943e0 100644
--- a/cdk/test/check_template.py
+++ b/cdk/test/check_template.py
@@ -1,7 +1,9 @@
 """Assertions over the synthesized CloudFormation templates.
 
-Run after `npx cdk synth -q -c environment=dev --exclusively IlluminateBase-dev`
-(--exclusively skips Lambda asset bundling, which needs Docker).
+Run from cdk/ after:
+  npx cdk synth -q -c environment=dev -c initialUserPassword=Synth-Only-Passw0rd! --exclusively IlluminateBase-dev
+The password is a placeholder so the initial-user resources synthesize; --exclusively skips
+Lambda asset bundling, which needs Docker.
 """
 
 import json
@@ -24,9 +26,13 @@ def check(name: str, ok: bool) -> bool:
 def main() -> int:
     [client] = resources("IlluminateBase-dev", "AWS::Cognito::UserPoolClient")
     writable = client["Properties"].get("WriteAttributes")
+    groups = [g["Properties"]["GroupName"] for g in resources("IlluminateBase-dev", "AWS::Cognito::UserPoolGroup")]
+    base_text = (OUT / "IlluminateBase-dev.template.json").read_text()
     results = [
         check("user pool client declares its writable attributes", writable is not None),
         check("users cannot write custom:tenant_id", writable is not None and "custom:tenant_id" not in writable),
+        check("illuminate-admins group exists", "illuminate-admins" in groups),
+        check("initial user is added to the admin group", "adminAddUserToGroup" in base_text),
     ]
     return 0 if all(results) else 1
 
diff --git a/tests/test_admin_authz.py b/tests/test_admin_authz.py
new file mode 100644
index 0000000..8abe598
--- /dev/null
+++ b/tests/test_admin_authz.py
@@ -0,0 +1,63 @@
+import pytest
+from fastapi.testclient import TestClient
+
+import lambda_handler
+import tenant_store
+from semantic_layer.models import Glossary, Tenant
+
+AUTH = {"Authorization": "Bearer test"}
+ADMIN = {"sub": "a1", "custom:tenant_id": "t1", "cognito:groups": ["illuminate-admins"]}
+MEMBER = {"sub": "u1", "custom:tenant_id": "t1"}
+
+ADMIN_ROUTES = [
+    ("get", "/api/v1/admin/metrics", None),
+    ("get", "/api/v1/admin/overlay/metric.student_count.v1", None),
+    ("put", "/api/v1/admin/overlay/metric.student_count.v1",
+     {"measure_sql": "SELECT 1", "diff_description": "x"}),
+    ("delete", "/api/v1/admin/overlay/metric.student_count.v1", None),
+]
+
+
+@pytest.fixture
+def client(monkeypatch):
+    monkeypatch.setattr(tenant_store, "load_tenant",
+                        lambda tid: Tenant(id=tid, display_name=tid, overlays={}, glossary=Glossary(synonyms={})))
+    monkeypatch.setattr(tenant_store, "get_overlay", lambda tid, mid: None)
+    monkeypatch.setattr(tenant_store, "put_overlay", lambda *a, **k: None)
+    monkeypatch.setattr(tenant_store, "delete_overlay", lambda tid, mid: None)
+    return TestClient(lambda_handler.app)
+
+
+def _as(monkeypatch, user):
+    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user if a else None)
+
+
+@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES)
+def test_admin_routes_reject_non_admins(client, monkeypatch, method, path, body):
+    _as(monkeypatch, MEMBER)
+    r = client.request(method, path, headers=AUTH, json=body)
+    assert r.status_code == 403
+    assert "illuminate-admins" in r.json()["detail"]
+
+
+def test_admin_can_list_metrics(client, monkeypatch):
+    _as(monkeypatch, ADMIN)
+    r = client.get("/api/v1/admin/metrics", headers=AUTH)
+    assert r.status_code == 200
+    assert r.json()["tenant_id"] == "t1"
+
+
+def test_admin_without_tenant_is_still_rejected(client, monkeypatch):
+    _as(monkeypatch, {"sub": "a1", "cognito:groups": ["illuminate-admins"]})
+    assert client.get("/api/v1/admin/metrics", headers=AUTH).status_code == 403
+
+
+@pytest.mark.parametrize("method,path", [
+    ("get", "/api/v1/admin/metrics"),
+    ("post", "/api/v1/semantic/compile"),
+    ("get", "/api/v1/semantic/catalog"),
+    ("get", "/api/conversations/abc"),
+])
+def test_missing_authorization_header_is_401(client, method, path):
+    r = client.request(method, path, json={"metrics": ["metric.reportable_courses.v1"]})
+    assert r.status_code == 401
```

- [ ] **Step 3: Run them to verify they fail**

Run: `$PY -m pytest -q tests/test_admin_authz.py; (cd cdk && npx cdk synth -q -c environment=dev -c initialUserPassword=Synth-Only-Passw0rd! --exclusively IlluminateBase-dev > /dev/null && $PY test/check_template.py)`
Expected: 8 test failures: non-admins get 200, and a missing header gets 422. Template: `FAIL illuminate-admins group exists`, `FAIL initial user is added to the admin group`.

- [ ] **Step 4: Implement**

```diff
diff --git a/cdk/lib/base/auth.ts b/cdk/lib/base/auth.ts
index dc039ed..b898b5c 100644
--- a/cdk/lib/base/auth.ts
+++ b/cdk/lib/base/auth.ts
@@ -18,6 +18,9 @@ export interface AuthProps {
   initialTenantId?: string;
 }
 
+/** Members may edit their tenant's metric overlays; the API reads it from the cognito:groups claim. */
+export const ADMIN_GROUP_NAME = 'illuminate-admins';
+
 export class Auth extends Construct {
   public readonly userPool: cognito.UserPool;
   public readonly userPoolClient: cognito.UserPoolClient;
@@ -48,6 +51,12 @@ export class Auth extends Construct {
       },
     });
 
+    const adminGroup = new cognito.CfnUserPoolGroup(this, 'AdminGroup', {
+      userPoolId: this.userPool.userPoolId,
+      groupName: ADMIN_GROUP_NAME,
+      description: 'Can edit tenant metric overlays',
+    });
+
     // Set UserPoolTier to LITE (not exposed in L2 construct)
     const cfnUserPool = this.userPool.node.defaultChild as cognito.CfnUserPool;
     cfnUserPool.addPropertyOverride('UserPoolTier', 'LITE');
@@ -156,6 +165,27 @@ export class Auth extends Construct {
       });
 
       setPassword.node.addDependency(createUser);
+
+      const addToAdmins = new cr.AwsCustomResource(this, 'InitialUserAdminGroup', {
+        onCreate: {
+          service: 'CognitoIdentityServiceProvider',
+          action: 'adminAddUserToGroup',
+          parameters: {
+            UserPoolId: this.userPool.userPoolId,
+            Username: props.initialUserEmail,
+            GroupName: ADMIN_GROUP_NAME,
+          },
+          physicalResourceId: cr.PhysicalResourceId.of(`initial-user-admin-${props.initialUserEmail}`),
+        },
+        policy: cr.AwsCustomResourcePolicy.fromStatements([
+          new iam.PolicyStatement({
+            actions: ['cognito-idp:AdminAddUserToGroup'],
+            resources: [this.userPool.userPoolArn],
+          }),
+        ]),
+      });
+      addToAdmins.node.addDependency(createUser);
+      addToAdmins.node.addDependency(adminGroup);
     }
   }
 }
diff --git a/lambda_handler.py b/lambda_handler.py
index f98da37..f64d568 100644
--- a/lambda_handler.py
+++ b/lambda_handler.py
@@ -455,7 +455,7 @@ async def health_check():
 @app.post("/api/chat", response_model=ChatResponse)
 async def chat(
     request: ChatRequest,
-    authorization: str = Header(...)
+    authorization: Optional[str] = Header(None)
 ):
     """Send a message via chat_engine (non-streaming)."""
     user = _get_user_from_token(authorization)
@@ -505,7 +505,7 @@ async def chat(
 @app.post("/api/chat/stream")
 async def chat_stream(
     request: ChatRequest,
-    authorization: str = Header(...)
+    authorization: Optional[str] = Header(None)
 ):
     """
     Send a message and receive streaming response via Server-Sent Events.
@@ -587,7 +587,7 @@ async def cancel_chat(
 @app.get("/api/conversations/{context_id}")
 async def get_conversation(
     context_id: str,
-    authorization: str = Header(...)
+    authorization: Optional[str] = Header(None)
 ):
     """Get conversation history by context ID."""
     user = _get_user_from_token(authorization)
@@ -603,7 +603,7 @@ async def get_conversation(
 @app.delete("/api/conversations/{context_id}")
 async def clear_conversation(
     context_id: str,
-    authorization: str = Header(...)
+    authorization: Optional[str] = Header(None)
 ):
     """Clear a conversation context."""
     user = _get_user_from_token(authorization)
@@ -650,7 +650,7 @@ async def _proxy_dictionary_request(path: str) -> object:
 
 
 @app.get("/api/v1/dictionary/submodels")
-async def dictionary_submodels(authorization: str = Header(...)):
+async def dictionary_submodels(authorization: Optional[str] = Header(None)):
     """Returns all CDM domains with display names."""
     user = _get_user_from_token(authorization)
     if not user:
@@ -659,7 +659,7 @@ async def dictionary_submodels(authorization: str = Header(...)):
 
 
 @app.get("/api/v1/dictionary/definitions")
-async def dictionary_definitions(authorization: str = Header(...)):
+async def dictionary_definitions(authorization: Optional[str] = Header(None)):
     """Returns all column definitions."""
     user = _get_user_from_token(authorization)
     if not user:
@@ -668,7 +668,7 @@ async def dictionary_definitions(authorization: str = Header(...)):
 
 
 @app.get("/api/v1/dictionary/erd")
-async def dictionary_erd(authorization: str = Header(...)):
+async def dictionary_erd(authorization: Optional[str] = Header(None)):
     """Returns entity relationships (foreign keys)."""
     user = _get_user_from_token(authorization)
     if not user:
@@ -681,7 +681,7 @@ async def dictionary_preview(
     schema: str,
     table: str,
     limit: int = 20,
-    authorization: str = Header(...),
+    authorization: Optional[str] = Header(None),
 ):
     """Preview sample data from a Snowflake table.
 
@@ -743,7 +743,7 @@ class DashboardMetricRequest(BaseModel):
 @app.post("/api/v1/dashboard/query")
 async def dashboard_query(
     request: DashboardQueryRequest,
-    authorization: str = Header(...),
+    authorization: Optional[str] = Header(None),
 ):
     """Execute a read-only SQL query against Snowflake for dashboard widgets.
 
@@ -778,7 +778,7 @@ async def dashboard_query(
 @app.post("/api/v1/dashboard/metric")
 async def dashboard_metric(
     request: DashboardMetricRequest,
-    authorization: str = Header(...),
+    authorization: Optional[str] = Header(None),
 ):
     """Execute a canonical metric for a dashboard widget.
 
@@ -879,14 +879,14 @@ def _compile_contract(contract: QueryContract, authorization: str):
 
 
 @app.post("/api/v1/semantic/compile")
-async def semantic_compile(contract: QueryContract, authorization: str = Header(...)) -> dict:
+async def semantic_compile(contract: QueryContract, authorization: Optional[str] = Header(None)) -> dict:
     """Compile a semantic query contract to SQL without executing it."""
     return _compile_contract(contract, authorization).model_dump()
 
 
 @app.get("/api/v1/semantic/catalog")
 async def semantic_catalog(
-    authorization: str = Header(...),
+    authorization: Optional[str] = Header(None),
     if_none_match: Optional[str] = Header(default=None),
 ):
     """Public datasets, dimensions, measures and metrics; supports If-None-Match."""
@@ -908,7 +908,7 @@ async def semantic_catalog(
 
 
 @app.post("/api/v1/semantic/query")
-async def semantic_query(contract: QueryContract, authorization: str = Header(...)):
+async def semantic_query(contract: QueryContract, authorization: Optional[str] = Header(None)):
     """Compile a semantic query contract and run it through the execution guard."""
     compiled = _compile_contract(contract, authorization)
 
@@ -946,8 +946,13 @@ class OverlayPutRequest(BaseModel):
     last_reviewed: Optional[str] = None  # ISO date; defaults to today server-side
 
 
-def _require_tenant(user: Optional[dict]) -> str:
-    """Extract tenant_id or raise 403 — admin endpoints require it."""
+ADMIN_GROUP = "illuminate-admins"
+
+
+def _require_admin_tenant(user: Optional[dict]) -> str:
+    """The caller's tenant_id; 403 unless they are in the admin group and carry a tenant."""
+    if ADMIN_GROUP not in (user or {}).get("cognito:groups", []):
+        raise HTTPException(status_code=403, detail=f"Requires membership of the {ADMIN_GROUP} group.")
     tid = _tenant_id_from_user(user)
     if not tid:
         raise HTTPException(
@@ -961,7 +966,7 @@ def _require_tenant(user: Optional[dict]) -> str:
 
 
 @app.get("/api/v1/admin/metrics")
-async def admin_list_metrics(authorization: str = Header(...)) -> dict:
+async def admin_list_metrics(authorization: Optional[str] = Header(None)) -> dict:
     """List all canonical metrics + this tenant's current overlay state.
 
     Each entry: id, display_name, description, owner (canonical), entity,
@@ -971,7 +976,7 @@ async def admin_list_metrics(authorization: str = Header(...)) -> dict:
     user = _get_user_from_token(authorization)
     if not user:
         raise HTTPException(status_code=401, detail="Invalid or expired token")
-    tenant_id = _require_tenant(user)
+    tenant_id = _require_admin_tenant(user)
 
     from semantic_layer.engine import load_canonical
     import tenant_store
@@ -1006,13 +1011,13 @@ async def admin_list_metrics(authorization: str = Header(...)) -> dict:
 @app.get("/api/v1/admin/overlay/{metric_id}")
 async def admin_get_overlay(
     metric_id: str,
-    authorization: str = Header(...),
+    authorization: Optional[str] = Header(None),
 ) -> dict:
     """Return the current overlay for one metric, or null if none exists."""
     user = _get_user_from_token(authorization)
     if not user:
         raise HTTPException(status_code=401, detail="Invalid or expired token")
-    tenant_id = _require_tenant(user)
+    tenant_id = _require_admin_tenant(user)
 
     from semantic_layer.engine import load_canonical
     import tenant_store
@@ -1039,7 +1044,7 @@ async def admin_get_overlay(
 async def admin_put_overlay(
     metric_id: str,
     request: OverlayPutRequest,
-    authorization: str = Header(...),
+    authorization: Optional[str] = Header(None),
 ) -> dict:
     """Create or update this tenant's overlay for one metric.
 
@@ -1050,7 +1055,7 @@ async def admin_put_overlay(
     user = _get_user_from_token(authorization)
     if not user:
         raise HTTPException(status_code=401, detail="Invalid or expired token")
-    tenant_id = _require_tenant(user)
+    tenant_id = _require_admin_tenant(user)
 
     from semantic_layer.engine import (
         SqlSafetyError,
@@ -1123,13 +1128,13 @@ async def admin_put_overlay(
 @app.delete("/api/v1/admin/overlay/{metric_id}")
 async def admin_delete_overlay(
     metric_id: str,
-    authorization: str = Header(...),
+    authorization: Optional[str] = Header(None),
 ) -> dict:
     """Remove this tenant's overlay for one metric. Canonical applies after."""
     user = _get_user_from_token(authorization)
     if not user:
         raise HTTPException(status_code=401, detail="Invalid or expired token")
-    tenant_id = _require_tenant(user)
+    tenant_id = _require_admin_tenant(user)
 
     import tenant_store
     tenant_store.delete_overlay(tenant_id, metric_id)
```

- [ ] **Step 5: Verify**

Run: `$PY -m pytest -q tests`
Expected: `161 passed`.

- [ ] **Step 6: Verify**

Run: `(cd cdk && npx cdk synth -q -c environment=dev -c initialUserPassword=Synth-Only-Passw0rd! --exclusively IlluminateBase-dev > /dev/null && $PY test/check_template.py)`
Expected: all `PASS`.

- [ ] **Step 7: Verify**

Run: `cd cdk && npx tsc --noEmit`
Expected: no output.

- [ ] **Step 8: Commit, open the PR, merge**

```bash
git add tests/test_admin_authz.py cdk/test/check_template.py lambda_handler.py cdk/lib/base/auth.ts
git commit -m "feat: illuminate-admins group required for /admin endpoints; missing token is 401

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin feat/admin-group
gh pr create --base main --title "feat: illuminate-admins group required for /admin endpoints; missing token is 401" --body "$(cat <<'EOF'
**Claude:** Phase 3b. Adds a Cognito group, `illuminate-admins`, and puts the initial user in it. The four `/api/v1/admin/*` routes now return 403 unless the token's `cognito:groups` claim includes that group; previously any user with a tenant could edit that tenant's overlays. Every route's `Authorization` header is now optional at the framework level, so a missing header gets the route's own 401 instead of FastAPI's 422. All 18 routes that take a token validate it themselves (checked by walking the code's syntax tree); cancel gets its check in 3d.

Reviewable surface: 4 files, about 130 lines.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge feat/admin-group --squash --delete-branch
```

### Task 3 (PR 3c): conversations are private to the user who created them

**Repo:** `illuminate-conversational-intelligence`

**Files:** `tests/test_conversation_ownership.py`, `requirements-dev.txt`, `conversation_store.py`, `lambda_handler.py`

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b feat/conversation-ownership origin/main
```

- [ ] **Step 2: Write the failing checks**

```diff
diff --git a/requirements-dev.txt b/requirements-dev.txt
index c58fa02..87355d9 100644
--- a/requirements-dev.txt
+++ b/requirements-dev.txt
@@ -2,3 +2,4 @@
 #   pip install -r requirements-lambda.txt -r requirements-dev.txt
 pytest>=8.0.0
 httpx>=0.27
+moto[dynamodb]>=5.0
diff --git a/tests/test_conversation_ownership.py b/tests/test_conversation_ownership.py
new file mode 100644
index 0000000..e716bd9
--- /dev/null
+++ b/tests/test_conversation_ownership.py
@@ -0,0 +1,75 @@
+import boto3
+import pytest
+from fastapi.testclient import TestClient
+from moto import mock_aws
+
+import conversation_store
+import lambda_handler
+
+AUTH = {"Authorization": "Bearer test"}
+
+
+@pytest.fixture
+def table(monkeypatch):
+    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
+    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
+    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
+    monkeypatch.delenv("AWS_PROFILE", raising=False)
+    with mock_aws():
+        t = boto3.resource("dynamodb", region_name="us-east-1").create_table(
+            TableName="conversations-test",
+            KeySchema=[{"AttributeName": "context_id", "KeyType": "HASH"}],
+            AttributeDefinitions=[{"AttributeName": "context_id", "AttributeType": "S"}],
+            BillingMode="PAY_PER_REQUEST",
+        )
+        monkeypatch.setattr(conversation_store, "_table", t)
+        yield t
+
+
+def test_history_is_private_to_its_owner(table):
+    conversation_store.save_turn("c1", "alice", "hi", "hello")
+    assert [m["content"] for m in conversation_store.load_history("c1", "alice")] == ["hi", "hello"]
+    assert conversation_store.load_history("c1", "bob") == []
+
+
+def test_another_user_cannot_append_to_or_overwrite_a_conversation(table):
+    conversation_store.save_turn("c1", "alice", "hi", "hello")
+    conversation_store.save_turn("c1", "bob", "mine now", "ok")
+    assert [m["content"] for m in conversation_store.load_history("c1", "alice")] == ["hi", "hello"]
+
+
+def test_only_the_owner_can_clear(table):
+    conversation_store.save_turn("c1", "alice", "hi", "hello")
+    assert conversation_store.clear_history("c1", "bob") is False
+    assert conversation_store.load_history("c1", "alice") != []
+    assert conversation_store.clear_history("c1", "alice") is True
+    assert conversation_store.load_history("c1", "alice") == []
+
+
+def test_legacy_items_without_an_owner_are_not_readable(table):
+    table.put_item(Item={"context_id": "old", "messages": '[{"role": "user", "content": "x"}]'})
+    assert conversation_store.load_history("old", "alice") == []
+
+
+@pytest.fixture
+def client(table, monkeypatch):
+    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: {"sub": "bob"} if a else None)
+    return TestClient(lambda_handler.app)
+
+
+def test_reading_someone_elses_conversation_is_404(client):
+    conversation_store.save_turn("c1", "alice", "hi", "hello")
+    assert client.get("/api/conversations/c1", headers=AUTH).status_code == 404
+
+
+def test_deleting_someone_elses_conversation_is_404_and_keeps_it(client):
+    conversation_store.save_turn("c1", "alice", "hi", "hello")
+    assert client.delete("/api/conversations/c1", headers=AUTH).status_code == 404
+    assert conversation_store.load_history("c1", "alice") != []
+
+
+def test_owner_can_read_their_conversation(client):
+    conversation_store.save_turn("c2", "bob", "q", "a")
+    r = client.get("/api/conversations/c2", headers=AUTH)
+    assert r.status_code == 200
+    assert [m["content"] for m in r.json()["messages"]] == ["q", "a"]
```

- [ ] **Step 3: Run them to verify they fail**

Run: `$PY -m pytest -q tests/test_conversation_ownership.py`
Expected: errors: `save_turn() takes 3 positional arguments but 4 were given` and `load_history() takes 1 positional argument but 2 were given`.

- [ ] **Step 4: Implement**

```diff
diff --git a/conversation_store.py b/conversation_store.py
index 1424f95..fdcff25 100644
--- a/conversation_store.py
+++ b/conversation_store.py
@@ -11,7 +11,8 @@ import logging
 from typing import Optional
 
 import boto3
-from boto3.dynamodb.conditions import Key
+from boto3.dynamodb.conditions import Attr, Key
+from botocore.exceptions import ClientError
 
 logger = logging.getLogger("API-PROXY")
 
@@ -31,19 +32,30 @@ def _get_table():
     return _table
 
 
-def load_history(context_id: str) -> list[dict]:
-    """Load conversation messages for a context_id.
+def _item(context_id: str) -> Optional[dict]:
+    return _get_table().get_item(Key={"context_id": context_id}).get("Item")
 
-    Returns list of {"role": "user"|"assistant", "content": "..."} dicts,
-    ordered chronologically. Returns empty list if no history.
+
+def owns(context_id: str, owner: str) -> bool:
+    """True when the conversation exists and was created by owner (a Cognito sub)."""
+    try:
+        item = _item(context_id)
+    except Exception as e:
+        logger.warning(f"Failed to read conversation owner: {e}")
+        return False
+    return bool(item) and item.get("owner_sub") == owner
+
+
+def load_history(context_id: str, owner: str) -> list[dict]:
+    """Messages for context_id, oldest first; empty if missing or owned by someone else.
+
+    Returns list of {"role": "user"|"assistant", "content": "..."} dicts.
     """
     if not context_id:
         return []
     try:
-        table = _get_table()
-        response = table.get_item(Key={"context_id": context_id})
-        item = response.get("Item")
-        if not item:
+        item = _item(context_id)
+        if not item or item.get("owner_sub") != owner:
             return []
         messages = json.loads(item.get("messages", "[]"))
         return messages[-_MAX_MESSAGES:]
@@ -52,38 +64,49 @@ def load_history(context_id: str) -> list[dict]:
         return []
 
 
-def save_turn(context_id: str, user_message: str, assistant_message: str):
-    """Append a user+assistant turn to conversation history.
+def save_turn(context_id: str, owner: str, user_message: str, assistant_message: str):
+    """Append a turn; never writes over a conversation that belongs to someone else.
 
-    Creates the item if it doesn't exist, appends if it does.
-    Trims to MAX_MESSAGES and sets TTL for automatic cleanup.
+    Items without an owner predate ownership tracking; the first writer claims them.
     """
     if not context_id:
         return
     try:
-        history = load_history(context_id)
+        history = load_history(context_id, owner)
         history.append({"role": "user", "content": user_message})
         history.append({"role": "assistant", "content": assistant_message})
-        # Trim to max
         history = history[-_MAX_MESSAGES:]
 
-        table = _get_table()
-        table.put_item(Item={
-            "context_id": context_id,
-            "messages": json.dumps(history),
-            "updated_at": int(time.time()),
-            "ttl": int(time.time()) + _TTL_SECONDS,
-        })
+        _get_table().put_item(
+            Item={
+                "context_id": context_id,
+                "owner_sub": owner,
+                "messages": json.dumps(history),
+                "updated_at": int(time.time()),
+                "ttl": int(time.time()) + _TTL_SECONDS,
+            },
+            ConditionExpression=Attr("owner_sub").not_exists() | Attr("owner_sub").eq(owner),
+        )
+    except ClientError as e:
+        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
+            logger.warning("Refused to save turn: context %s belongs to another user", context_id)
+        else:
+            logger.warning(f"Failed to save conversation history: {e}")
     except Exception as e:
         logger.warning(f"Failed to save conversation history: {e}")
 
 
-def clear_history(context_id: str):
-    """Delete conversation history for a context_id."""
+def clear_history(context_id: str, owner: str) -> bool:
+    """Delete the conversation if owner owns it; False when it is missing or someone else's."""
     if not context_id:
-        return
+        return False
     try:
-        table = _get_table()
-        table.delete_item(Key={"context_id": context_id})
-    except Exception as e:
-        logger.warning(f"Failed to clear conversation history: {e}")
+        _get_table().delete_item(
+            Key={"context_id": context_id},
+            ConditionExpression=Attr("owner_sub").eq(owner),
+        )
+        return True
+    except ClientError as e:
+        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
+            logger.warning(f"Failed to clear conversation history: {e}")
+        return False
diff --git a/lambda_handler.py b/lambda_handler.py
index f64d568..f609e60 100644
--- a/lambda_handler.py
+++ b/lambda_handler.py
@@ -338,6 +338,7 @@ class HealthResponse(BaseModel):
 
 async def send_message(
     message_text: str,
+    owner: str,
     context_id: Optional[str] = None,
     tenant_id: Optional[str] = None,
 ) -> dict:
@@ -346,7 +347,7 @@ async def send_message(
     from chat_engine import send_message as engine_send
     from conversation_store import load_history, save_turn
 
-    history = load_history(context_id) if context_id else []
+    history = load_history(context_id, owner) if context_id else []
     bedrock_history = []
     for msg in history:
         bedrock_history.append({
@@ -360,13 +361,14 @@ async def send_message(
     )
 
     if context_id:
-        save_turn(context_id, message_text, response_text)
+        save_turn(context_id, owner, message_text, response_text)
 
     return {"text": response_text, "contextId": context_id}
 
 
 async def send_message_streaming(
     message_text: str,
+    owner: str,
     context_id: Optional[str] = None,
     tenant_id: Optional[str] = None,
 ):
@@ -376,7 +378,7 @@ async def send_message_streaming(
 
     yield {"type": "status", "message": "Processing your question..."}
 
-    history = load_history(context_id) if context_id else []
+    history = load_history(context_id, owner) if context_id else []
     bedrock_history = []
     for msg in history:
         bedrock_history.append({
@@ -392,7 +394,7 @@ async def send_message_streaming(
             elif event["type"] == "raw_complete":
                 full_text = event["text"]
                 if context_id:
-                    save_turn(context_id, message_text, full_text)
+                    save_turn(context_id, owner, message_text, full_text)
 
         if not full_text:
             yield {"type": "error", "message": "Empty response"}
@@ -475,6 +477,7 @@ async def chat(
     try:
         result = await send_message(
             message_text=message_text,
+            owner=user["sub"],
             context_id=context_id,
             tenant_id=_tenant_id_from_user(user),
         )
@@ -534,6 +537,7 @@ async def chat_stream(
         try:
             async for event in send_message_streaming(
                 message_text=message_text,
+                owner=user["sub"],
                 context_id=context_id,
                 tenant_id=_tenant_id_from_user(user),
             ):
@@ -595,9 +599,10 @@ async def get_conversation(
         raise HTTPException(status_code=401, detail="Invalid or expired token")
 
     logger.info(f"Conversation history requested for context: {context_id}")
-    from conversation_store import load_history
-    history = load_history(context_id)
-    return {"messages": history}
+    from conversation_store import load_history, owns
+    if not owns(context_id, user["sub"]):
+        raise HTTPException(status_code=404, detail="Conversation not found")
+    return {"messages": load_history(context_id, user["sub"])}
 
 
 @app.delete("/api/conversations/{context_id}")
@@ -612,7 +617,8 @@ async def clear_conversation(
 
     logger.info(f"Clear conversation requested for context: {context_id}")
     from conversation_store import clear_history
-    clear_history(context_id)
+    if not clear_history(context_id, user["sub"]):
+        raise HTTPException(status_code=404, detail="Conversation not found")
     return {"success": True}
```

- [ ] **Step 5: Verify**

Run: `$PY -m pytest -q tests`
Expected: `168 passed`.

- [ ] **Step 6: Commit, open the PR, merge**

```bash
git add tests/test_conversation_ownership.py requirements-dev.txt conversation_store.py lambda_handler.py
git commit -m "fix: conversations are private to the user who created them

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin feat/conversation-ownership
gh pr create --base main --title "fix: conversations are private to the user who created them" --body "$(cat <<'EOF'
**Claude:** Phase 3c. Conversation items now record the creator's Cognito `sub` (`owner_sub`). History loads only for the owner. Saving uses a DynamoDB condition, so a turn can't append to or overwrite someone else's conversation. Deleting is likewise conditional. `GET` and `DELETE /api/conversations/{id}` return 404 for anything the caller doesn't own. Items written before this change have no owner and stop being readable; the first new write claims them, and they expire within 30 days by TTL anyway. Tests use `moto` (new dev dependency) so the condition expressions are evaluated for real.

Reviewable surface: 4 files, about 140 lines.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge feat/conversation-ownership --squash --delete-branch
```

### Task 4 (PR 3d): cancelling a chat request requires its owner's token

**Repo:** `illuminate-conversational-intelligence`

**Files:** `tests/test_cancel_authz.py`, `lambda_handler.py`

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b fix/cancel-ownership origin/main
```

- [ ] **Step 2: Write the failing checks**

```diff
diff --git a/tests/test_cancel_authz.py b/tests/test_cancel_authz.py
new file mode 100644
index 0000000..f4b53e7
--- /dev/null
+++ b/tests/test_cancel_authz.py
@@ -0,0 +1,51 @@
+import pytest
+from fastapi.testclient import TestClient
+
+import lambda_handler
+
+AUTH = {"Authorization": "Bearer test"}
+
+
+@pytest.fixture
+def client(monkeypatch):
+    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: {"sub": "bob"} if a else None)
+    monkeypatch.setattr(lambda_handler, "_request_owners", {})
+    monkeypatch.setattr(lambda_handler, "_cancelled_requests", set())
+    return TestClient(lambda_handler.app)
+
+
+def test_cancel_requires_a_token(client):
+    lambda_handler._request_owners["r1"] = "bob"
+    assert client.post("/api/chat/cancel/r1").status_code == 401
+    assert lambda_handler._cancelled_requests == set()
+
+
+def test_cannot_cancel_someone_elses_request(client):
+    lambda_handler._request_owners["r1"] = "alice"
+    assert client.post("/api/chat/cancel/r1", headers=AUTH).status_code == 404
+    assert lambda_handler._cancelled_requests == set()
+
+
+def test_unknown_request_is_404(client):
+    assert client.post("/api/chat/cancel/nope", headers=AUTH).status_code == 404
+
+
+def test_owner_can_cancel(client):
+    lambda_handler._request_owners["r1"] = "bob"
+    r = client.post("/api/chat/cancel/r1", headers=AUTH)
+    assert r.status_code == 200 and r.json()["success"] is True
+    assert "r1" in lambda_handler._cancelled_requests
+
+
+def test_streaming_registers_and_releases_the_request_owner(client, monkeypatch):
+    seen = []
+
+    async def fake_stream(message_text, owner, context_id=None, tenant_id=None):
+        seen.append(dict(lambda_handler._request_owners))
+        yield {"type": "status", "message": "working"}
+
+    monkeypatch.setattr(lambda_handler, "send_message_streaming", fake_stream)
+    r = client.post("/api/chat/stream", headers=AUTH, json={"message": "hi", "request_id": "r9"})
+    assert r.status_code == 200
+    assert seen == [{"r9": "bob"}]
+    assert lambda_handler._request_owners == {}
```

- [ ] **Step 3: Run them to verify they fail**

Run: `$PY -m pytest -q tests/test_cancel_authz.py`
Expected: 5 errors: `has no attribute '_request_owners'`.

- [ ] **Step 4: Implement**

```diff
diff --git a/lambda_handler.py b/lambda_handler.py
index f609e60..4572e5d 100644
--- a/lambda_handler.py
+++ b/lambda_handler.py
@@ -442,6 +442,9 @@ app.add_middleware(
 
 # Track cancelled request IDs
 _cancelled_requests: set[str] = set()
+# request_id -> Cognito sub of the user streaming it. Per Lambda instance, so a cancel that
+# lands on a different instance than its stream finds nothing and returns 404.
+_request_owners: dict[str, str] = {}
 
 
 @app.get("/health", response_model=HealthResponse)
@@ -532,6 +535,9 @@ async def chat_stream(
         f"context_id={context_id}, request_id={request_id}"
     )
 
+    if request_id:
+        _request_owners[request_id] = user["sub"]
+
     async def event_generator():
         """Relay SSE events from chat_engine to the frontend."""
         try:
@@ -565,6 +571,7 @@ async def chat_stream(
         finally:
             if request_id:
                 _cancelled_requests.discard(request_id)
+                _request_owners.pop(request_id, None)
 
     return StreamingResponse(
         event_generator(),
@@ -582,7 +589,13 @@ async def cancel_chat(
     request_id: str,
     authorization: Optional[str] = Header(None)
 ):
-    """Cancel an in-progress chat request."""
+    """Cancel an in-progress chat request owned by the caller."""
+    user = _get_user_from_token(authorization)
+    if not user:
+        raise HTTPException(status_code=401, detail="Invalid or expired token")
+    if _request_owners.get(request_id) != user["sub"]:
+        raise HTTPException(status_code=404, detail="No such request in progress")
+
     logger.info(f"Cancelling request: {request_id}")
     _cancelled_requests.add(request_id)
     return {"success": True, "request_id": request_id}
```

- [ ] **Step 5: Verify**

Run: `$PY -m pytest -q tests`
Expected: `173 passed`.

- [ ] **Step 6: Commit, open the PR, merge**

```bash
git add tests/test_cancel_authz.py lambda_handler.py
git commit -m "fix: cancelling a chat request requires its owner's token

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin fix/cancel-ownership
gh pr create --base main --title "fix: cancelling a chat request requires its owner's token" --body "$(cat <<'EOF'
**Claude:** Phase 3d. `POST /api/chat/cancel/{request_id}` had no authentication, so anyone could cancel anyone's stream. It now needs a valid token, and the request must belong to the caller: streams record `request_id → sub` while they run. A cancel that lands on a different Lambda instance from its stream returns 404. Before this change it returned success but couldn't actually reach that stream.

Reviewable surface: 2 files, about 80 lines.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge fix/cancel-ownership --squash --delete-branch
```

### Task 5 (PR 3e): remove the unused VPC/NAT gateway and the unattached WAF

**Repo:** `illuminate-conversational-intelligence`

**Files:** `cdk/test/check_template.py`, `cdk/lib/base/index.ts`, `cdk/lib/base/networking.ts`, `cdk/lib/base/waf.ts`, `cdk/lib/api/lambda-proxy.ts`, `cdk/bin/illuminate.ts`

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b chore/remove-unused-vpc-waf origin/main
```

- [ ] **Step 2: Write the failing checks**

```diff
diff --git a/cdk/test/check_template.py b/cdk/test/check_template.py
index 17943e0..00ac48b 100644
--- a/cdk/test/check_template.py
+++ b/cdk/test/check_template.py
@@ -33,6 +33,9 @@ def main() -> int:
         check("users cannot write custom:tenant_id", writable is not None and "custom:tenant_id" not in writable),
         check("illuminate-admins group exists", "illuminate-admins" in groups),
         check("initial user is added to the admin group", "adminAddUserToGroup" in base_text),
+        check("no VPC or NAT gateway (nothing runs in it)",
+              not resources("IlluminateBase-dev", "AWS::EC2::VPC") and not resources("IlluminateBase-dev", "AWS::EC2::NatGateway")),
+        check("no unattached WAF web ACL", not resources("IlluminateBase-dev", "AWS::WAFv2::WebACL")),
     ]
     return 0 if all(results) else 1
```

- [ ] **Step 3: Run them to verify they fail**

Run: `(cd cdk && npx cdk synth -q -c environment=dev -c initialUserPassword=Synth-Only-Passw0rd! --exclusively IlluminateBase-dev > /dev/null && $PY test/check_template.py)`
Expected: `FAIL no VPC or NAT gateway`, `FAIL no unattached WAF web ACL`.

- [ ] **Step 4: Implement**

```diff
diff --git a/cdk/bin/illuminate.ts b/cdk/bin/illuminate.ts
index 502274f..9135b04 100644
--- a/cdk/bin/illuminate.ts
+++ b/cdk/bin/illuminate.ts
@@ -32,7 +32,7 @@ const env: cdk.Environment = {
 };
 
 // =============================================================================
-// Stack 1: Base infrastructure (VPC, Cognito, S3, Secrets, WAF)
+// Stack 1: Base infrastructure (Cognito, S3, Secrets)
 // =============================================================================
 const base = new BaseStack(app, `IlluminateBase-${environment}`, {
   env,
diff --git a/cdk/lib/api/lambda-proxy.ts b/cdk/lib/api/lambda-proxy.ts
index ace2a56..064e9d0 100644
--- a/cdk/lib/api/lambda-proxy.ts
+++ b/cdk/lib/api/lambda-proxy.ts
@@ -149,7 +149,8 @@ export class LambdaProxy extends Construct {
       },
     });
 
-    // Function URL with response streaming
+    // Function URL with response streaming. Auth is the app's Cognito JWT check on every route:
+    // AWS_IAM behind CloudFront OAC would need the browser to sign a SHA-256 of each POST body.
     this.functionUrl = this.fn.addFunctionUrl({
       authType: lambda.FunctionUrlAuthType.NONE,
       invokeMode: lambda.InvokeMode.RESPONSE_STREAM,
diff --git a/cdk/lib/base/index.ts b/cdk/lib/base/index.ts
index c08511a..8610371 100644
--- a/cdk/lib/base/index.ts
+++ b/cdk/lib/base/index.ts
@@ -1,13 +1,10 @@
 import * as cdk from 'aws-cdk-lib';
 import { Construct } from 'constructs';
-import * as ec2 from 'aws-cdk-lib/aws-ec2';
 import * as cognito from 'aws-cdk-lib/aws-cognito';
 import * as s3 from 'aws-cdk-lib/aws-s3';
 import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
-import { Networking } from './networking';
 import { Auth } from './auth';
 import { Storage } from './storage';
-import { Waf } from './waf';
 import { Discovery } from './discovery';
 
 export interface BaseStackProps extends cdk.StackProps {
@@ -24,25 +21,14 @@ export interface BaseStackProps extends cdk.StackProps {
 }
 
 export class BaseStack extends cdk.Stack {
-  public readonly vpc: ec2.Vpc;
-  public readonly securityGroup: ec2.SecurityGroup;
   public readonly userPool: cognito.UserPool;
   public readonly userPoolClient: cognito.UserPoolClient;
   public readonly artifactsBucket: s3.Bucket;
   public readonly snowflakeSecret: secretsmanager.Secret;
-  public readonly webAclArn: string;
 
   constructor(scope: Construct, id: string, props: BaseStackProps) {
     super(scope, id, props);
 
-    const isProd = props.environment === 'prod';
-
-    const networking = new Networking(this, 'Networking', {
-      environment: props.environment,
-    });
-    this.vpc = networking.vpc;
-    this.securityGroup = networking.securityGroup;
-
     const auth = new Auth(this, 'Auth', {
       environment: props.environment,
       initialUserEmail: props.initialUserEmail,
@@ -64,13 +50,6 @@ export class BaseStack extends cdk.Stack {
     this.artifactsBucket = storage.artifactsBucket;
     this.snowflakeSecret = storage.snowflakeSecret;
 
-    const waf = new Waf(this, 'Waf', {
-      environment: props.environment,
-      isProd,
-      scope: 'REGIONAL',
-    });
-    this.webAclArn = waf.webAclArn;
-
     // Publish discovery parameters to SSM
     new Discovery(this, 'Discovery', {
       environment: props.environment,
diff --git a/cdk/lib/base/networking.ts b/cdk/lib/base/networking.ts
deleted file mode 100644
index 399b414..0000000
--- a/cdk/lib/base/networking.ts
+++ /dev/null
@@ -1,48 +0,0 @@
-import { Construct } from 'constructs';
-import * as ec2 from 'aws-cdk-lib/aws-ec2';
-
-export interface NetworkingProps {
-  environment: string;
-}
-
-export class Networking extends Construct {
-  public readonly vpc: ec2.Vpc;
-  public readonly securityGroup: ec2.SecurityGroup;
-
-  constructor(scope: Construct, id: string, props: NetworkingProps) {
-    super(scope, id);
-
-    this.vpc = new ec2.Vpc(this, 'Vpc', {
-      vpcName: `illuminate-vpc-${props.environment}`,
-      ipAddresses: ec2.IpAddresses.cidr('10.0.0.0/16'),
-      maxAzs: 2,
-      natGateways: 1,
-      subnetConfiguration: [
-        {
-          name: 'Public',
-          subnetType: ec2.SubnetType.PUBLIC,
-          cidrMask: 24,
-          mapPublicIpOnLaunch: true,
-        },
-        {
-          name: 'Private',
-          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
-          cidrMask: 24,
-        },
-      ],
-    });
-
-    this.securityGroup = new ec2.SecurityGroup(this, 'SecurityGroup', {
-      vpc: this.vpc,
-      securityGroupName: `illuminate-sg-${props.environment}`,
-      description: 'Security group for Illuminate agents',
-    });
-
-    // Allow HTTPS traffic within the security group
-    this.securityGroup.addIngressRule(
-      this.securityGroup,
-      ec2.Port.tcp(443),
-      'Allow HTTPS from self',
-    );
-  }
-}
diff --git a/cdk/lib/base/waf.ts b/cdk/lib/base/waf.ts
deleted file mode 100644
index 2ed6351..0000000
--- a/cdk/lib/base/waf.ts
+++ /dev/null
@@ -1,109 +0,0 @@
-import { Construct } from 'constructs';
-import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
-
-export interface WafProps {
-  environment: string;
-  isProd: boolean;
-  /** 'REGIONAL' for ALB/API Gateway, 'CLOUDFRONT' for CloudFront distributions */
-  scope: 'REGIONAL' | 'CLOUDFRONT';
-  /** Rule names to exclude from AWSManagedRulesCommonRuleSet */
-  commonRuleExclusions?: string[];
-}
-
-/**
- * Reusable WAF WebACL construct. Used by both BaseStack (REGIONAL) and
- * FrontendStack (CLOUDFRONT) with the same rule set.
- */
-export class Waf extends Construct {
-  public readonly webAclArn: string;
-
-  constructor(scope: Construct, id: string, props: WafProps) {
-    super(scope, id);
-
-    const suffix = props.scope === 'CLOUDFRONT' ? 'cf-waf' : 'waf';
-
-    const commonRuleConfig: wafv2.CfnWebACL.RuleProperty = {
-      name: 'AWSManagedRulesCommonRuleSet',
-      priority: 2,
-      statement: {
-        managedRuleGroupStatement: {
-          vendorName: 'AWS',
-          name: 'AWSManagedRulesCommonRuleSet',
-          ...(props.commonRuleExclusions?.length ? {
-            excludedRules: props.commonRuleExclusions.map(name => ({ name })),
-          } : {}),
-        },
-      },
-      overrideAction: { none: {} },
-      visibilityConfig: {
-        sampledRequestsEnabled: true,
-        cloudWatchMetricsEnabled: true,
-        metricName: 'AWSManagedRulesCommonRuleSet',
-      },
-    };
-
-    const webAcl = new wafv2.CfnWebACL(this, 'WebACL', {
-      name: `illuminate-${suffix}-${props.environment}`,
-      scope: props.scope,
-      defaultAction: { allow: {} },
-      rules: [
-        {
-          name: 'RateLimitRule',
-          priority: 1,
-          statement: {
-            rateBasedStatement: {
-              limit: props.isProd ? 1000 : 2000,
-              aggregateKeyType: 'IP',
-            },
-          },
-          action: { block: {} },
-          visibilityConfig: {
-            sampledRequestsEnabled: true,
-            cloudWatchMetricsEnabled: true,
-            metricName: 'RateLimitRule',
-          },
-        },
-        commonRuleConfig,
-        {
-          name: 'AWSManagedRulesSQLiRuleSet',
-          priority: 3,
-          statement: {
-            managedRuleGroupStatement: {
-              vendorName: 'AWS',
-              name: 'AWSManagedRulesSQLiRuleSet',
-            },
-          },
-          overrideAction: { none: {} },
-          visibilityConfig: {
-            sampledRequestsEnabled: true,
-            cloudWatchMetricsEnabled: true,
-            metricName: 'AWSManagedRulesSQLiRuleSet',
-          },
-        },
-        {
-          name: 'AWSManagedRulesKnownBadInputsRuleSet',
-          priority: 4,
-          statement: {
-            managedRuleGroupStatement: {
-              vendorName: 'AWS',
-              name: 'AWSManagedRulesKnownBadInputsRuleSet',
-            },
-          },
-          overrideAction: { none: {} },
-          visibilityConfig: {
-            sampledRequestsEnabled: true,
-            cloudWatchMetricsEnabled: true,
-            metricName: 'AWSManagedRulesKnownBadInputsRuleSet',
-          },
-        },
-      ],
-      visibilityConfig: {
-        sampledRequestsEnabled: true,
-        cloudWatchMetricsEnabled: true,
-        metricName: `illuminate-${suffix}-${props.environment}`,
-      },
-    });
-
-    this.webAclArn = webAcl.attrArn;
-  }
-}
```

- [ ] **Step 5: Verify**

Run: `(cd cdk && npx cdk synth -q -c environment=dev -c initialUserPassword=Synth-Only-Passw0rd! --exclusively IlluminateBase-dev > /dev/null && $PY test/check_template.py)`
Expected: all `PASS`.

- [ ] **Step 6: Verify**

Run: `cd cdk && npx tsc --noEmit`
Expected: no output.

- [ ] **Step 7: Commit, open the PR, merge**

```bash
git add cdk/test/check_template.py cdk/lib/base/index.ts cdk/lib/base/networking.ts cdk/lib/base/waf.ts cdk/lib/api/lambda-proxy.ts cdk/bin/illuminate.ts
git commit -m "chore: remove the unused VPC/NAT gateway and the unattached WAF

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin chore/remove-unused-vpc-waf
gh pr create --base main --title "chore: remove the unused VPC/NAT gateway and the unattached WAF" --body "$(cat <<'EOF'
**Claude:** Phase 3e. Nothing runs in the base stack's VPC; the Lambda isn't attached to it. Its NAT gateway costs money every hour. The regional WAF web ACL was never associated with anything, and a Lambda Function URL can't take a WAF anyway. Both are removed. The API stack imports nothing from them: its only cross-stack imports are the user pool, client, bucket and secret. A comment at the Function URL now records why it stays JWT-only rather than IAM behind CloudFront. Deploy note: the next base-stack deploy deletes the VPC, subnets, NAT, EIP and web ACL.

Reviewable surface: 6 files, -180 lines.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge chore/remove-unused-vpc-waf --squash --delete-branch
```

### Task 6 (PR 3f): API role can read but not write the Snowflake secret

**Repo:** `illuminate-conversational-intelligence`

**Files:** `cdk/test/check_template.py`, `cdk/lib/api/lambda-proxy.ts`

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b fix/lambda-secret-read-only origin/main
```

- [ ] **Step 2: Write the failing checks**

```diff
diff --git a/cdk/test/check_template.py b/cdk/test/check_template.py
index 00ac48b..6850de4 100644
--- a/cdk/test/check_template.py
+++ b/cdk/test/check_template.py
@@ -36,6 +36,8 @@ def main() -> int:
         check("no VPC or NAT gateway (nothing runs in it)",
               not resources("IlluminateBase-dev", "AWS::EC2::VPC") and not resources("IlluminateBase-dev", "AWS::EC2::NatGateway")),
         check("no unattached WAF web ACL", not resources("IlluminateBase-dev", "AWS::WAFv2::WebACL")),
+        check("API cannot write the Snowflake secret (nothing in the API writes it)",
+              "secretsmanager:PutSecretValue" not in json.dumps(resources("IlluminateApi-dev", "AWS::IAM::Policy"))),
     ]
     return 0 if all(results) else 1
```

- [ ] **Step 3: Run them to verify they fail**

Run: `(cd cdk && npx cdk synth -q -c environment=dev -c initialUserPassword=Synth-Only-Passw0rd! --exclusively IlluminateBase-dev > /dev/null && $PY test/check_template.py)`
Expected: `FAIL API cannot write the Snowflake secret`.

- [ ] **Step 4: Implement**

```diff
diff --git a/cdk/lib/api/lambda-proxy.ts b/cdk/lib/api/lambda-proxy.ts
index 064e9d0..9751620 100644
--- a/cdk/lib/api/lambda-proxy.ts
+++ b/cdk/lib/api/lambda-proxy.ts
@@ -72,7 +72,7 @@ export class LambdaProxy extends Construct {
 
     // Secrets Manager (read + write for config endpoint)
     role.addToPolicy(new iam.PolicyStatement({
-      actions: ['secretsmanager:GetSecretValue', 'secretsmanager:PutSecretValue'],
+      actions: ['secretsmanager:GetSecretValue'],
       resources: [props.snowflakeSecretArn],
     }));
```

- [ ] **Step 5: Verify**

Run: `(cd cdk && npx cdk synth -q -c environment=dev -c initialUserPassword=Synth-Only-Passw0rd! --exclusively IlluminateBase-dev > /dev/null && $PY test/check_template.py)`
Expected: all `PASS`.

- [ ] **Step 6: Commit, open the PR, merge**

```bash
git add cdk/test/check_template.py cdk/lib/api/lambda-proxy.ts
git commit -m "fix: API role can read but not write the Snowflake secret

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin fix/lambda-secret-read-only
gh pr create --base main --title "fix: API role can read but not write the Snowflake secret" --body "$(cat <<'EOF'
**Claude:** Phase 3f. The Lambda role had `secretsmanager:PutSecretValue` on the Snowflake secret, but nothing in the API writes it. The frontend's Settings editor called a `/api/v1/config/snowflake` route that doesn't exist; 3h removes that editor. Least privilege: read only.

Reviewable surface: 2 files, 3 lines.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge fix/lambda-secret-read-only --squash --delete-branch
```

### Task 7 (PR 3g): response scrubber no longer redacts plain 9-10 digit numbers

**Repo:** `illuminate-conversational-intelligence`

**Files:** `tests/test_scrub_pii.py`, `lambda_handler.py`

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b fix/pii-scrubber-digits origin/main
```

- [ ] **Step 2: Write the failing checks**

```diff
diff --git a/tests/test_scrub_pii.py b/tests/test_scrub_pii.py
new file mode 100644
index 0000000..1284186
--- /dev/null
+++ b/tests/test_scrub_pii.py
@@ -0,0 +1,24 @@
+import pytest
+
+from lambda_handler import _scrub_pii
+
+
+@pytest.mark.parametrize("text", [
+    "There were 123456789 page views this term.",
+    "Course 1234567890 has 42 students.",
+    "Revenue grew to 2500000000 credits.",
+])
+def test_plain_large_numbers_survive(text):
+    assert _scrub_pii(text) == text
+
+
+@pytest.mark.parametrize("text,redacted", [
+    ("SSN 123-45-6789 on file", "[SSN REDACTED]"),
+    ("email jane.doe@example.edu now", "[EMAIL REDACTED]"),
+    ("call (555) 123-4567 today", "[PHONE REDACTED]"),
+    ("call 555-123-4567 today", "[PHONE REDACTED]"),
+    ("call 555.123.4567 today", "[PHONE REDACTED]"),
+    ("card 4111 1111 1111 1111 used", "[CARD REDACTED]"),
+])
+def test_formatted_pii_is_redacted(text, redacted):
+    assert redacted in _scrub_pii(text)
```

- [ ] **Step 3: Run them to verify they fail**

Run: `$PY -m pytest -q tests/test_scrub_pii.py`
Expected: `4 failed, 5 passed`: three plain numbers redacted; `(555) 123-4567` not redacted.

- [ ] **Step 4: Implement**

```diff
diff --git a/lambda_handler.py b/lambda_handler.py
index 4572e5d..912e12f 100644
--- a/lambda_handler.py
+++ b/lambda_handler.py
@@ -38,13 +38,12 @@ logger = logging.getLogger("API-PROXY")
 # Post-processing PII filter — runs on EVERY response before returning to user
 # =============================================================================
 
-# Patterns for common PII types (programmatic, not prompt-dependent)
+# Patterns for common PII types (programmatic, not prompt-dependent). Bare digit runs are not
+# matched: query results are full of 9-10 digit counts and IDs.
 _PII_PATTERNS = [
     (re.compile(r'\b\d{3}-\d{2}-\d{4}\b'), '[SSN REDACTED]'),                        # SSN with dashes
-    (re.compile(r'\b\d{9}\b'), '[ID REDACTED]'),                                       # SSN without dashes (9 digits)
     (re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'), '[EMAIL REDACTED]'),  # Email
-    (re.compile(r'\b\d{10}\b'), '[PHONE REDACTED]'),                                   # 10-digit phone
-    (re.compile(r'\b\(\d{3}\)\s*\d{3}-\d{4}\b'), '[PHONE REDACTED]'),                 # Phone (xxx) xxx-xxxx
+    (re.compile(r'\(\d{3}\)\s*\d{3}-\d{4}\b'), '[PHONE REDACTED]'),                   # Phone (xxx) xxx-xxxx
     (re.compile(r'\b\d{3}\.\d{3}\.\d{4}\b'), '[PHONE REDACTED]'),                     # Phone xxx.xxx.xxxx
     (re.compile(r'\b\d{3}-\d{3}-\d{4}\b'), '[PHONE REDACTED]'),                       # Phone xxx-xxx-xxxx
     (re.compile(r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b'), '[CARD REDACTED]'),   # Credit card
```

- [ ] **Step 5: Verify**

Run: `$PY -m pytest -q tests`
Expected: `182 passed`.

- [ ] **Step 6: Commit, open the PR, merge**

```bash
git add tests/test_scrub_pii.py lambda_handler.py
git commit -m "fix: response scrubber no longer redacts plain 9-10 digit numbers

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin fix/pii-scrubber-digits
gh pr create --base main --title "fix: response scrubber no longer redacts plain 9-10 digit numbers" --body "$(cat <<'EOF'
**Claude:** Phase 3g. The response PII scrubber replaced every 9- or 10-digit number with `[ID REDACTED]`/`[PHONE REDACTED]`, which mangled counts and IDs in answers. Those two patterns are removed. Formatted SSNs, emails, phone numbers and card numbers are still redacted. The `(xxx) xxx-xxxx` pattern never matched, because `\b` can't sit before `(`. That is now fixed.

Reviewable surface: 2 files, about 30 lines.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge fix/pii-scrubber-digits --squash --delete-branch
```

### Task 8 (PR 3h): admin-only UI for metric definitions; remove the non-functional Snowflake settings editor

**Repo:** `illuminate-poc`

**Files:** `src/services/authService.ts`, `src/context/AuthContext.tsx`, `src/components/NavDrawer.tsx`, `src/app/settings/page.tsx`, `src/app/admin/definitions/page.tsx`, `src/services/configApi.ts`

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b feat/admin-only-ui origin/main
```

- [ ] **Step 2: Implement**

```diff
diff --git a/src/app/admin/definitions/page.tsx b/src/app/admin/definitions/page.tsx
index c2709cd..13b988e 100644
--- a/src/app/admin/definitions/page.tsx
+++ b/src/app/admin/definitions/page.tsx
@@ -8,8 +8,25 @@ import {
 import {
   Layers, Loader2, Pencil, Save, RotateCcw, Trash2, AlertTriangle, CheckCircle2, X,
 } from "lucide-react";
+import { useAuth } from "@/context/AuthContext";
 
 export default function MetricDefinitionsPage() {
+  const { isAdmin } = useAuth();
+  if (!isAdmin) {
+    return (
+      <div className="max-w-3xl mx-auto px-4 sm:px-6 py-16 text-center">
+        <Layers size={28} className="mx-auto text-gray-400 mb-3" />
+        <h1 className="text-xl font-semibold text-gray-900">Metric Definitions</h1>
+        <p className="text-gray-500 mt-2">
+          Editing metric definitions requires administrator access. Ask your Illuminate administrator to add you.
+        </p>
+      </div>
+    );
+  }
+  return <MetricDefinitionsEditor />;
+}
+
+function MetricDefinitionsEditor() {
   const [tenantId, setTenantId] = useState<string>("");
   const [metrics, setMetrics] = useState<MetricSummary[]>([]);
   const [loading, setLoading] = useState(true);
diff --git a/src/app/settings/page.tsx b/src/app/settings/page.tsx
index 6cd2ca4..cb54016 100644
--- a/src/app/settings/page.tsx
+++ b/src/app/settings/page.tsx
@@ -1,17 +1,8 @@
 "use client";
 
-import { useState, useEffect, useCallback } from "react";
-import {
-  getSnowflakeConfig, updateSnowflakeConfig,
-  type SnowflakeConfig, type SnowflakeConfigUpdate,
-} from "@/services/configApi";
 import Link from "next/link";
-import {
-  Settings, User, Bell, Shield, Globe, Database,
-  Layers,
-  Copy, Check, Download, Eye, EyeOff, Pencil, Save,
-  Loader2, ChevronDown, ChevronUp, X,
-} from "lucide-react";
+import { Settings, User, Bell, Shield, Globe, Layers } from "lucide-react";
+import { useAuth } from "@/context/AuthContext";
 
 const sections = [
   { icon: User, title: "Profile", description: "Manage your account details, display name, and avatar" },
@@ -20,355 +11,10 @@ const sections = [
   { icon: Globe, title: "Language & Region", description: "Set your preferred language, timezone, and date format" },
 ];
 
-function CopyButton({ text }: { text: string }) {
-  const [copied, setCopied] = useState(false);
-  const handleCopy = async () => {
-    await navigator.clipboard.writeText(text);
-    setCopied(true);
-    setTimeout(() => setCopied(false), 2000);
-  };
-  return (
-    <button onClick={handleCopy} className="p-1 text-gray-400 hover:text-[#0066FF] rounded transition-colors" title="Copy">
-      {copied ? <Check size={14} className="text-emerald-500" /> : <Copy size={14} />}
-    </button>
-  );
-}
-
-// ── Snowflake Configuration ───────────────────────────────
-
-const configFields: { key: keyof Omit<SnowflakeConfig, "has_password">; label: string; env: string }[] = [
-  { key: "account", label: "Account", env: "SNOWFLAKE_ACCOUNT" },
-  { key: "user", label: "User", env: "SNOWFLAKE_USER" },
-  { key: "database", label: "Database", env: "SNOWFLAKE_DATABASE" },
-  { key: "warehouse", label: "Warehouse", env: "SNOWFLAKE_WAREHOUSE" },
-  { key: "role", label: "Role", env: "SNOWFLAKE_ROLE" },
-];
-
-function SnowflakeConfigPanel() {
-  const [expanded, setExpanded] = useState(false);
-  const [config, setConfig] = useState<SnowflakeConfig | null>(null);
-  const [loading, setLoading] = useState(false);
-  const [error, setError] = useState<string | null>(null);
-
-  // Editing state
-  const [editing, setEditing] = useState(false);
-  const [editValues, setEditValues] = useState<Record<string, string>>({});
-  const [newPassword, setNewPassword] = useState("");
-  const [showPassword, setShowPassword] = useState(false);
-  const [saving, setSaving] = useState(false);
-  const [saveSuccess, setSaveSuccess] = useState(false);
-
-  // Standalone password change (view mode)
-  const [changingPassword, setChangingPassword] = useState(false);
-  const [pwValue, setPwValue] = useState("");
-  const [showPwValue, setShowPwValue] = useState(false);
-  const [pwSaving, setPwSaving] = useState(false);
-  const [pwSuccess, setPwSuccess] = useState(false);
-  const [pwError, setPwError] = useState<string | null>(null);
-
-  const handlePasswordSave = async () => {
-    if (!pwValue.trim()) return;
-    setPwSaving(true);
-    setPwError(null);
-    try {
-      await updateSnowflakeConfig({ password: pwValue });
-      setPwSuccess(true);
-      setPwValue("");
-      setChangingPassword(false);
-      await loadConfig();
-      setTimeout(() => setPwSuccess(false), 3000);
-    } catch (err) {
-      setPwError(err instanceof Error ? err.message : "Failed to update password");
-    } finally {
-      setPwSaving(false);
-    }
-  };
-
-  const loadConfig = useCallback(async () => {
-    setLoading(true);
-    setError(null);
-    try {
-      const data = await getSnowflakeConfig();
-      setConfig(data);
-      // Pre-fill edit values
-      const values: Record<string, string> = {};
-      configFields.forEach((f) => { values[f.key] = data[f.key] || ""; });
-      setEditValues(values);
-    } catch (err) {
-      setError(err instanceof Error ? err.message : "Failed to load config");
-    } finally {
-      setLoading(false);
-    }
-  }, []);
-
-  useEffect(() => {
-    if (expanded && !config && !loading) loadConfig();
-  }, [expanded, config, loading, loadConfig]);
-
-  const handleSave = async () => {
-    if (!config) return;
-    setSaving(true);
-    setError(null);
-    setSaveSuccess(false);
-
-    // Build update payload — only changed fields
-    const updates: SnowflakeConfigUpdate = {};
-    configFields.forEach((f) => {
-      if (editValues[f.key] !== config[f.key]) {
-        (updates as Record<string, string>)[f.key] = editValues[f.key];
-      }
-    });
-    if (newPassword) updates.password = newPassword;
-
-    if (Object.keys(updates).length === 0) {
-      setEditing(false);
-      setSaving(false);
-      return;
-    }
-
-    try {
-      await updateSnowflakeConfig(updates);
-      setSaveSuccess(true);
-      setNewPassword("");
-      setEditing(false);
-      await loadConfig(); // Refresh
-      setTimeout(() => setSaveSuccess(false), 3000);
-    } catch (err) {
-      setError(err instanceof Error ? err.message : "Failed to save");
-    } finally {
-      setSaving(false);
-    }
-  };
-
-  const handleCancel = () => {
-    if (config) {
-      const values: Record<string, string> = {};
-      configFields.forEach((f) => { values[f.key] = config[f.key] || ""; });
-      setEditValues(values);
-    }
-    setNewPassword("");
-    setEditing(false);
-  };
-
-  const downloadEnv = () => {
-    if (!config) return;
-    const lines = configFields.map((f) => `${f.env}=${config[f.key] || ""}`);
-    lines.splice(2, 0, "SNOWFLAKE_PASSWORD="); // placeholder after user
-    const blob = new Blob([lines.join("\n")], { type: "text/plain" });
-    const url = URL.createObjectURL(blob);
-    const a = document.createElement("a");
-    a.href = url; a.download = "snowflake.env"; a.click();
-    URL.revokeObjectURL(url);
-  };
-
-  const downloadJson = () => {
-    if (!config) return;
-    const obj: Record<string, string> = {};
-    configFields.forEach((f) => { obj[f.env] = config[f.key] || ""; });
-    obj["SNOWFLAKE_PASSWORD"] = "";
-    const blob = new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" });
-    const url = URL.createObjectURL(blob);
-    const a = document.createElement("a");
-    a.href = url; a.download = "snowflake-credentials.json"; a.click();
-    URL.revokeObjectURL(url);
-  };
-
-  const copyAll = async () => {
-    if (!config) return;
-    const lines = configFields.map((f) => `${f.env}=${config[f.key] || ""}`);
-    lines.splice(2, 0, "SNOWFLAKE_PASSWORD=");
-    await navigator.clipboard.writeText(lines.join("\n"));
-  };
-
-  return (
-    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden md:col-span-2">
-      <button onClick={() => setExpanded(!expanded)} className="w-full flex items-center gap-4 p-5 hover:bg-gray-50 transition-colors text-left">
-        <div className="w-10 h-10 rounded-lg bg-gray-50 flex items-center justify-center flex-shrink-0">
-          <Database size={20} className="text-gray-500" />
-        </div>
-        <div className="flex-1">
-          <h3 className="text-base font-semibold text-gray-900">Snowflake Configuration</h3>
-          <p className="text-sm text-gray-500 mt-0.5">View and manage data warehouse connection settings</p>
-        </div>
-        {expanded ? <ChevronUp size={18} className="text-gray-400" /> : <ChevronDown size={18} className="text-gray-400" />}
-      </button>
-
-      {expanded && (
-        <div className="border-t border-gray-200 px-5 py-4">
-          {loading ? (
-            <div className="flex items-center justify-center py-8 text-gray-400">
-              <Loader2 size={20} className="animate-spin mr-2" /> Loading configuration...
-            </div>
-          ) : error && !config ? (
-            <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-700">{error}</div>
-          ) : config ? (
-            <>
-              {/* Config table */}
-              <div className="border border-gray-200 rounded-lg overflow-hidden mb-4">
-                <table className="min-w-full divide-y divide-gray-200">
-                  <thead className="bg-gray-50">
-                    <tr>
-                      <th className="px-4 py-2.5 text-left text-xs font-medium text-gray-500 uppercase w-44">Parameter</th>
-                      <th className="px-4 py-2.5 text-left text-xs font-medium text-gray-500 uppercase">Value</th>
-                      {!editing && <th className="px-4 py-2.5 text-right text-xs font-medium text-gray-500 uppercase w-16" />}
-                    </tr>
-                  </thead>
-                  <tbody className="divide-y divide-gray-100">
-                    {configFields.map((field) => (
-                      <tr key={field.key} className="hover:bg-gray-50">
-                        <td className="px-4 py-3">
-                          <div className="text-xs font-mono text-gray-400">{field.env}</div>
-                          <div className="text-sm font-medium text-gray-900">{field.label}</div>
-                        </td>
-                        <td className="px-4 py-3">
-                          {editing ? (
-                            <input
-                              type="text"
-                              value={editValues[field.key] || ""}
-                              onChange={(e) => setEditValues((prev) => ({ ...prev, [field.key]: e.target.value }))}
-                              className="w-full px-2.5 py-1.5 rounded border border-gray-200 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-[#0066FF] focus:border-transparent"
-                            />
-                          ) : (
-                            <span className="text-sm font-mono text-gray-700">{config[field.key] || "—"}</span>
-                          )}
-                        </td>
-                        {!editing && (
-                          <td className="px-4 py-3 text-right">
-                            <CopyButton text={config[field.key] || ""} />
-                          </td>
-                        )}
-                      </tr>
-                    ))}
-
-                    {/* Password row */}
-                    <tr className="hover:bg-gray-50">
-                      <td className="px-4 py-3">
-                        <div className="text-xs font-mono text-gray-400">SNOWFLAKE_PASSWORD</div>
-                        <div className="text-sm font-medium text-gray-900">Password</div>
-                      </td>
-                      <td className="px-4 py-3" colSpan={editing ? 1 : undefined}>
-                        {editing ? (
-                          <div className="relative">
-                            <input
-                              type={showPassword ? "text" : "password"}
-                              value={newPassword}
-                              onChange={(e) => setNewPassword(e.target.value)}
-                              placeholder={config.has_password ? "Leave blank to keep current" : "Enter password"}
-                              className="w-full px-2.5 py-1.5 pr-8 rounded border border-gray-200 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-[#0066FF] focus:border-transparent"
-                            />
-                            <button onClick={() => setShowPassword(!showPassword)} className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600">
-                              {showPassword ? <EyeOff size={14} /> : <Eye size={14} />}
-                            </button>
-                          </div>
-                        ) : changingPassword ? (
-                          <div className="flex items-center gap-2">
-                            <div className="relative flex-1">
-                              <input
-                                type={showPwValue ? "text" : "password"}
-                                value={pwValue}
-                                onChange={(e) => setPwValue(e.target.value)}
-                                onKeyDown={(e) => { if (e.key === "Enter") handlePasswordSave(); }}
-                                placeholder="Enter new password"
-                                autoFocus
-                                className="w-full px-2.5 py-1.5 pr-8 rounded border border-gray-200 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-[#0066FF] focus:border-transparent"
-                              />
-                              <button onClick={() => setShowPwValue(!showPwValue)} className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600">
-                                {showPwValue ? <EyeOff size={14} /> : <Eye size={14} />}
-                              </button>
-                            </div>
-                            <button
-                              onClick={handlePasswordSave}
-                              disabled={pwSaving || !pwValue.trim()}
-                              className="px-2.5 py-1.5 bg-[#0066FF] hover:bg-[#0052cc] text-white text-xs font-medium rounded transition-colors disabled:opacity-50"
-                            >
-                              {pwSaving ? <Loader2 size={12} className="animate-spin" /> : "Save"}
-                            </button>
-                            <button
-                              onClick={() => { setChangingPassword(false); setPwValue(""); setPwError(null); }}
-                              className="px-2.5 py-1.5 text-xs text-gray-500 hover:text-gray-700 rounded hover:bg-gray-100 transition-colors"
-                            >
-                              Cancel
-                            </button>
-                          </div>
-                        ) : (
-                          <span className="text-sm font-mono text-gray-500">
-                            {config.has_password ? "••••••••••••" : "Not set"}
-                          </span>
-                        )}
-                        {pwError && <p className="text-xs text-red-600 mt-1">{pwError}</p>}
-                        {pwSuccess && <p className="text-xs text-emerald-600 mt-1">Password updated</p>}
-                      </td>
-                      {!editing && !changingPassword && (
-                        <td className="px-4 py-3 text-right">
-                          <button
-                            onClick={() => setChangingPassword(true)}
-                            className="text-xs text-[#0066FF] hover:text-[#0052cc] font-medium transition-colors"
-                          >
-                            Change
-                          </button>
-                        </td>
-                      )}
-                      {!editing && changingPassword && <td />}
-                    </tr>
-                  </tbody>
-                </table>
-              </div>
-
-              {/* Error / Success */}
-              {error && <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-700 mb-3">{error}</div>}
-              {saveSuccess && <div className="bg-emerald-50 border border-emerald-200 rounded-lg p-3 text-sm text-emerald-700 mb-3">Configuration updated. Snowflake connection has been refreshed.</div>}
-
-              {/* Actions */}
-              <div className="flex items-center gap-2">
-                {editing ? (
-                  <>
-                    <button
-                      onClick={handleSave}
-                      disabled={saving}
-                      className="flex items-center gap-1.5 px-4 py-2 bg-[#0066FF] hover:bg-[#0052cc] text-white text-sm font-medium rounded-lg transition-colors disabled:opacity-50"
-                    >
-                      {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
-                      {saving ? "Saving..." : "Save Changes"}
-                    </button>
-                    <button onClick={handleCancel} className="flex items-center gap-1.5 px-4 py-2 text-sm text-gray-600 border border-gray-200 rounded-lg hover:bg-gray-50 transition-colors">
-                      <X size={14} /> Cancel
-                    </button>
-                  </>
-                ) : (
-                  <>
-                    <button
-                      onClick={() => setEditing(true)}
-                      className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-gray-600 border border-gray-200 rounded-lg hover:border-[#0066FF]/30 hover:text-[#0066FF] transition-colors"
-                    >
-                      <Pencil size={13} /> Edit Configuration
-                    </button>
-                    <button onClick={copyAll} className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-gray-600 border border-gray-200 rounded-lg hover:border-[#0066FF]/30 hover:text-[#0066FF] transition-colors">
-                      <Copy size={13} /> Copy All
-                    </button>
-                    <button onClick={downloadEnv} className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-gray-600 border border-gray-200 rounded-lg hover:border-[#0066FF]/30 hover:text-[#0066FF] transition-colors">
-                      <Download size={13} /> .env
-                    </button>
-                    <button onClick={downloadJson} className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-gray-600 border border-gray-200 rounded-lg hover:border-[#0066FF]/30 hover:text-[#0066FF] transition-colors">
-                      <Download size={13} /> JSON
-                    </button>
-                  </>
-                )}
-              </div>
-
-              <p className="text-xs text-gray-400 mt-3">
-                Changes take effect immediately. The backend will reconnect to Snowflake with the new credentials on the next query.
-              </p>
-            </>
-          ) : null}
-        </div>
-      )}
-    </div>
-  );
-}
-
 // ── Settings Page ─────────────────────────────────────────
 
 export default function SettingsPage() {
+  const { isAdmin } = useAuth();
   return (
     <div className="max-w-7xl mx-auto px-4 sm:px-6 py-8">
       <div className="mb-8">
@@ -399,29 +45,28 @@ export default function SettingsPage() {
           );
         })}
 
-        {/* Metric Definitions — clickable card linking to the overlay editor */}
-        <Link
-          href="/admin/definitions"
-          className="bg-white rounded-xl border border-gray-200 p-5 hover:shadow-lg hover:border-[#0066FF]/30 transition-all cursor-pointer group block"
-        >
-          <div className="flex items-start gap-4">
-            <div className="w-10 h-10 rounded-lg bg-gray-50 flex items-center justify-center flex-shrink-0 group-hover:bg-[#0066FF]/10 transition-colors">
-              <Layers size={20} className="text-gray-500 group-hover:text-[#0066FF] transition-colors" />
-            </div>
-            <div>
-              <h3 className="text-base font-semibold text-gray-900 group-hover:text-[#0066FF] transition-colors">
-                Metric Definitions
-              </h3>
-              <p className="text-sm text-gray-500 mt-1">
-                Override Blackboard&apos;s canonical metric definitions with
-                your institution&apos;s own — retention windows, FTE divisor,
-                completion-rate filters. Edits go live immediately.
-              </p>
+        {isAdmin && (
+          <Link
+            href="/admin/definitions"
+            className="bg-white rounded-xl border border-gray-200 p-5 hover:shadow-lg hover:border-[#0066FF]/30 transition-all cursor-pointer group block"
+          >
+            <div className="flex items-start gap-4">
+              <div className="w-10 h-10 rounded-lg bg-gray-50 flex items-center justify-center flex-shrink-0 group-hover:bg-[#0066FF]/10 transition-colors">
+                <Layers size={20} className="text-gray-500 group-hover:text-[#0066FF] transition-colors" />
+              </div>
+              <div>
+                <h3 className="text-base font-semibold text-gray-900 group-hover:text-[#0066FF] transition-colors">
+                  Metric Definitions
+                </h3>
+                <p className="text-sm text-gray-500 mt-1">
+                  Override Blackboard&apos;s canonical metric definitions with
+                  your institution&apos;s own — retention windows, FTE divisor,
+                  completion-rate filters. Edits go live immediately.
+                </p>
+              </div>
             </div>
-          </div>
-        </Link>
-
-        <SnowflakeConfigPanel />
+          </Link>
+        )}
       </div>
     </div>
   );
diff --git a/src/components/NavDrawer.tsx b/src/components/NavDrawer.tsx
index 60cc92b..86d9cb5 100644
--- a/src/components/NavDrawer.tsx
+++ b/src/components/NavDrawer.tsx
@@ -20,12 +20,14 @@ import {
 } from "lucide-react";
 import { SnowflakeLogo } from "./SnowflakeLogo";
 import { BrandLogo } from "./BrandLogo";
+import { useAuth } from "@/context/AuthContext";
 
 interface NavSection {
   label: string;
   href: string;
   icon: React.ElementType;
   children?: { label: string; href: string }[];
+  adminOnly?: boolean;
   external?: boolean;
 }
 
@@ -46,7 +48,7 @@ const navSections: NavSection[] = [
     ],
   },
   { label: "Data Dictionary", href: "/developer", icon: BookOpen },
-  { label: "Metric Definitions", href: "/admin/definitions", icon: Layers },
+  { label: "Metric Definitions", href: "/admin/definitions", icon: Layers, adminOnly: true },
   { label: "Settings", href: "/settings", icon: Settings },
   {
     label: "Privacy & Security",
@@ -69,6 +71,7 @@ export function NavDrawer({
   onClose: () => void;
 }) {
   const pathname = usePathname();
+  const { isAdmin } = useAuth();
   const [expanded, setExpanded] = useState<Record<string, boolean>>({});
 
   useEffect(() => {
@@ -127,7 +130,7 @@ export function NavDrawer({
 
         {/* Nav Items */}
         <nav className="flex-1 overflow-y-auto py-3">
-          {navSections.map((section) => {
+          {navSections.filter((section) => isAdmin || !section.adminOnly).map((section) => {
             const Icon = section.icon;
             const active = isActive(section.href);
             const isExpanded = expanded[section.label];
diff --git a/src/context/AuthContext.tsx b/src/context/AuthContext.tsx
index f8b7375..79948e3 100644
--- a/src/context/AuthContext.tsx
+++ b/src/context/AuthContext.tsx
@@ -6,6 +6,7 @@ import { authService } from "@/services/authService";
 interface AuthContextType {
   isAuthenticated: boolean;
   user: { id: string; name: string; email?: string } | null;
+  isAdmin: boolean;
   login: (username: string, password: string) => Promise<void>;
   signOut: () => void;
   isLoading: boolean;
@@ -93,12 +94,14 @@ function LoginPage({ onLogin, error, loading }: {
 export function AuthProvider({ children }: { children: React.ReactNode }) {
   const [isAuthenticated, setIsAuthenticated] = useState(false);
   const [user, setUser] = useState<AuthContextType["user"]>(null);
+  const [isAdmin, setIsAdmin] = useState(false);
   const [isLoading, setIsLoading] = useState(true);
   const [error, setError] = useState<string | null>(null);
 
   useEffect(() => {
     setIsAuthenticated(authService.isAuthenticated());
     setUser(authService.getUser());
+    setIsAdmin(authService.isAdmin());
     setIsLoading(false);
   }, []);
 
@@ -108,6 +111,7 @@ export function AuthProvider({ children }: { children: React.ReactNode }) {
     try {
       const u = await authService.login(username, password);
       setUser(u);
+      setIsAdmin(authService.isAdmin());
       setIsAuthenticated(true);
     } catch (err) {
       setError(err instanceof Error ? err.message : "Login failed");
@@ -120,10 +124,11 @@ export function AuthProvider({ children }: { children: React.ReactNode }) {
     authService.logout();
     setIsAuthenticated(false);
     setUser(null);
+    setIsAdmin(false);
   }, []);
 
   return (
-    <AuthContext.Provider value={{ isAuthenticated, user, login, signOut, isLoading, error }}>
+    <AuthContext.Provider value={{ isAuthenticated, user, isAdmin, login, signOut, isLoading, error }}>
       {/* Always render children so the router stays mounted and knows the URL */}
       {children}
 
diff --git a/src/services/authService.ts b/src/services/authService.ts
index 58e1df7..8b668c4 100644
--- a/src/services/authService.ts
+++ b/src/services/authService.ts
@@ -10,6 +10,8 @@ interface AuthState {
 }
 
 const STORAGE_KEY = "illuminate_auth";
+/** Cognito group whose members may edit metric overlays; the API enforces the same group. */
+export const ADMIN_GROUP = "illuminate-admins";
 const SESSION_MAX_AGE_MS = 4 * 60 * 60 * 1000; // 4 hours
 
 /** Decode a JWT payload without verification (just to read exp). */
@@ -144,6 +146,13 @@ class AuthService {
     });
   }
 
+  /** Read from the current ID token's cognito:groups claim, so it can't go stale in storage. */
+  isAdmin(): boolean {
+    const payload = this.state.token ? decodeJwtPayload(this.state.token) : null;
+    const groups = payload?.["cognito:groups"];
+    return Array.isArray(groups) && groups.includes(ADMIN_GROUP);
+  }
+
   isAuthenticated(): boolean {
     if (!this.state.isAuthenticated || !this.state.token) return false;
     if (this.state.loginTimestamp && Date.now() - this.state.loginTimestamp > SESSION_MAX_AGE_MS) {
diff --git a/src/services/configApi.ts b/src/services/configApi.ts
deleted file mode 100644
index c6d3cd1..0000000
--- a/src/services/configApi.ts
+++ /dev/null
@@ -1,52 +0,0 @@
-"use client";
-
-import { authService } from "./authService";
-
-const API_URL = process.env.NEXT_PUBLIC_AGENT_API_URL || "http://localhost:8000";
-
-export interface SnowflakeConfig {
-  account: string;
-  user: string;
-  has_password: boolean;
-  database: string;
-  warehouse: string;
-  role: string;
-}
-
-export interface SnowflakeConfigUpdate {
-  account?: string;
-  user?: string;
-  password?: string;
-  database?: string;
-  warehouse?: string;
-  role?: string;
-}
-
-export async function getSnowflakeConfig(): Promise<SnowflakeConfig> {
-  const token = await authService.getValidToken();
-  const resp = await fetch(`${API_URL}/api/v1/config/snowflake`, {
-    headers: {
-      "Content-Type": "application/json",
-      ...(token ? { Authorization: `Bearer ${token}` } : {}),
-    },
-  });
-  if (!resp.ok) throw new Error(`Failed to load config (${resp.status})`);
-  return resp.json();
-}
-
-export async function updateSnowflakeConfig(updates: SnowflakeConfigUpdate): Promise<{ success: boolean; updated_fields: string[] }> {
-  const token = await authService.getValidToken();
-  const resp = await fetch(`${API_URL}/api/v1/config/snowflake`, {
-    method: "PUT",
-    headers: {
-      "Content-Type": "application/json",
-      ...(token ? { Authorization: `Bearer ${token}` } : {}),
-    },
-    body: JSON.stringify(updates),
-  });
-  if (!resp.ok) {
-    const text = await resp.text();
-    throw new Error(`Failed to update config (${resp.status}): ${text}`);
-  }
-  return resp.json();
-}
```

- [ ] **Step 3: Verify**

Run: `npx tsc --noEmit`
Expected: no output.

- [ ] **Step 4: Verify**

Run: `npm run build`
Expected: exits 0; static export written to `out/`.

- [ ] **Step 5: Commit, open the PR, merge**

```bash
git add src/services/authService.ts src/context/AuthContext.tsx src/components/NavDrawer.tsx src/app/settings/page.tsx src/app/admin/definitions/page.tsx src/services/configApi.ts
git commit -m "feat: admin-only UI for metric definitions; remove the non-functional Snowflake settings editor

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin feat/admin-only-ui
gh pr create --base main --title "feat: admin-only UI for metric definitions; remove the non-functional Snowflake settings editor" --body "$(cat <<'EOF'
**Claude:** Phase 3h (frontend half of 3b). `isAdmin` is read from the ID token's `cognito:groups` claim, the same group the API enforces. The Metric Definitions nav item and Settings card show only for admins, and `/admin/definitions` shows an access message to everyone else. The Settings "Snowflake Configuration" panel is removed: it called `/api/v1/config/snowflake`, which the backend has never implemented, so it could only fail. `configApi.ts` goes with it. This repo has no test framework, so verification is `tsc` plus `npm run build`. A browser check needs a Cognito login.

Reviewable surface: 6 files, +61/-434 lines (mostly the removed editor).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge feat/admin-only-ui --squash --delete-branch
```
