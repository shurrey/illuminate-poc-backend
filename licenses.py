"""License tiers and what each may query. Core sees CDM_* only; Premium also sees LEARN."""

from __future__ import annotations

LICENSES = frozenset({"core", "premium"})

LEARN_REFUSAL = "This query uses LEARN data, which is available with Illuminate Premium."
PREVIEW_REFUSAL = "Data preview for LEARN tables is available with Illuminate Premium"


def allows_learn(tenant) -> bool:
    return tenant is not None and tenant.license == "premium"


def schema_allowed(tenant, schema: str) -> bool:
    """Case-insensitive; CDM_* and INFORMATION_SCHEMA for every license, LEARN for premium only."""
    name = schema.upper()
    if name.startswith("CDM_") or name == "INFORMATION_SCHEMA":
        return True
    return name == "LEARN" and allows_learn(tenant)
