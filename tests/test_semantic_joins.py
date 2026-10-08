import pytest
import sqlglot

from semantic_layer.compiler import CompileError, compile_query
from semantic_layer.contract import QueryContract
from tests.semantic_fixtures import COURSES, ENROLLMENTS, catalog, dataset

CAT = catalog(ENROLLMENTS, COURSES)


def _compile(cat=CAT, **contract):
    q = compile_query(QueryContract(**contract), cat, "DB")
    sqlglot.parse_one(q.sql, read="snowflake")
    return q


def test_dimension_from_a_many_to_one_dataset_is_joined():
    q = _compile(measures=["dataset.enrollments.v1:enrollments"], dimensions=["course_name"])
    assert "LEFT JOIN DS_COURSES_V1 AS j1\n  ON b.COURSE_ID = j1.COURSE_ID" in q.sql
    assert "j1.COURSE_NAME AS course_name" in q.sql
    assert "COUNT(b.ID) AS enrollments" in q.sql
    assert q.provenance.datasets == ["dataset.enrollments.v1", "dataset.courses.v1"]


def test_joining_toward_a_finer_grain_is_rejected():
    with pytest.raises(CompileError, match="multiply"):
        _compile(measures=["dataset.courses.v1:courses"], dimensions=["dataset.enrollments.v1:course_role"])


def test_own_dimension_wins_over_a_joined_one_with_the_same_name():
    q = _compile(measures=["dataset.enrollments.v1:enrollments"], dimensions=["course_role"])
    assert "DS_COURSES_V1" not in q.sql
    assert "COURSE_ROLE AS course_role" in q.sql


def test_qualified_dimension_selects_the_joined_dataset():
    q = _compile(measures=["dataset.enrollments.v1:enrollments"], dimensions=["dataset.courses.v1:course_role"])
    assert "j1.COURSE_NAME AS course_role" in q.sql


def test_unknown_or_unreachable_qualified_dimension_is_an_error():
    with pytest.raises(CompileError, match="no dimension"):
        _compile(measures=["dataset.enrollments.v1:enrollments"], dimensions=["dataset.courses.v1:nope"])
    other = dataset(id="dataset.other.v1", entities=[])
    with pytest.raises(CompileError, match="cannot be reached"):
        _compile(catalog(ENROLLMENTS, COURSES, other), measures=["dataset.enrollments.v1:enrollments"],
                 dimensions=["dataset.other.v1:course_role"])


def test_ambiguous_dimension_across_joined_datasets_is_an_error():
    twin = dataset(id="dataset.twin.v1", base_sql=COURSES.base_sql, entities=COURSES.model_dump()["entities"],
                   dimensions=COURSES.model_dump()["dimensions"], measures=COURSES.model_dump()["measures"],
                   pii_columns=[], complete=True)
    with pytest.raises(CompileError, match="ambiguous"):
        _compile(catalog(ENROLLMENTS, COURSES, twin), measures=["dataset.enrollments.v1:enrollments"],
                 dimensions=["course_name"])


def test_filters_and_time_ranges_resolve_through_joins():
    q = _compile(measures=["dataset.enrollments.v1:enrollments"],
                 filters=[{"dimension": "course_name", "op": "eq", "values": ["Biology"]}],
                 time_range={"dimension": "course_start", "start": "2026-01-01"})
    assert "j1.COURSE_NAME = 'Biology'" in q.sql
    assert "CAST(j1.START_DATE AS DATE) >= '2026-01-01'" in q.sql


def test_metric_filters_are_qualified_to_the_measure_dataset_in_a_join():
    q = _compile(metrics=["metric.student_enrollments.v1"], dimensions=["course_name"])
    assert "COUNT(CASE WHEN b.COURSE_ROLE = 'S' THEN b.ID END) AS student_enrollments" in q.sql


def test_measures_from_two_datasets_aggregate_separately_then_join_on_dimensions():
    q = _compile(measures=["dataset.enrollments.v1:enrollments", "dataset.courses.v1:courses"],
                 dimensions=["course_name"], order_by=[{"field": "courses"}], limit=10)
    outer = q.sql[q.sql.rindex("\nSELECT"):]
    assert "FULL OUTER JOIN g2" in outer
    assert "g1.course_name IS NOT DISTINCT FROM g2.course_name" in outer
    assert "COALESCE(g1.course_name, g2.course_name) AS course_name" in outer
    assert "g1.enrollments" in outer and "g2.courses" in outer
    assert outer.rstrip().endswith("LIMIT 10")


def test_measures_from_two_datasets_without_dimensions_cross_join():
    q = _compile(measures=["dataset.enrollments.v1:enrollments", "dataset.courses.v1:courses"])
    assert "CROSS JOIN g2" in q.sql


def test_every_dimension_must_be_reachable_from_every_measure_dataset():
    with pytest.raises(CompileError, match="enrolled_at"):
        _compile(measures=["dataset.enrollments.v1:enrollments", "dataset.courses.v1:courses"],
                 dimensions=["enrolled_at__day"])


def test_empty_grain_suffix_is_rejected():
    with pytest.raises(CompileError, match="grain"):
        _compile(measures=["dataset.enrollments.v1:enrollments"], dimensions=["course_role__"])


def test_dimensions_are_only_joined_from_complete_datasets():
    partial = dataset(id="dataset.partial_courses.v1", base_sql=COURSES.base_sql + " WHERE c.START_DATE IS NOT NULL",
                      entities=COURSES.model_dump()["entities"], dimensions=COURSES.model_dump()["dimensions"],
                      measures=COURSES.model_dump()["measures"], pii_columns=[], complete=False)
    cat = catalog(ENROLLMENTS, partial)
    with pytest.raises(CompileError, match="not complete"):
        _compile(cat, measures=["dataset.enrollments.v1:enrollments"], dimensions=["dataset.partial_courses.v1:course_name"])
    with pytest.raises(CompileError, match="unknown dimension"):
        _compile(cat, measures=["dataset.enrollments.v1:enrollments"], dimensions=["course_name"])


def test_three_datasets_with_a_dimension_merge_on_the_coalesced_key():
    third = dataset(id="dataset.third.v1")
    q = _compile(catalog(ENROLLMENTS, COURSES, third),
                 measures=["dataset.enrollments.v1:enrollments", "dataset.courses.v1:courses", "dataset.third.v1:people"],
                 dimensions=["dataset.courses.v1:course_name"], order_by=[{"field": "course_name", "direction": "asc"}])
    outer = q.sql[q.sql.rindex("\nSELECT"):]
    assert "COALESCE(g1.course_name, g2.course_name) IS NOT DISTINCT FROM g3.course_name" in outer
    assert "COALESCE(g1.course_name, g2.course_name, g3.course_name) AS course_name" in outer
    assert "ORDER BY\n  course_name" in outer


def test_filters_and_time_ranges_through_joins_apply_in_every_group():
    q = _compile(measures=["dataset.enrollments.v1:enrollments", "dataset.courses.v1:courses"],
                 filters=[{"dimension": "course_name", "op": "eq", "values": ["Biology"]}],
                 time_range={"dimension": "course_start", "start": "2026-01-01"})
    g1 = q.sql[q.sql.index("g1 AS ("):q.sql.index("g2 AS (")]
    g2 = q.sql[q.sql.index("g2 AS ("):q.sql.rindex("\nSELECT")]
    assert "j1.COURSE_NAME = 'Biology'" in g1 and "CAST(j1.START_DATE AS DATE) >= '2026-01-01'" in g1
    assert "COURSE_NAME = 'Biology'" in g2 and "CAST(START_DATE AS DATE) >= '2026-01-01'" in g2
