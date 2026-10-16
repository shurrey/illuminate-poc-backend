"""Tenant fixtures for route tests: two registered tenants and helpers to install them."""
import tenancy
from tenancy import Tenant, TenantNotReady

TENANT_A = Tenant("tenant-a", "Alpha University", "Premium", False)
TENANT_B = Tenant("tenant-b", "Beta College", "Premium", False)


def install_tenants(monkeypatch, *tenants):
    """Make `tenancy.get_tenant` resolve only the given tenants."""
    by_id = {t.tenant_id: t for t in tenants}

    def fake_get_tenant(tenant_id):
        if tenant_id not in by_id:
            raise TenantNotReady(tenant_id)
        return by_id[tenant_id]

    monkeypatch.setattr(tenancy, "get_tenant", fake_get_tenant)


def user_of(tenant, *groups, sub="u1"):
    """A decoded-token user belonging to the tenant, in the given Cognito groups."""
    return {"sub": sub, "custom:tenant_id": tenant.tenant_id, "cognito:groups": list(groups)}
