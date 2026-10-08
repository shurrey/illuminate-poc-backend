import sqlglot
import pytest

from semantic_layer.compiler import CompileError, build_ctes, compile_query
from semantic_layer.contract import QueryContract
from semantic_layer.schema import SemanticMetric
from tests.semantic_fixtures import ENROLLMENTS, STUDENT_ENROLLMENTS, catalog, dataset


def _compile(cat=None, **contract):
    return compile_query(QueryContract(**contract), cat or catalog(), "DB")


def _outer(sql: str) -> str:
    return sql[sql.rindex("\nSELECT"):]


def test_metric_filters_scope_the_measure_not_the_query():
    q = _compile(metrics=["metric.student_enrollments.v1"], measures=["dataset.enrollments.v1:people"])
    outer = _outer(q.sql)
    assert "COUNT(CASE WHEN COURSE_ROLE = 'S' THEN ID END) AS student_enrollments" in outer
    assert "COUNT(DISTINCT PERSON_ID) AS people" in outer
    assert "WHERE" not in outer


def test_ratio_divides_aggregates_and_guards_zero():
    q = _compile(measures=["dataset.enrollments.v1:per_person"])
    assert "COUNT(ID) / NULLIF(COUNT(DISTINCT PERSON_ID), 0) AS per_person" in q.sql


def test_dimensions_group_and_time_grains_truncate():
    q = _compile(measures=["dataset.enrollments.v1:enrollments"], dimensions=["course_role", "enrolled_at__month"])
    outer = _outer(q.sql)
    assert "COURSE_ROLE AS course_role" in outer
    assert "CAST(DATE_TRUNC('month', ENROLLMENT_TIME) AS DATE) AS enrolled_at__month" in outer
    assert "GROUP BY\n  1,\n  2" in outer


def test_filter_values_are_escaped_literals():
    q = _compile(measures=["dataset.enrollments.v1:enrollments"],
                 filters=[{"dimension": "course_role", "op": "eq", "values": ["x' OR 1=1 --"]}])
    where = sqlglot.parse_one(q.sql, read="snowflake").args["where"].this
    assert isinstance(where, sqlglot.exp.EQ)
    assert where.expression.this == "x' OR 1=1 --"


@pytest.mark.parametrize("op,values,fragment", [
    ("in", ["S", "P"], "COURSE_ROLE IN ('S', 'P')"),
    ("not_in", ["S"], "NOT COURSE_ROLE IN ('S')"),
    ("between", ["A", "M"], "COURSE_ROLE BETWEEN 'A' AND 'M'"),
    ("is_null", [], "COURSE_ROLE IS NULL"),
    ("not_null", [], "NOT COURSE_ROLE IS NULL"),
    ("contains", ["st"], "CONTAINS(LOWER(COURSE_ROLE), LOWER('st'))"),
    ("gte", [3], "COURSE_ROLE >= 3"),
    ("eq", [True], "COURSE_ROLE = TRUE"),
])
def test_filter_operators(op, values, fragment):
    q = _compile(measures=["dataset.enrollments.v1:enrollments"],
                 filters=[{"dimension": "course_role", "op": op, "values": values}])
    assert fragment in q.sql


def test_time_range_compares_dates():
    q = _compile(measures=["dataset.enrollments.v1:enrollments"],
                 time_range={"dimension": "enrolled_at", "start": "2026-01-01", "end": "2026-06-30"})
    assert "CAST(ENROLLMENT_TIME AS DATE) >= '2026-01-01'" in q.sql
    assert "CAST(ENROLLMENT_TIME AS DATE) <= '2026-06-30'" in q.sql


def test_time_range_rejects_a_grain_suffix():
    with pytest.raises(CompileError, match="grain"):
        _compile(measures=["dataset.enrollments.v1:enrollments"], time_range={"dimension": "enrolled_at__month", "start": "2026-01-01"})


def test_time_range_rejects_non_time_dimension():
    with pytest.raises(CompileError, match="time dimension"):
        _compile(measures=["dataset.enrollments.v1:enrollments"], time_range={"dimension": "course_role", "start": "2026-01-01"})


def test_limit_is_emitted_once():
    q = _compile(measures=["dataset.enrollments.v1:enrollments"], limit=7)
    assert q.sql.upper().count("LIMIT") == 1
    assert q.sql.rstrip().endswith("LIMIT 7")


def test_order_by_must_name_a_selected_field():
    q = _compile(measures=["dataset.enrollments.v1:enrollments"], order_by=[{"field": "enrollments", "direction": "asc"}])
    assert "ORDER BY\n  enrollments" in q.sql
    with pytest.raises(CompileError, match="order_by"):
        _compile(measures=["dataset.enrollments.v1:enrollments"], order_by=[{"field": "nope"}])


def test_pii_dimension_cannot_be_selected():
    with pytest.raises(CompileError, match="personally identifiable"):
        _compile(measures=["dataset.enrollments.v1:enrollments"], dimensions=["email"])


def test_pii_dimension_can_be_filtered():
    q = _compile(measures=["dataset.enrollments.v1:enrollments"],
                 filters=[{"dimension": "email", "op": "not_null"}])
    assert "NOT EMAIL IS NULL" in q.sql


@pytest.mark.parametrize("contract,match", [
    ({"metrics": ["metric.nope.v1"]}, "unknown metric"),
    ({"measures": ["dataset.enrollments.v1:nope"]}, "unknown measure"),
    ({"measures": ["dataset.enrollments.v1:enrollments"], "dimensions": ["nope"]}, "unknown dimension"),
    ({"measures": ["dataset.enrollments.v1:enrollments"], "dimensions": ["enrolled_at__year"]}, "grain"),
    ({"measures": ["dataset.enrollments.v1:enrollments"], "filters": [{"dimension": "nope", "op": "is_null"}]}, "unknown dimension 'nope'"),
    ({"metrics": ["metric.student_enrollments.v1"], "measures": ["dataset.enrollments.v1:enrollments"],
      "dimensions": []}, None),
])
def test_contract_errors(contract, match):
    if match is None:
        _compile(**contract)
        return
    with pytest.raises(CompileError, match=match):
        _compile(**contract)


def test_internal_datasets_cannot_be_queried():
    with pytest.raises(CompileError, match="internal"):
        _compile(catalog(dataset(visibility="internal")), measures=["dataset.enrollments.v1:enrollments"])


def test_non_cdm_tables_are_rejected():
    bad = dataset(base_sql="SELECT ID, PERSON_ID, COURSE_ID, COURSE_ROLE, ENROLLMENT_TIME, EMAIL FROM OTHER.SECRETS")
    with pytest.raises(CompileError, match="outside this database"):
        _compile(catalog(bad), measures=["dataset.enrollments.v1:enrollments"])


def test_refs_inline_dependencies_first_and_once():
    base = dataset(id="dataset.base.v1")
    mid = dataset(id="dataset.mid.v1", depends_on=["dataset.base.v1"],
                  base_sql="SELECT * FROM {{ ref('dataset.base.v1') }}")
    top = dataset(depends_on=["dataset.mid.v1", "dataset.base.v1"],
                  base_sql="SELECT m.* FROM {{ ref('dataset.mid.v1') }} m JOIN {{ ref('dataset.base.v1') }} b ON b.ID = m.ID")
    ctes = build_ctes(catalog(base, mid, top), ["dataset.enrollments.v1"], "DB")
    assert list(ctes) == ["DS_BASE_V1", "DS_MID_V1", "DS_ENROLLMENTS_V1"]
    q = _compile(catalog(base, mid, top), measures=["dataset.enrollments.v1:enrollments"])
    assert q.provenance.datasets == ["dataset.base.v1", "dataset.mid.v1", "dataset.enrollments.v1"]


def test_ref_must_be_declared_in_depends_on():
    base = dataset(id="dataset.base.v1")
    top = dataset(base_sql="SELECT * FROM {{ ref('dataset.base.v1') }}")
    with pytest.raises(CompileError, match="depends_on"):
        build_ctes(catalog(base, top), ["dataset.enrollments.v1"], "DB")


def test_dependency_cycles_are_rejected():
    a = dataset(id="dataset.a.v1", depends_on=["dataset.b.v1"], base_sql="SELECT * FROM {{ ref('dataset.b.v1') }}")
    b = dataset(id="dataset.b.v1", depends_on=["dataset.a.v1"], base_sql="SELECT * FROM {{ ref('dataset.a.v1') }}")
    with pytest.raises(CompileError, match="cycle"):
        build_ctes(catalog(a, b), ["dataset.a.v1"], "DB")


def test_template_injection_in_base_sql_is_a_compile_error():
    bad = dataset(base_sql="SELECT {{ ''.__class__ }} FROM {{ database }}.CDM_LMS.PERSON")
    with pytest.raises(CompileError, match="not allowed"):
        _compile(catalog(bad), measures=["dataset.enrollments.v1:enrollments"])


def test_provenance_lists_metrics_and_measures():
    q = _compile(metrics=["metric.student_enrollments.v1"])
    assert q.provenance.governed is True
    assert q.provenance.metrics == ["metric.student_enrollments.v1"]
    assert q.provenance.measures == ["dataset.enrollments.v1:enrollments"]


def test_time_dimension_filters_compare_whole_days():
    q = _compile(measures=["dataset.enrollments.v1:enrollments"],
                 filters=[{"dimension": "enrolled_at", "op": "eq", "values": ["2026-01-01"]}])
    assert "CAST(ENROLLMENT_TIME AS DATE) = '2026-01-01'" in q.sql


def test_metric_and_measure_with_the_same_output_name_collide():
    same_name = SemanticMetric(**(STUDENT_ENROLLMENTS.model_dump() | {"id": "metric.enrollments.v1"}))
    with pytest.raises(CompileError, match="collide"):
        _compile(catalog(metrics=(same_name,)),
                 metrics=["metric.enrollments.v1"], measures=["dataset.enrollments.v1:enrollments"])


@pytest.mark.parametrize("declared", [["email"], ["Email"]])
def test_pii_check_ignores_case(declared):
    with pytest.raises(CompileError, match="personally identifiable"):
        _compile(catalog(dataset(pii_columns=declared)),
                 measures=["dataset.enrollments.v1:enrollments"], dimensions=["email"])


def test_known_pii_column_names_cannot_be_selected_even_if_undeclared():
    with pytest.raises(CompileError, match="personally identifiable"):
        _compile(catalog(dataset(pii_columns=[])),
                 measures=["dataset.enrollments.v1:enrollments"], dimensions=["email"])


@pytest.mark.parametrize("table", [
    "OTHERDB.CDM_LMS.PERSON",
    'DB."cdm_lms".PERSON',
    '"OTHERDB".CDM_LMS.PERSON',
    "CDM_LMS.PERSON",
])
def test_tables_must_be_in_this_database_and_a_cdm_schema(table):
    bad = dataset(base_sql=f"SELECT ID, PERSON_ID, COURSE_ID, COURSE_ROLE, ENROLLMENT_TIME, EMAIL FROM {table}")
    with pytest.raises(CompileError, match="outside"):
        _compile(catalog(bad), measures=["dataset.enrollments.v1:enrollments"])


def test_scoped_count_star_counts_matching_rows():
    star = dataset(measures=[{"name": "rows", "agg": "count", "expr": "*"}])
    metric = SemanticMetric(**(STUDENT_ENROLLMENTS.model_dump() | {"id": "metric.student_rows.v1",
                                                                     "measure": "dataset.enrollments.v1:rows"}))
    q = _compile(catalog(star, metrics=(metric,)), metrics=["metric.student_rows.v1"])
    assert "COUNT(CASE WHEN COURSE_ROLE = 'S' THEN 1 END) AS student_rows" in q.sql


def test_grain_suffix_is_not_a_filter_dimension():
    with pytest.raises(CompileError, match="'enrolled_at__month' has a grain suffix; filters take the plain dimension name"):
        _compile(measures=["dataset.enrollments.v1:enrollments"],
                 filters=[{"dimension": "enrolled_at__month", "op": "not_null"}])


def test_order_by_a_grain_dimension():
    q = _compile(measures=["dataset.enrollments.v1:enrollments"], dimensions=["enrolled_at__month"],
                 order_by=[{"field": "enrolled_at__month", "direction": "asc"}])
    assert "ORDER BY\n  enrolled_at__month" in q.sql


def test_base_sql_ending_in_a_line_comment_still_compiles():
    commented = dataset(base_sql=ENROLLMENTS.base_sql + "\n-- trailing note")
    q = _compile(catalog(commented), measures=["dataset.enrollments.v1:enrollments"])
    sqlglot.parse_one(q.sql, read="snowflake")
