import pytest

from roles import has_access, role_of
from tests.test_mcp_audience import AUTH, SWEEP, _no_backend, mcp_user, world  # noqa: F401

NO_ACCESS = {"detail": "You don't have access to Illuminate", "code": "NO_ACCESS"}


def web_user(*groups, tenant_id="tb"):
    return {"sub": "u1", "custom:tenant_id": tenant_id, "cognito:groups": list(groups)}


@pytest.mark.parametrize("groups, role", [
    (("illuminate-users",), "viewer"),
    (("illuminate-users", "platform-system-admins"), "admin"),
    (("illuminate-users", "platform-program-admins"), "author"),
    (("illuminate-users", "platform-program-admins", "illuminate-admins"), "admin"),
    (("illuminate-users", "platform-program-admins", "platform-system-admins"), "admin"),
])
def test_platform_groups_map_to_illuminate_roles(groups, role):
    assert role_of(web_user(*groups)) == role


@pytest.mark.parametrize("groups, allowed", [
    ((), False),
    (("illuminate-admins",), False),
    (("platform-system-admins",), False),
    (("ai-first-lms-users",), False),
    (("illuminate-users",), True),
])
def test_only_the_access_group_grants_access(groups, allowed):
    assert has_access(web_user(*groups)) is allowed


def test_a_token_without_groups_claim_has_no_access():
    assert has_access({"sub": "u1", "custom:tenant_id": "tb"}) is False


@pytest.mark.parametrize("method,path", SWEEP)
def test_every_route_refuses_a_web_user_without_access_before_any_backend_call(world, monkeypatch, method, path):
    world.as_user(web_user("illuminate-admins", "platform-system-admins"))
    calls = _no_backend(monkeypatch)
    kw = {"json": {}} if method in ("POST", "PUT") else {}
    r = world.client.request(method, path, headers=AUTH, **kw)
    assert (r.status_code, r.json()) == (403, NO_ACCESS)
    assert calls == []


@pytest.mark.parametrize("method,path", SWEEP)
def test_every_route_refuses_an_mcp_user_without_access(world, monkeypatch, method, path):
    user = mcp_user("tb", "illuminate-admins")
    user["cognito:groups"].remove("illuminate-users")
    world.as_user(user)
    calls = _no_backend(monkeypatch)
    kw = {"json": {}} if method in ("POST", "PUT") else {}
    r = world.client.request(method, path, headers=AUTH, **kw)
    assert (r.status_code, r.json()) == (403, NO_ACCESS)
    assert calls == [] and world.sessions.calls == []


def test_health_needs_no_token(world):
    assert world.client.get("/health").status_code == 200


def test_me_without_a_token_is_still_401(world):
    assert world.client.get("/api/v1/me").status_code == 401


def test_access_alone_is_a_viewer(world):
    world.as_user(web_user("illuminate-users"))
    body = world.client.get("/api/v1/me", headers=AUTH).json()
    assert (body["roles"], body["is_admin"]) == (["viewer"], False)


def test_system_admins_are_illuminate_admins(world):
    world.as_user(web_user("illuminate-users", "platform-system-admins"))
    body = world.client.get("/api/v1/me", headers=AUTH).json()
    assert (body["roles"], body["is_admin"]) == (["admin"], True)
    assert world.client.get("/api/v1/admin/settings/mcp", headers=AUTH).json() == {"enabled": True}


def test_program_admins_are_illuminate_authors(world):
    world.as_user(web_user("illuminate-users", "platform-program-admins"))
    body = world.client.get("/api/v1/me", headers=AUTH).json()
    assert (body["roles"], body["is_admin"]) == (["author"], False)
    assert world.client.get("/api/v1/admin/settings/mcp", headers=AUTH).status_code == 403

