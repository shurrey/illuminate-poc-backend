"""Every canonical dataset and metric passes offline validation and compiles."""

import pytest

from semantic_layer.catalog import load_catalog
from semantic_layer.compiler import compile_query
from semantic_layer.contract import QueryContract
from semantic_layer.validate import load_pii_columns, load_snapshot, validate_dataset, validate_metric

CATALOG = load_catalog()
SNAPSHOT = load_snapshot()
PII = load_pii_columns()
PUBLIC = [d for d in CATALOG.datasets.values() if d.visibility == "public"]


@pytest.mark.parametrize("ds", CATALOG.datasets.values(), ids=lambda d: d.id)
def test_dataset_is_valid(ds):
    assert validate_dataset(ds, CATALOG, SNAPSHOT, PII) == []


@pytest.mark.parametrize("ds", PUBLIC, ids=lambda d: d.id)
def test_every_measure_and_dimension_compiles(ds):
    dims = [d.name for d in ds.dimensions if d.column not in ds.pii_columns]
    time_range = {"dimension": ds.required_time_range, "start": "2026-01-01"} if ds.required_time_range else None
    compile_query(QueryContract(measures=[f"{ds.id}:{m.name}" for m in ds.measures], dimensions=dims, time_range=time_range),
                  CATALOG, "DB")


@pytest.mark.parametrize("metric", CATALOG.metrics.values(), ids=lambda m: m.id)
def test_metric_is_valid_and_compiles(metric):
    assert validate_metric(metric, CATALOG) == []
    compile_query(QueryContract(metrics=[metric.id]), CATALOG, "DB")


def test_catalog_is_not_empty():
    assert CATALOG.datasets and CATALOG.metrics


@pytest.mark.parametrize("metric", CATALOG.metrics.values(), ids=lambda m: m.id)
def test_compiled_metric_passes_the_execution_guard(metric, monkeypatch):
    import snowflake_client

    monkeypatch.setattr(snowflake_client, "query_sql", lambda sql, params=None, **k: {"columns": [], "rows": []})
    compiled = compile_query(QueryContract(metrics=[metric.id]), CATALOG, "DB")
    assert snowflake_client.validate_and_execute(compiled.sql, compiled=True) == {"columns": [], "rows": []}


@pytest.mark.parametrize("metric_id", ["metric.courses.v1", "metric.classic_courses.v1", "metric.ultra_courses.v1"])
def test_course_count_metrics_exclude_deleted_courses(metric_id):
    metric = CATALOG.metrics[metric_id]
    assert "live" in metric.default_filters
    live = CATALOG.datasets[metric.dataset_id].filter("live")
    assert live is not None and "COURSE_DELETED_IND" in live.sql


@pytest.mark.parametrize("ds_id", sorted(CATALOG.datasets), ids=str)
def test_every_dataset_reads_only_cdm_tables_and_its_own_ctes(ds_id):
    from semantic_layer.compiler import _check_tables, build_ctes

    ctes = build_ctes(CATALOG, [ds_id], "DB")
    sql = "WITH " + ",\n".join(f"{n} AS (\n{s}\n)" for n, s in ctes.items()) + f"\nSELECT * FROM {list(ctes)[-1]}"
    _check_tables(sql, "DB")


def test_activity_log_reads_ultra_clicks_in_either_case_once_each():
    sql = CATALOG.datasets["dataset.activity_log.v1"].base_sql
    assert "UPPER(ue.EVENT_TYPE) = 'CLICK'" in sql
    assert "PARTITION BY ue.DATA:eventId" in sql
    assert "ipAddress" not in sql and "userAgent" not in sql


def test_assignment_submissions_ignore_deleted_grades():
    assert "gr.ROW_DELETED_TIME IS NULL" in CATALOG.datasets["dataset.student_assignments.v1"].base_sql


@pytest.mark.parametrize("metric_id", ["metric.student_enrollments.v1", "metric.instructor_enrollments.v1"])
def test_enrollment_metrics_do_not_claim_to_count_people(metric_id):
    m = CATALOG.metrics[metric_id]
    assert not any("how many" in s or "count" in s for s in m.synonyms)
    assert "not distinct people" in m.description


def test_sis_students_measure_counts_students_and_instructor_names_are_gone():
    ds = CATALOG.datasets["dataset.sis_enrollment_attributes.v1"]
    assert "STUDENT_IND" in ds.measure("students").expr
    assert ds.dimension("primary_instructor_name") is None and "STAFF" not in ds.base_sql


def test_the_grade_distribution_counts_enrollments():
    m = CATALOG.metrics["metric.graded_enrollments.v1"]
    assert m.measure == "dataset.student_grade.v1:graded_enrollments"
