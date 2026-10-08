from semantic_layer.catalog import load_catalog
from semantic_layer.chat_tools import MODEL_ROW_LIMIT, ChatTools

CATALOG = load_catalog()


class FakeWarehouse:
    def __init__(self, rows=None, error=None):
        self.rows, self.error, self.sql = rows if rows is not None else [{"N": 1}], error, []

    def __call__(self, sql, params=None):
        self.sql.append(sql)
        if self.error:
            return {"error": self.error}
        return {"columns": list(self.rows[0]) if self.rows else [], "rows": self.rows}


def _tools(warehouse=None):
    return ChatTools(CATALOG, "DB", execute=warehouse or FakeWarehouse())


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
    assert out.content["rows"] == [{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3}]
    assert out.content["provenance"]["governed"] is True
    table = next(a for a in out.artifacts if a["type"] == "table")
    assert table["title"] == "Courses by term"
    assert table["data"] == {"columns": ["TERM_NAME", "REPORTABLE_COURSES"], "rows": [{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3}]}
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


def test_warehouse_errors_are_returned_with_the_sql():
    out = _tools(FakeWarehouse(error="boom")).dispatch("query_semantic", {"metrics": ["metric.reportable_courses.v1"]})
    assert out.content["error"] == "boom" and "DS_COURSE_FILTERS_V1" in out.content["sql"]
    assert out.artifacts == []


def test_chart_artifact_uses_the_result_rows():
    wh = FakeWarehouse([{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3}])
    out = _tools(wh).dispatch("query_semantic", {"metrics": ["metric.reportable_courses.v1"], "dimensions": ["term_name"],
                                                 "chart": {"type": "bar", "x": "TERM_NAME", "y": "REPORTABLE_COURSES"}})
    chart = next(a for a in out.artifacts if a["type"] == "chart")
    assert chart["data"]["chart_type"] == "bar"
    assert chart["data"]["x_axis"] == "TERM_NAME" and chart["data"]["data"] == wh.rows


def test_chart_naming_missing_columns_is_dropped_with_a_note():
    out = _tools().dispatch("query_semantic", {"metrics": ["metric.reportable_courses.v1"],
                                              "chart": {"type": "bar", "x": "NOPE", "y": "N"}})
    assert not any(a["type"] == "chart" for a in out.artifacts)
    assert "NOPE" in out.content["chart_error"]


def test_unknown_tool():
    assert "Unknown tool" in _tools().dispatch("drop_tables", {}).content["error"]
