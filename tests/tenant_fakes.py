"""Tenant fixtures for route tests: two registered tenants and helpers to install them."""
import snowflake_client
import tenancy
from tenancy import Tenant, TenantNotReady

TENANT_A = Tenant("tenant-a", "Alpha University", "Premium", False)
TENANT_B = Tenant("tenant-b", "Beta College", "Premium", False)


def install_tenants(monkeypatch, *tenants, with_databases=True, database=None):
    """Make `tenancy.get_tenant` resolve only the given tenants, each with database DB_<TENANT_ID>
    (hyphens as underscores), or `database` for every tenant.

    with_databases=False leaves `snowflake_client.tenant_database` real, for tests of the secret path.
    """
    by_id = {t.tenant_id: t for t in tenants}

    def fake_get_tenant(tenant_id):
        if tenant_id not in by_id:
            raise TenantNotReady(tenant_id)
        return by_id[tenant_id]

    def fake_tenant_database(tenant):
        if tenant is None or tenant.tenant_id not in by_id:
            raise TenantNotReady(tenant.tenant_id if tenant else "no tenant")
        return database or "DB_" + tenant.tenant_id.replace("-", "_").upper()

    monkeypatch.setattr(tenancy, "get_tenant", fake_get_tenant)
    if with_databases:
        monkeypatch.setattr(snowflake_client, "tenant_database", fake_tenant_database)


def user_of(tenant, *groups, sub="u1"):
    """A decoded-token user belonging to the tenant, in the given Cognito groups."""
    return {"sub": sub, "custom:tenant_id": tenant.tenant_id, "cognito:groups": list(groups)}
