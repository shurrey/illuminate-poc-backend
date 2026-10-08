import pytest
from pydantic import ValidationError

from semantic_layer.contract import ContractFilter, QueryContract, TimeRange
from semantic_layer.schema import DatasetDimension, Measure, SemanticMetric
from tests.semantic_fixtures import ENROLLMENTS


def test_ratio_measure_requires_numerator_and_denominator():
    with pytest.raises(ValidationError, match="ratio needs numerator"):
        Measure(name="r", agg="ratio", numerator="a")


def test_plain_measure_rejects_ratio_fields():
    with pytest.raises(ValidationError, match="needs expr only"):
        Measure(name="m", agg="sum", expr="X", numerator="a")


def test_grains_rejected_on_non_time_dimension():
    with pytest.raises(ValidationError, match="only valid on time"):
        DatasetDimension(name="d", column="C", type="categorical", grains=["day"])


def test_dataset_rejects_duplicate_dimension_and_measure_names():
    raw = ENROLLMENTS.model_dump()
    raw["measures"].append({"name": "course_role", "agg": "count", "expr": "ID"})
    with pytest.raises(ValidationError, match="must be unique"):
        type(ENROLLMENTS)(**raw)


def test_dataset_rejects_ratio_over_unknown_measure():
    raw = ENROLLMENTS.model_dump()
    raw["measures"].append({"name": "bad", "agg": "ratio", "numerator": "nope", "denominator": "people"})
    with pytest.raises(ValidationError, match="non-ratio measures"):
        type(ENROLLMENTS)(**raw)


def test_unknown_fields_are_rejected():
    raw = ENROLLMENTS.model_dump() | {"tenant_id": "x"}
    with pytest.raises(ValidationError):
        type(ENROLLMENTS)(**raw)


def test_metric_measure_must_name_a_dataset_measure():
    with pytest.raises(ValidationError):
        SemanticMetric(id="metric.x.v1", display_name="x", description="x", owner="o", authority="a",
                       last_reviewed="2026-01-01", measure="enrollments")


@pytest.mark.parametrize("op,values", [("eq", []), ("between", [1]), ("is_null", [1]), ("in", [])])
def test_filter_arity_is_enforced(op, values):
    with pytest.raises(ValidationError, match="takes"):
        ContractFilter(dimension="d", op=op, values=values)


def test_filter_keeps_json_true_as_bool():
    assert ContractFilter(dimension="d", op="eq", values=[True]).values == [True]


def test_contract_needs_a_metric_or_measure():
    with pytest.raises(ValidationError, match="at least one"):
        QueryContract(dimensions=["course_role"])


@pytest.mark.parametrize("limit", [0, 1001])
def test_contract_limit_bounds(limit):
    with pytest.raises(ValidationError):
        QueryContract(metrics=["metric.x.v1"], limit=limit)


def test_time_range_rejects_inverted_bounds():
    with pytest.raises(ValidationError, match="after end"):
        TimeRange(dimension="d", start="2026-02-01", end="2026-01-01")
