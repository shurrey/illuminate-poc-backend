import dataclasses

from semantic_layer.catalog import load_catalog
import snowflake_client
from semantic_layer.chat_tools import MODEL_ROW_LIMIT, ChatTools
from snowflake_client import Compiled, Result, Spliced
from licenses import LEARN_REFUSAL
from tests.tenant_fakes import TENANT_A, TENANT_B, TENANT_CORE

CATALOG = load_catalog()


class FakeWarehouse:
    """Stands in for snowflake_client.execute: records each statement the guard accepted and its context."""

    def __init__(self, rows=None, error=None):
        self.rows, self.error, self.sql, self.validated, self.ctx = (
            rows if rows is not None else [{"N": 1}], error, [], [], [])

    def __call__(self, v, ctx):
        self.sql.append(v.sql)
        self.validated.append(v)
        self.ctx.append(ctx)
        if self.error:
            return Result(columns=[], rows=[], error=self.error)
        return Result(columns=list(self.rows[0]) if self.rows else [], rows=self.rows)


SEARCHED = ("search_catalog",)


def _tools(warehouse=None):
    return ChatTools(CATALOG, tenant=TENANT_A, execute=warehouse or FakeWarehouse())


def test_specs_name_every_tool_once():
    names = [s["name"] for s in _tools().specs]
    assert names == sorted(set(names), key=names.index)
    assert {"search_catalog", "query_semantic"} <= set(names)


def test_search_catalog_returns_matches():
    out = _tools().dispatch("search_catalog", {"question": "average grade"})
    assert out.artifacts == []
    assert out.content["matches"][0]["id"] == "metric.average_grade.v1"


def test_query_semantic_runs_compiled_sql_and_returns_provenance():
    wh = FakeWarehouse([{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3}])
    out = _tools(wh).dispatch("query_semantic", {"metrics": ["metric.reportable_courses.v1"], "dimensions": ["term_name"],
                                                 "title": "Courses by term"})
    assert wh.sql and "DS_COURSE_FILTERS_V1" in wh.sql[0]
    assert out.content["rows"] == [{"term_name": "Fall", "reportable_courses": 3}]
    assert out.content["provenance"]["governed"] is True
    table = next(a for a in out.artifacts if a["type"] == "table")
    assert table["title"] == "Courses by term"
    assert table["data"] == {"columns": ["term_name", "reportable_courses"], "rows": [{"term_name": "Fall", "reportable_courses": 3}]}
    assert table["query"]["metrics"] == ["metric.reportable_courses.v1"]
    assert table["sql"] == wh.sql[0]
    assert any(a["type"] == "sql" and a["data"] == wh.sql[0] for a in out.artifacts)


def test_model_sees_at_most_the_row_limit_but_the_artifact_has_every_row():
    rows = [{"N": i} for i in range(MODEL_ROW_LIMIT + 50)]
    out = _tools(FakeWarehouse(rows)).dispatch("query_semantic", {"metrics": ["metric.reportable_courses.v1"], "limit": 1000})
    assert len(out.content["rows"]) == MODEL_ROW_LIMIT
    assert out.content["truncated"] is True and out.content["row_count"] == len(rows)
    assert len(next(a for a in out.artifacts if a["type"] == "table")["data"]["rows"]) == len(rows)


def test_invalid_contract_is_reported_to_the_model_and_nothing_runs():
    wh = FakeWarehouse()
    out = _tools(wh).dispatch("query_semantic", {"metrics": ["metric.nope.v1"]})
    assert "unknown metric" in out.content["error"]
    assert wh.sql == [] and out.artifacts == []
    out = _tools(wh).dispatch("query_semantic", {"limit": 5000})
    assert "error" in out.content and wh.sql == []


def test_warehouse_errors_reach_the_model_without_the_compiled_sql():
    out = _tools(FakeWarehouse(error="boom")).dispatch("query_semantic", {"metrics": ["metric.reportable_courses.v1"]})
    assert out.content == {"error": "boom"}
    assert out.artifacts == []


def test_chart_artifact_uses_the_result_rows():
    wh = FakeWarehouse([{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3}])
    out = _tools(wh).dispatch("query_semantic", {"metrics": ["metric.reportable_courses.v1"], "dimensions": ["term_name"],
                                                 "chart": {"type": "bar", "x": "TERM_NAME", "y": "REPORTABLE_COURSES"}})
    chart = next(a for a in out.artifacts if a["type"] == "chart")
    assert chart["data"]["chart_type"] == "bar"
    assert chart["data"]["x_axis"] == "term_name"
    assert chart["data"]["data"] == [{"term_name": "Fall", "reportable_courses": 3}]


def test_chart_naming_missing_columns_is_dropped_with_a_note():
    out = _tools().dispatch("query_semantic", {"metrics": ["metric.reportable_courses.v1"],
                                              "chart": {"type": "bar", "x": "NOPE", "y": "N"}})
    assert not any(a["type"] == "chart" for a in out.artifacts)
    assert "nope" in out.content["chart_error"]


def test_unknown_tool():
    assert "Unknown tool" in _tools().dispatch("drop_tables", {}).content["error"]


DICTIONARY = {
    ("CDM_LMS", "COURSE"): [
        {"name": "ID", "type": "NUMBER", "pii": False, "description": "Primary key"},
        {"name": "NAME", "type": "TEXT", "pii": False, "description": "Course name"},
    ],
}


def _fallback(warehouse=None):
    return ChatTools(CATALOG, tenant=TENANT_A, execute=warehouse or FakeWarehouse(),
                     describe=lambda schema, table: DICTIONARY.get((schema.upper(), table.upper())))


def test_execute_sql_requires_a_reason_and_runs_nothing_without_one():
    wh = FakeWarehouse()
    for reason in (None, "", "   "):
        out = _fallback(wh).dispatch("execute_sql", {"sql": "SELECT 1", "reason": reason}, called=SEARCHED)
        assert "reason" in out.content["error"]
    assert wh.sql == []


def test_execute_sql_results_are_marked_ungoverned_with_the_reason():
    wh = FakeWarehouse([{"N": 7}])
    out = _fallback(wh).dispatch("execute_sql", {"sql": "SELECT COUNT(*) AS N FROM DB.CDM_LMS.COURSE",
                                                 "reason": "No dataset covers raw course counts by instance"},
                                    called=SEARCHED)
    assert wh.sql == ["SELECT COUNT(*) AS N FROM DB.CDM_LMS.COURSE"]
    reason = "No dataset covers raw course counts by instance"
    assert out.content["provenance"] == {"governed": False, "governance": "none", "rows": "modified", "reason": reason,
                                         "columns": {"N": {"governed": False, "reason": reason}}}
    assert all(a["provenance"]["governed"] is False for a in out.artifacts)
    assert "query" not in out.artifacts[0]


def test_execute_sql_guard_refusals_pass_through_and_nothing_runs():
    wh = FakeWarehouse()
    out = _fallback(wh).dispatch(
        "execute_sql", {"sql": "SELECT a.B FROM X.Y a", "reason": "testing the error path"}, called=SEARCHED)
    assert "allowed list" in out.content["error"] and out.artifacts == [] and wh.sql == []


def test_describe_cdm_table_returns_columns():
    out = _fallback().dispatch("describe_cdm_table", {"schema": "cdm_lms", "table": "course"})
    assert [c["name"] for c in out.content["columns"]] == ["ID", "NAME"]


def test_describe_unknown_table_is_an_error():
    out = _fallback().dispatch("describe_cdm_table", {"schema": "CDM_LMS", "table": "NOPE"})
    assert "CDM_LMS.NOPE" in out.content["error"]


def test_specs_include_the_fallback_tools():
    names = {s["name"] for s in _fallback().specs}
    assert {"execute_sql", "describe_cdm_table"} <= names


def test_only_governed_queries_run_with_the_compiled_guard():
    wh = FakeWarehouse()
    tools = ChatTools(CATALOG, tenant=TENANT_A, execute=wh)
    tools.dispatch("query_semantic", {"metrics": ["metric.reportable_courses.v1"]})
    tools.dispatch("execute_sql", {"sql": "SELECT COUNT(*) AS N FROM CDM_LMS.COURSE", "reason": "no governed metric counts all course rows"}, called=SEARCHED)
    assert [type(v.mode).__name__ for v in wh.validated] == ["Compiled", "Freehand"]


def test_governed_results_use_the_contract_names_so_charts_match():
    wh = FakeWarehouse([{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3}])
    out = _tools(wh).dispatch("query_semantic", {"metrics": ["metric.reportable_courses.v1"], "dimensions": ["term_name"],
                                                 "chart": {"type": "bar", "x": "term_name", "y": "REPORTABLE_COURSES"}})
    assert "chart_error" not in out.content
    chart = next(a for a in out.artifacts if a["type"] == "chart")
    assert (chart["data"]["x_axis"], chart["data"]["y_axis"]) == ("term_name", "reportable_courses")


def test_the_model_is_not_sent_the_compiled_sql():
    out = _tools().dispatch("query_semantic", {"metrics": ["metric.reportable_courses.v1"]})
    assert "sql" not in out.content



def test_execute_sql_needs_the_catalog_searched_first_in_the_turn():
    wh = FakeWarehouse()
    out = _tools(wh).dispatch("execute_sql", {"sql": "SELECT 1", "reason": "no governed metric counts raw rows"})
    assert "search_catalog" in out.content["error"] and wh.sql == []


def test_execute_sql_needs_a_real_reason():
    wh = FakeWarehouse()
    out = _tools(wh).dispatch("execute_sql", {"sql": "SELECT 1", "reason": "."}, called=SEARCHED)
    assert "reason" in out.content["error"] and wh.sql == []


def test_filter_values_schema_names_the_value_types():
    spec = next(s for s in _tools().specs if s["name"] == "query_semantic")
    items = spec["inputSchema"]["json"]["properties"]["filters"]["items"]["properties"]["values"]["items"]
    assert items == {"type": ["string", "number", "boolean"]}


def test_warehouse_errors_reach_the_model_without_internal_object_or_role_names():
    raw = "002003 (42S02): SQL compilation error:\nObject 'PROD_DB.SECRET_SCHEMA.T' does not exist or not authorized, role BBDATA_ROLE"
    tools = ChatTools(CATALOG, tenant=TENANT_A, execute=FakeWarehouse(error=raw))
    out = tools.dispatch("execute_sql", {"sql": "SELECT 1", "reason": "testing the error path"}, called=SEARCHED)
    assert "SECRET_SCHEMA" not in out.content["error"] and "BBDATA_ROLE" not in out.content["error"]
    assert "does not exist" in out.content["error"]


def test_query_semantic_provenance_lists_the_tenant_overlays_it_used():
    from semantic_layer.overlays import Overlay
    used = Overlay(target="metric:metric.reportable_courses.v1", default_filters=[], version=3)
    unused = Overlay(target="metric:metric.average_grade.v1", default_filters=[], version=1)
    tools = ChatTools(CATALOG, tenant=TENANT_A, execute=FakeWarehouse([{"REPORTABLE_COURSES": 3}]), overlays=[used, unused])
    out = tools.dispatch("query_semantic", {"metrics": ["metric.reportable_courses.v1"]})
    assert out.content["provenance"]["overlays"] == ["metric:metric.reportable_courses.v1@v3"]
    assert all(a["provenance"]["overlays"] == ["metric:metric.reportable_courses.v1@v3"] for a in out.artifacts)


def test_chat_tools_execute_with_their_tenant(monkeypatch):
    seen = []
    monkeypatch.setattr(snowflake_client, "query_sql",
                        lambda sql, params=None, **k: seen.append(k["tenant"]) or {"columns": ["N"], "rows": [{"N": 1}]})
    tools = ChatTools(CATALOG, tenant=TENANT_B)
    tools.dispatch("execute_sql", {"sql": "SELECT 1 AS N FROM DB.CDM_LMS.COURSE",
                                   "reason": "No dataset covers raw course counts"}, called=SEARCHED)
    tools.dispatch("query_semantic", {"metrics": ["metric.reportable_courses.v1"]})
    assert seen == [TENANT_B, TENANT_B]


def test_core_tool_specs_never_mention_learn():
    tools = ChatTools(CATALOG, tenant=TENANT_CORE)
    assert "learn" not in str(tools.specs).lower()


def test_premium_tool_specs_mention_learn_and_keep_the_tool_names():
    specs = _tools().specs
    assert [s["name"] for s in specs] == [s["name"] for s in ChatTools(CATALOG, tenant=TENANT_CORE).specs]
    assert "LEARN" in {s["name"]: s for s in specs}["describe_cdm_table"]["description"]
    assert "LEARN" in {s["name"]: s for s in specs}["execute_sql"]["description"]
    assert not any("{" in s["description"] for s in specs)


def test_core_describe_learn_refused():
    tools = ChatTools(CATALOG, tenant=TENANT_CORE)
    assert tools.dispatch("describe_cdm_table", {"schema": "learn", "table": "USERS"}).content == {"error": LEARN_REFUSAL}


def test_premium_describe_learn_lists_columns():
    content = _tools().dispatch("describe_cdm_table", {"schema": "LEARN", "table": "ACTIVITY_ACCUMULATOR"}).content
    assert content["table"] == "LEARN.ACTIVITY_ACCUMULATOR"
    by_name = {c["name"]: c for c in content["columns"]}
    assert by_name["COURSE_PK1"]["type"] == "NUMBER" and by_name["COURSE_PK1"]["pii"] is False


IDENTITY_CONTRACT = {"measures": ["dataset.course_student_activity.v1:students"], "dimensions": ["person_email"]}


def test_chat_refuses_an_identity_dimension_for_a_viewer_and_runs_nothing():
    wh = FakeWarehouse()
    out = ChatTools(CATALOG, tenant=TENANT_A, execute=wh).dispatch("query_semantic", IDENTITY_CONTRACT)
    assert "personally identifiable" in out.content["error"] and wh.sql == []


def test_chat_runs_an_identity_dimension_for_an_allowed_role_and_audits_it():
    wh, audited = FakeWarehouse(), []
    tools = ChatTools(CATALOG, tenant=TENANT_A, execute=wh, allow_identity=True,
                      identity_audit=lambda kind, subject: audited.append(kind))
    out = tools.dispatch("query_semantic", IDENTITY_CONTRACT)
    assert "error" not in out.content and "PERSON_EMAIL" in wh.sql[0]
    assert audited == ["governed"]


def test_chat_passes_allow_identity_to_the_guard_for_governed_and_freehand_sql(monkeypatch):
    seen = []
    monkeypatch.setattr(snowflake_client, "execute",
                        lambda v, ctx: seen.append((isinstance(v.mode, Compiled), ctx.allow_identity))
                        or Result(columns=["N"], rows=[{"N": 1}]))
    reason = "no governed metric covers this question"
    for allow in (False, True):
        tools = ChatTools(CATALOG, tenant=TENANT_A, allow_identity=allow)
        tools.dispatch("query_semantic", {"metrics": ["metric.reportable_courses.v1"]})
        tools.dispatch("execute_sql", {"sql": "SELECT 1", "reason": reason}, called=SEARCHED)
    assert seen == [(True, False), (False, False), (True, True), (False, True)]


COURSES_BY_TERM = {"metrics": ["metric.reportable_courses.v1"], "dimensions": ["term_name"]}
MIXED_SQL = ("SELECT g.term_name, g.reportable_courses, c.N FROM governed_1 g"
             " CROSS JOIN (SELECT COUNT(*) AS N FROM DB.CDM_LMS.COURSE) c")
MIXED_REASON = "No governed metric counts every course row"


def _mixed(wh, sql=MIXED_SQL, governed=None, **tool_input):
    return _fallback(wh).dispatch("execute_sql", {"sql": sql, "reason": MIXED_REASON,
                                                  "governed": governed or {"governed_1": COURSES_BY_TERM}, **tool_input},
                                  called=SEARCHED)


def test_execute_sql_with_a_governed_part_runs_the_spliced_sql_and_labels_each_column():
    wh = FakeWarehouse([{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3, "N": 7}])
    out = _mixed(wh)
    assert len(wh.sql) == 1 and "governed_1 AS (" in wh.sql[0] and "DS_COURSE_FILTERS_V1" in wh.sql[0]
    validated = wh.validated[0]
    assert isinstance(validated.mode, Spliced) and validated.spliced.sql.strip() == wh.sql[0]
    assert validated.spliced.part_names == frozenset({"governed_1"})
    provenance = out.content["provenance"]
    assert provenance == {
        "governed": False, "governance": "partial", "rows": "modified", "reason": MIXED_REASON,
        "columns": {"TERM_NAME": {"governed": True, "definition": "dimension:term_name"},
                    "REPORTABLE_COURSES": {"governed": True, "definition": "metric.reportable_courses.v1"},
                    "N": {"governed": False, "reason": MIXED_REASON}},
        "metrics": ["metric.reportable_courses.v1"], "measures": ["dataset.course_filters.v1:courses"],
        "datasets": ["dataset.course_filters.v1"], "overlays": [],
    }
    assert out.content["rows"] == [{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3, "N": 7}]
    assert "sql" not in out.content


def test_a_partly_governed_result_carries_the_outer_sql_its_parts_and_the_executed_sql():
    wh = FakeWarehouse([{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3, "N": 7}])
    out = _mixed(wh)
    table, sql = (next(a for a in out.artifacts if a["type"] == kind) for kind in ("table", "sql"))
    assert table["title"] == sql["title"] == "Partly governed query result"
    assert sql["data"] == table["sql"] == sql["sql"] == MIXED_SQL
    for artifact in (table, sql):
        assert artifact["governed_parts"] == {"governed_1": COURSES_BY_TERM}
        assert artifact["executed_sql"] == wh.sql[0]
        assert artifact["provenance"]["governance"] == "partial"
        assert "query" not in artifact


def test_a_query_of_only_governed_columns_is_a_governed_query_result():
    wh = FakeWarehouse([{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3}])
    out = _mixed(wh, sql="SELECT term_name, reportable_courses FROM governed_1 WHERE reportable_courses > 1")
    assert out.content["provenance"]["governed"] is True
    assert out.content["provenance"]["governance"] == "full"
    assert {a["title"] for a in out.artifacts} == {"Governed query result"}


def test_a_given_title_wins_over_the_governance_title():
    out = _mixed(FakeWarehouse([{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3, "N": 7}]), title="Courses vs all")
    assert {a["title"] for a in out.artifacts} == {"Courses vs all"}


def test_provenance_unions_the_parts_and_lists_the_overlays_they_used():
    from semantic_layer.overlays import Overlay
    used = Overlay(target="metric:metric.reportable_courses.v1", default_filters=[], version=3)
    unused = Overlay(target="metric:metric.average_grade.v1", default_filters=[], version=1)
    tools = ChatTools(CATALOG, tenant=TENANT_A, overlays=[used, unused],
                      execute=FakeWarehouse([{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3, "STUDENTS": 9}]))
    out = tools.dispatch("execute_sql", {
        "sql": "SELECT a.term_name, a.reportable_courses, b.students FROM governed_1 a JOIN governed_2 b"
               " ON a.term_name = b.term_name",
        "reason": MIXED_REASON,
        "governed": {"governed_1": COURSES_BY_TERM,
                     "governed_2": {"measures": ["dataset.student_grade.v1:students"], "dimensions": ["term_name"]}},
    }, called=SEARCHED)
    provenance = out.content["provenance"]
    assert provenance["metrics"] == ["metric.reportable_courses.v1"]
    assert provenance["measures"] == ["dataset.course_filters.v1:courses", "dataset.student_grade.v1:students"]
    assert {"dataset.course_filters.v1", "dataset.student_grade.v1"} <= set(provenance["datasets"])
    assert len(provenance["datasets"]) == len(set(provenance["datasets"]))
    assert provenance["overlays"] == ["metric:metric.reportable_courses.v1@v3"]


def test_an_invalid_governed_part_is_reported_like_query_semantic_and_nothing_runs():
    wh = FakeWarehouse()
    out = _mixed(wh, governed={"governed_1": {"metrics": ["metric.nope.v1"]}})
    assert "governed_1" in out.content["error"] and "unknown metric" in out.content["error"]
    out = _mixed(wh, governed={"governed_1": {"limit": 5000}})
    assert "governed_1" in out.content["error"]
    out = _mixed(wh, governed={"governed_1": "metric.reportable_courses.v1"})
    assert "governed_1" in out.content["error"]
    out = _mixed(wh, governed=["governed_1"])
    assert "governed" in out.content["error"]
    assert wh.sql == [] and out.artifacts == []


def test_a_splice_refusal_reaches_the_model_and_nothing_runs():
    wh = FakeWarehouse()
    out = _mixed(wh, sql="SELECT COUNT(*) AS N FROM DB.CDM_LMS.COURSE")
    assert "governed_1 is declared but the SQL never selects from it" in out.content["error"]
    assert wh.sql == [] and out.artifacts == []


def test_governed_parts_still_need_a_governed_tool_called_first():
    wh = FakeWarehouse()
    out = _tools(wh).dispatch("execute_sql", {"sql": MIXED_SQL, "reason": MIXED_REASON,
                                              "governed": {"governed_1": COURSES_BY_TERM}})
    assert "search_catalog" in out.content["error"] and wh.sql == []


def test_governed_parts_compile_and_run_with_the_callers_identity_rights(monkeypatch):
    seen = []
    monkeypatch.setattr(snowflake_client, "execute",
                        lambda v, ctx: seen.append((v, ctx)) or Result(columns=["PERSON_EMAIL"], rows=[]))
    email = {"governed_1": {"measures": ["dataset.course_student_activity.v1:students"], "dimensions": ["person_email"]}}
    request = {"sql": "SELECT person_email FROM governed_1", "reason": MIXED_REASON, "governed": email}
    viewer = ChatTools(CATALOG, tenant=TENANT_A).dispatch("execute_sql", request, called=SEARCHED)
    assert "personally identifiable" in viewer.content["error"] and seen == []
    audited = []
    admin = ChatTools(CATALOG, tenant=TENANT_A, allow_identity=True,
                      identity_audit=lambda kind, subject: audited.append(kind))
    assert "error" not in admin.dispatch("execute_sql", request, called=SEARCHED).content
    v, ctx = seen[0]
    assert ctx.allow_identity is True and ctx.tenant == TENANT_A and isinstance(v.mode, Spliced)
    assert v.spliced.part_names == frozenset({"governed_1"}) and isinstance(ctx.extra_pii, frozenset)
    assert audited == ["governed", "freehand"]


def test_execute_sql_spec_describes_governed_parts_for_every_licence():
    for tenant in (TENANT_A, TENANT_CORE):
        spec = next(s for s in ChatTools(CATALOG, tenant=tenant).specs if s["name"] == "execute_sql")
        assert "governed_1" in spec["description"]
        governed = spec["inputSchema"]["json"]["properties"]["governed"]
        assert governed["type"] == "object"
        assert "metrics" in governed["additionalProperties"]["properties"]


def test_a_parts_explicit_default_sized_limit_is_kept_for_the_follow_up():
    part = {**COURSES_BY_TERM, "limit": 100, "order_by": [{"field": "reportable_courses"}]}
    out = _mixed(FakeWarehouse([{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3, "N": 7}]), governed={"governed_1": part})
    assert out.artifacts[0]["governed_parts"]["governed_1"]["limit"] == 100
    assert out.content["provenance"]["rows"] == "modified"


def test_artifacts_say_which_tool_kind_produced_them():
    semantic = _tools().dispatch("query_semantic", {"metrics": ["metric.reportable_courses.v1"]})
    raw = _tools().dispatch("execute_sql", {"sql": "SELECT 1", "reason": MIXED_REASON}, called=SEARCHED)
    mixed = _mixed(FakeWarehouse([{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3, "N": 7}]))
    assert {a["kind"] for a in semantic.artifacts} == {"semantic"}
    assert {a["kind"] for a in raw.artifacts} == {a["kind"] for a in mixed.artifacts} == {"sql"}


class TruncatingWarehouse(FakeWarehouse):
    def __call__(self, v, ctx):
        return dataclasses.replace(super().__call__(v, ctx), truncated=True)


def test_a_warehouse_truncated_single_part_result_is_partial_with_rows_modified_and_says_so():
    wh = TruncatingWarehouse([{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3}])
    out = _mixed(wh, sql="SELECT term_name, reportable_courses FROM governed_1")
    provenance = out.content["provenance"]
    assert (provenance["governance"], provenance["rows"], provenance["governed"]) == ("partial", "modified", False)
    assert provenance["truncated"] is True and out.content["truncated"] is True
    assert "1000 rows" in out.content["note"]
    assert all(a["truncated"] is True and a["provenance"]["truncated"] is True for a in out.artifacts)


def test_a_warehouse_truncated_ungoverned_result_says_so():
    out = _tools(TruncatingWarehouse()).dispatch("execute_sql", {"sql": "SELECT 1", "reason": MIXED_REASON},
                                                 called=SEARCHED)
    assert out.content["truncated"] is True and out.content["provenance"]["truncated"] is True
    assert "1000 rows" in out.content["note"]


def test_an_untruncated_single_part_result_stays_full():
    out = _mixed(FakeWarehouse([{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3}]),
                 sql="SELECT term_name, reportable_courses FROM governed_1")
    assert out.content["provenance"]["governance"] == "full" and "truncated" not in out.content["provenance"]
    assert "note" not in out.content
