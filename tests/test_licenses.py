import pytest

import licenses
from tests.tenant_fakes import TENANT_A, TENANT_CORE


def test_licenses_are_core_and_premium():
    assert licenses.LICENSES == frozenset({"core", "premium"})


def test_refusal_texts():
    assert licenses.LEARN_REFUSAL == "This query uses LEARN data, which is available with Illuminate Premium."
    assert licenses.PREVIEW_REFUSAL == "Data preview for LEARN tables is available with Illuminate Premium"


def test_allows_learn_only_for_premium():
    assert licenses.allows_learn(TENANT_A)
    assert not licenses.allows_learn(TENANT_CORE)
    assert not licenses.allows_learn(None)


@pytest.mark.parametrize("schema", ["CDM_LMS", "cdm_sis", "INFORMATION_SCHEMA", "information_schema"])
def test_cdm_and_information_schema_always_allowed(schema):
    assert licenses.schema_allowed(TENANT_CORE, schema)
    assert licenses.schema_allowed(TENANT_A, schema)


@pytest.mark.parametrize("schema", ["LEARN", "learn", "Learn"])
def test_learn_schema_is_premium_only(schema):
    assert licenses.schema_allowed(TENANT_A, schema)
    assert not licenses.schema_allowed(TENANT_CORE, schema)


@pytest.mark.parametrize("tenant", [TENANT_A, TENANT_CORE])
def test_other_schemas_never_allowed(tenant):
    assert not licenses.schema_allowed(tenant, "PUBLIC")
