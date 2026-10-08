from semantic_layer.catalog import load_catalog
from semantic_layer.prompt import build_system_prompt

CATALOG = load_catalog()
PROMPT = build_system_prompt(CATALOG, "PROD_DB")


def test_every_public_dataset_and_metric_is_described():
    for ds in CATALOG.datasets.values():
        if ds.visibility == "public":
            assert ds.id in PROMPT and ds.grain in PROMPT
    for m in CATALOG.metrics.values():
        assert m.id in PROMPT


def test_dimensions_list_their_grains_and_measures_their_names():
    assert "course_start_week (time: week, month, quarter, year)" in PROMPT
    assert "active_students" in PROMPT


def test_pii_dimensions_are_marked_filter_only():
    assert "student_email (filter only)" in PROMPT


def test_no_dataset_sql_or_raw_schema_dump():
    assert "base_sql" not in PROMPT and "SELECT" not in PROMPT
    assert "CDM_LMS.PERSON_COURSE" not in PROMPT


def test_rules_put_governed_tools_first_and_require_a_reason_for_freehand_sql():
    assert PROMPT.index("search_catalog") < PROMPT.index("execute_sql")
    assert "reason" in PROMPT
    assert "PROD_DB" in PROMPT


def test_no_text_markers_are_requested():
    for marker in ("[CHART_CONFIG]", "[SQL_QUERY]", "[QUERY_PARAMS]"):
        assert marker not in PROMPT
