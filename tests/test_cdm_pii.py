import pytest

import snowflake_client
from semantic_layer.catalog import load_catalog
from semantic_layer.chat_tools import ChatTools
from tests.tenant_fakes import TENANT_A


@pytest.fixture
def executed(monkeypatch):
    calls = []
    monkeypatch.setattr(snowflake_client, "query_sql", lambda sql, params=None, **k: calls.append(sql) or {"columns": [], "rows": []})
    return calls


def _validate(sql, **kwargs):
    return snowflake_client.validate_and_execute(sql, tenant=TENANT_A, **kwargs)


def _refused_as_pii(result):
    return "personally identifiable" in result.get("error", "")


@pytest.mark.parametrize("sql", [
    "SELECT USER_NAME FROM CDM_CLB.PERSON",
    "SELECT p.BIRTH_DATE FROM CDM_LMS.PERSON p",
    "SELECT p.POSTAL_CODE FROM CDM_LMS.PERSON p",
    "SELECT p.SOURCE_ID FROM CDM_LMS.PERSON p",
    "SELECT p.BIRTH_DATE, p.POSTAL_CODE, p.SOURCE_ID FROM CDM_LMS.PERSON p",
    "SELECT c.BIRTH_DATE FROM CDM_CRM.CONTACT c",
    "SELECT c.GENDER_SOURCE_DESCRIPTION, COUNT(*) AS n FROM CDM_CRM.CONTACT c GROUP BY 1",
    "SELECT c.ETHNIC_GROUP_SOURCE_CODE FROM CDM_CRM.CONTACT c",
    "SELECT r.RACE_SOURCE_DESCRIPTION FROM CDM_CRM.CONTACT_RACE r",
    "SELECT p.STAGE:email FROM CDM_LMS.PERSON p",
    "SELECT p.STAGE:email::string AS e FROM CDM_LMS.PERSON p",
    "SELECT p.STAGE['email'] AS e FROM CDM_LMS.PERSON p",
    "SELECT GET_PATH(p.STAGE, 'email') AS e FROM CDM_LMS.PERSON p",
    "SELECT \"USER_NAME\" FROM CDM_CLB.PERSON",
    "WITH x AS (SELECT p.BIRTH_DATE AS d FROM CDM_LMS.PERSON p) SELECT d FROM x",
    "SELECT d FROM (SELECT BIRTH_DATE AS d FROM CDM_LMS.PERSON)",
    "SELECT c.NAME, p.BIRTH_DATE FROM CDM_LMS.COURSE c JOIN CDM_LMS.PERSON p ON p.ID = c.ID",
    "SELECT MIN(p.BIRTH_DATE) FROM CDM_LMS.PERSON p",
    "SELECT e.DATA FROM CDM_TLM.ULTRA_EVENTS e",
])
def test_dictionary_flagged_cdm_columns_are_refused_outside_count(executed, sql):
    assert _refused_as_pii(_validate(sql)), sql
    assert executed == []


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(DISTINCT USER_NAME) AS n FROM CDM_CLB.PERSON",
    "SELECT COUNT(p.BIRTH_DATE) AS n, COUNT(DISTINCT p.POSTAL_CODE) AS z FROM CDM_LMS.PERSON p",
    "SELECT COUNT(p.STAGE:email) AS n FROM CDM_LMS.PERSON p",
    "SELECT COUNT_IF(c.GENDER_SOURCE_CODE = 'F') AS n FROM CDM_CRM.CONTACT c",
    "SELECT COUNT(*) AS n FROM CDM_LMS.PERSON p WHERE p.BIRTH_DATE < '2000-01-01'",
])
def test_counting_dictionary_flagged_cdm_columns_is_allowed(executed, sql):
    assert "error" not in _validate(sql), sql
    assert len(executed) == 1


@pytest.mark.parametrize("sql", [
    "SELECT p.INSTITUTION_ROLE, COUNT(*) AS n FROM CDM_LMS.PERSON p GROUP BY 1",
    "SELECT c.DESCRIPTION FROM CDM_LMS.COURSE c",
    "SELECT c.NAME, c.COURSE_NUMBER FROM CDM_LMS.COURSE c",
])
def test_cdm_columns_the_dictionary_does_not_flag_are_allowed(executed, sql):
    assert "error" not in _validate(sql), sql
    assert len(executed) == 1


def test_compiled_sql_keeps_its_own_rules(executed):
    assert "error" not in _validate("SELECT p.BIRTH_DATE AS b FROM CDM_LMS.PERSON p", compiled=True)


def test_unresolvable_column_a_referenced_cdm_table_flags_is_pii(monkeypatch, executed):
    monkeypatch.setattr(snowflake_client, "_resolve", lambda col, scope: None)
    assert _refused_as_pii(_validate("SELECT BIRTH_DATE FROM CDM_LMS.PERSON"))
    assert "error" not in _validate("SELECT INSTITUTION_ROLE FROM CDM_LMS.PERSON")


def test_resolver_failure_is_a_pii_refusal(monkeypatch, executed):
    def boom(*a, **k):
        raise RuntimeError("resolver failure")
    monkeypatch.setattr(snowflake_client, "_resolve", boom)
    assert _refused_as_pii(_validate("SELECT c.NAME FROM CDM_LMS.COURSE c"))
    assert executed == []


def test_chat_execute_passes_every_dataset_pii_column(monkeypatch):
    seen = []
    monkeypatch.setattr(snowflake_client, "validate_and_execute",
                        lambda sql, params=None, **kw: seen.append(kw) or {"columns": [], "rows": []})
    catalog = load_catalog()
    ChatTools(catalog, "DB", tenant=TENANT_A).dispatch(
        "execute_sql", {"sql": "SELECT COUNT(*) AS n FROM CDM_LMS.COURSE", "reason": "no governed metric fits this"},
        called=("search_catalog",))
    declared = {c.upper() for ds in catalog.datasets.values() for c in ds.pii_columns}
    assert declared and seen and set(seen[0]["extra_pii"]) == declared
