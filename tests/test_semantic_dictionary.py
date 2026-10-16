import io
import json

import pytest

from semantic_layer import dictionary

DEFINITIONS = [
    {"name": "CDM_LMS.PERSON.EMAIL", "columnDataType": "TEXT", "text": "Email",
     "technicalSpecifications": [{"isPii": True}]},
    {"name": "CDM_LMS.PERSON.ID", "columnDataType": "NUMBER", "technicalSpecifications": []},
    {"name": "LEARN.USERS.EMAIL", "columnDataType": "TEXT"},
    {"name": "CDM_LMS.PERSON", "text": "table-level entry"},
]


@pytest.fixture(autouse=True)
def fresh_cache():
    dictionary._tables.cache_clear()
    yield
    dictionary._tables.cache_clear()


def test_columns_come_from_definitions_with_pii_flags(monkeypatch):
    monkeypatch.setattr(dictionary.urllib.request, "urlopen",
                        lambda url, timeout: io.BytesIO(json.dumps(DEFINITIONS).encode()))
    cols = dictionary.describe_table("cdm_lms", "person")
    assert cols == [
        {"name": "EMAIL", "type": "TEXT", "pii": True, "description": "Email"},
        {"name": "ID", "type": "NUMBER", "pii": False, "description": ""},
    ]
    assert dictionary.describe_table("CDM_LMS", "NO_SUCH_TABLE") is None


def test_learn_comes_from_the_snapshot_without_the_network(monkeypatch):
    def down(url, timeout):
        raise AssertionError("LEARN must not touch the network")

    monkeypatch.setattr(dictionary.urllib.request, "urlopen", down)
    cols = {c["name"]: c for c in dictionary.describe_table("learn", "users")}
    assert cols["B_FAX"]["pii"] is True and cols["B_FAX"]["description"] == ""
    assert dictionary.describe_table("LEARN", "NO_SUCH_TABLE") is None


def test_unreachable_dictionary_returns_none(monkeypatch):
    def down(url, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(dictionary.urllib.request, "urlopen", down)
    assert dictionary.describe_table("CDM_LMS", "PERSON") is None


def test_a_failed_dictionary_fetch_is_not_retried_immediately(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise OSError("down")

    dictionary._tables.cache_clear()
    monkeypatch.setattr(dictionary, "_failed_at", None, raising=False)
    monkeypatch.setattr(dictionary.urllib.request, "urlopen", boom)
    assert dictionary.describe_table("CDM_LMS", "COURSE") is None
    assert dictionary.describe_table("CDM_LMS", "COURSE") is None
    assert len(calls) == 1
