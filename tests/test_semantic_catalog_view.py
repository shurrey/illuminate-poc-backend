import json

import pytest
from pydantic import ConfigDict

from semantic_layer.compiler import CompileError, compile_query
from semantic_layer.contract import QueryContract
from semantic_layer.schema import Catalog, SemanticMetric

from semantic_layer.catalog_view import public_catalog
from tests.semantic_fixtures import ENROLLMENTS, STUDENT_ENROLLMENTS, catalog, dataset


def test_lists_public_datasets_without_base_sql_or_columns():
    view = public_catalog(catalog())
    [ds] = view["datasets"]
    assert ds["id"] == "dataset.enrollments.v1"
    text = json.dumps(view)
    assert "base_sql" not in text and "CDM_LMS" not in text
    assert all("column" not in d for d in ds["dimensions"]) and all("column" not in m for m in ds["measures"])


def test_pii_dimensions_are_marked_unselectable():
    dims = {d["name"]: d for d in public_catalog(catalog())["datasets"][0]["dimensions"]}
    assert dims["email"]["selectable"] is False
    assert dims["course_role"]["selectable"] is True


def test_internal_datasets_and_their_metrics_are_hidden():
    hidden = dataset(visibility="internal")
    view = public_catalog(catalog(hidden))
    assert view["datasets"] == [] and view["metrics"] == []


def test_metrics_carry_their_definition_metadata():
    [m] = public_catalog(catalog())["metrics"]
    assert m["id"] == STUDENT_ENROLLMENTS.id
    assert m["measure"] == "dataset.enrollments.v1:enrollments"
    assert m["default_filters"] == ["students"]
    assert m["last_reviewed"] == "2026-10-08"


def test_filters_expose_name_description_and_condition():
    [f] = public_catalog(catalog())["datasets"][0]["filters"]
    assert f == {"name": "students", "description": "", "sql": "COURSE_ROLE = 'S'"}


@pytest.mark.parametrize("declared", [["email"], []])
def test_selectable_agrees_with_the_compiler(declared):
    ds = dataset(pii_columns=declared + ["ID", "PERSON_ID"])
    dims = {d["name"]: d for d in public_catalog(catalog(ds))["datasets"][0]["dimensions"]}
    assert dims["email"]["selectable"] is False
    with pytest.raises(CompileError, match="personally identifiable"):
        compile_query(QueryContract(measures=["dataset.enrollments.v1:enrollments"], dimensions=["email"]),
                      catalog(ds), "DB")


def test_fields_added_to_definitions_are_not_published_automatically():
    class WithSecret(SemanticMetric):
        model_config = ConfigDict(frozen=True, extra="allow")

    leaky = WithSecret(**STUDENT_ENROLLMENTS.model_dump(), secret_sql="SELECT * FROM CDM_LMS.PERSON")
    view = public_catalog(Catalog(datasets={ENROLLMENTS.id: ENROLLMENTS}, metrics={leaky.id: leaky}))
    assert "secret_sql" not in json.dumps(view)


def test_datasets_list_the_public_datasets_whose_dimensions_they_can_use():
    from semantic_layer.catalog import load_catalog

    view = {d["id"]: d for d in public_catalog(load_catalog())["datasets"]}
    assert "dataset.courses.v1" in view["dataset.student_grade.v1"]["joins"]
    assert all(j in view for d in view.values() for j in d["joins"])


def test_datasets_say_when_they_require_a_time_range():
    from tests.semantic_fixtures import dataset

    view = public_catalog(catalog(dataset(required_time_range="enrolled_at")))
    assert view["datasets"][0]["required_time_range"] == "enrolled_at"


def _dataset_view(view, ds_id):
    return next(d for d in view["datasets"] if d["id"] == ds_id)


def test_measures_carry_their_expression_and_filters_their_condition():
    from semantic_layer.catalog import load_catalog
    grades = _dataset_view(public_catalog(load_catalog()), "dataset.student_grade.v1")
    measure = next(m for m in grades["measures"] if m["name"] == "average_grade_percentage")
    assert measure["expr"] == "GRADE_PERCENTAGE"
    ratio = next(m for m in grades["measures"] if m["agg"] == "ratio")
    assert ratio["expr"] is None
    assert grades["filters"] and all(isinstance(f["sql"], str) and f["sql"] for f in grades["filters"])


def test_expressions_reflect_the_tenants_overrides():
    from semantic_layer.catalog import load_catalog
    from semantic_layer.overlays import Overlay, apply_overlays
    target = "measure:dataset.student_grade.v1:average_grade_percentage"
    catalog = apply_overlays(load_catalog(), [Overlay(target=target, expr="ROUND(GRADE_PERCENTAGE, 0)", version=1)])
    measure = next(m for m in _dataset_view(public_catalog(catalog), "dataset.student_grade.v1")["measures"]
                   if m["name"] == "average_grade_percentage")
    assert measure["expr"] == "ROUND(GRADE_PERCENTAGE, 0)"
