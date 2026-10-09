import pytest

from semantic_layer.catalog import load_catalog
from semantic_layer.compiler import compile_query
from semantic_layer.contract import QueryContract
from semantic_layer.overlays import Overlay, OverlayError, apply_overlays, validate_overlay

CATALOG = load_catalog()
GRADE = "dataset.student_grade.v1"


def _sql(catalog, metric):
    return compile_query(QueryContract(metrics=[metric]), catalog, "DB").sql


def test_a_measure_overlay_changes_the_metrics_built_on_it_and_leaves_the_canonical_catalog_alone():
    ov = Overlay(target=f"measure:{GRADE}:average_grade_percentage", expr="ROUND(GRADE_PERCENTAGE, 0)")
    tenant = apply_overlays(CATALOG, [ov])
    assert "ROUND(GRADE_PERCENTAGE, 0)" in _sql(tenant, "metric.average_grade.v1")
    assert "ROUND(GRADE_PERCENTAGE, 0)" not in _sql(CATALOG, "metric.average_grade.v1")


def test_filter_overlays_replace_a_filter_or_add_a_tenant_only_one():
    tenant = apply_overlays(CATALOG, [
        Overlay(target=f"filter:{GRADE}:graded_only", sql="GRADE_PERCENTAGE > 0"),
        Overlay(target=f"filter:{GRADE}:honours", sql="GRADE_PERCENTAGE >= 90", description="Honours students"),
    ])
    ds = tenant.datasets[GRADE]
    assert ds.filter("graded_only").sql == "GRADE_PERCENTAGE > 0"
    assert ds.filter("honours").description == "Honours students"


def test_a_metric_overlay_replaces_its_default_filters():
    tenant = apply_overlays(CATALOG, [Overlay(target="metric:metric.average_grade.v1", default_filters=["graded_only"])])
    assert tenant.metrics["metric.average_grade.v1"].default_filters == ["graded_only"]


def test_a_valid_overlay_has_no_errors():
    assert validate_overlay(Overlay(target=f"measure:{GRADE}:average_grade_percentage", expr="ROUND(GRADE_PERCENTAGE, 0)"), CATALOG) == []


@pytest.mark.parametrize("target,field,value,message", [
    (f"measure:{GRADE}:average_grade_percentage", "expr", "{{ database }}", "template"),
    (f"measure:{GRADE}:average_grade_percentage", "expr", "{% for x in y %}1{% endfor %}", "template"),
    (f"measure:{GRADE}:average_grade_percentage", "expr", "GRADE_PERCENTAGE; DROP TABLE X", "one expression"),
    (f"filter:{GRADE}:graded_only", "sql", "1 = 1; DELETE FROM X", "one expression"),
    (f"measure:{GRADE}:average_grade_percentage", "expr", "(SELECT MAX(EMAIL) FROM CDM_LMS.PERSON)", "queries or tables"),
    (f"filter:{GRADE}:graded_only", "sql", "PERSON_ID IN (SELECT ID FROM CDM_LMS.PERSON)", "queries or tables"),
    (f"measure:{GRADE}:average_grade_percentage", "expr", "NO_SUCH_COLUMN", "does not output"),
    (f"filter:{GRADE}:graded_only", "sql", "NO_SUCH_COLUMN = 1", "does not output"),
    (f"measure:{GRADE}:share_failing", "expr", "GRADE_PERCENTAGE", "ratio"),
    ("metric:metric.average_grade.v1", "default_filters", ["no_such_filter"], "no filter"),
    ("measure:dataset.nope.v1:x", "expr", "1", "unknown dataset"),
    ("widget:x", "expr", "1", "target"),
    (f"measure:{GRADE}:average_grade_percentage", "expr", "ASCII(SUBSTR(IDENTIFIER('PERSON_ID'), 1, 1))", "not allowed"),
    (f"measure:{GRADE}:average_grade_percentage", "expr", "LENGTH(GET(OBJECT_CONSTRUCT(*), 'PERSON_ID'))", "not allowed"),
    (f"measure:{GRADE}:average_grade_percentage", "expr", "$3", "not allowed"),
    (f"measure:{GRADE}:average_grade_percentage", "expr", "HASH(*)", "not allowed"),
    (f"measure:{GRADE}:average_grade_percentage", "expr", "OTHERDB.PUBLIC.MYUDF(GRADE_PERCENTAGE)", "not allowed"),
    (f"measure:{GRADE}:average_grade_percentage", "expr", "AVG(GRADE_PERCENTAGE)", "aggregate"),
    (f"measure:{GRADE}:average_grade_percentage", "expr", "RANK() OVER (ORDER BY GRADE_PERCENTAGE)", "aggregate"),
    (f"filter:{GRADE}:graded_only", "sql", "IDENTIFIER('PERSON_ID') > 0", "not allowed"),
    (f"filter:{GRADE}:2024_terms", "sql", "GRADE_PERCENTAGE > 0", "name"),
])
def test_invalid_overlays_are_rejected(target, field, value, message):
    try:
        errors = validate_overlay(Overlay(target=target, **{field: value}), CATALOG)
    except OverlayError as e:
        errors = [str(e)]
    assert errors and any(message in e for e in errors), errors


def test_a_measure_overlay_may_not_return_pii_values():
    errors = validate_overlay(Overlay(target=f"measure:{GRADE}:average_grade_percentage", expr="PERSON_ID"), CATALOG)
    assert any("PII" in e for e in errors), errors


def test_an_overlay_must_set_the_field_its_target_needs():
    with pytest.raises(ValueError):
        Overlay(target="metric:metric.average_grade.v1", expr="1")


@pytest.mark.parametrize("expr", ["IFF(GRADE_PERCENTAGE > 100, 100, GRADE_PERCENTAGE)", "TRY_TO_NUMBER(GRADE_PERCENTAGE::STRING)",
                                  "ZEROIFNULL(GRADE_PERCENTAGE)", "DIV0(GRADE_PERCENTAGE, 2)"])
def test_ordinary_row_level_expressions_are_allowed(expr):
    assert validate_overlay(Overlay(target=f"measure:{GRADE}:average_grade_percentage", expr=expr), CATALOG) == []
