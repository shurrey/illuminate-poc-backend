import json
import logging

import pytest
from fastapi.testclient import TestClient

import lambda_handler
import snowflake_client
from roles import role_allows_identity, role_of
from semantic_layer.catalog import load_catalog
from semantic_layer.catalog_view import public_catalog
from semantic_layer.chat_tools import ChatTools
from semantic_layer.compiler import CompileError, compile_query
from semantic_layer.contract import QueryContract

AUTH = {"Authorization": "Bearer test"}
NAMED = {"measures": ["dataset.course_student_activity.v1:students"], "dimensions": ["person_email"]}


def user(*groups):
    return {"sub": "u1", "cognito:groups": list(groups)}


@pytest.mark.parametrize("groups, role", [
    ((), "viewer"),
    (("illuminate-admins",), "admin"),
    (("illuminate-authors",), "author"),
    (("illuminate-developers",), "developer"),
    (("illuminate-authors", "illuminate-admins"), "admin"),
    (("someone-elses-group",), "viewer"),
])
def test_role_of_maps_groups_to_roles(groups, role):
    assert role_of(user(*groups)) == role


def test_only_viewers_are_denied_identity():
    assert [role_allows_identity(user(*g)) for g in [(), ("illuminate-admins",), ("illuminate-authors",), ("illuminate-developers",)]] \
        == [False, True, True, True]


def test_the_compiler_selects_identity_only_when_allowed():
    contract = QueryContract(**NAMED)
    with pytest.raises(CompileError, match="personally identifiable"):
        compile_query(contract, load_catalog(), "DB")
    assert "PERSON_EMAIL" in compile_query(contract, load_catalog(), "DB", allow_identity=True).sql


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_DATABASE", "TESTDB")
    monkeypatch.setattr(snowflake_client, "query_sql",
                        lambda sql, params=None: {"columns": ["PERSON_EMAIL", "STUDENTS"], "rows": []})
    return TestClient(lambda_handler.app)


def test_viewers_cannot_select_identity_through_the_api(client, monkeypatch):
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user())
    assert client.post("/api/v1/semantic/query", headers=AUTH, json=NAMED).status_code == 400


def test_authors_select_identity_and_the_query_is_logged(client, monkeypatch, caplog):
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user("illuminate-authors"))
    with caplog.at_level(logging.INFO, logger="API-PROXY"):
        assert client.post("/api/v1/semantic/query", headers=AUTH, json=NAMED).status_code == 200
    [record] = [r for r in caplog.records if r.getMessage().startswith("identity_query")]
    assert "sub=u1" in record.getMessage() and "person_email" in record.getMessage()


def test_unnamed_queries_are_not_logged_as_identity(client, monkeypatch, caplog):
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: user("illuminate-authors"))
    with caplog.at_level(logging.INFO, logger="API-PROXY"):
        client.post("/api/v1/semantic/query", headers=AUTH, json={"measures": NAMED["measures"]})
    assert not [r for r in caplog.records if r.getMessage().startswith("identity_query")]


def test_the_catalog_marks_identity_selectable_by_role():
    def selectable(allow):
        ds = next(d for d in public_catalog(load_catalog(), allow_identity=allow)["datasets"]
                  if d["id"] == "dataset.course_student_activity.v1")
        return next(d for d in ds["dimensions"] if d["name"] == "person_email")["selectable"]
    assert (selectable(False), selectable(True)) == (False, True)


def test_chat_never_selects_identity():
    tools = ChatTools(load_catalog(), "DB", execute=lambda *a, **k: {"columns": [], "rows": []})
    out = tools.dispatch("query_semantic", NAMED)
    assert "personally identifiable" in json.dumps(out.content)
