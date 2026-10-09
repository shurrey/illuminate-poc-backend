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
    compile_query(QueryContract(measures=[f"{ds.id}:{m.name}" for m in ds.measures], dimensions=dims),
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
