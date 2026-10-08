import json

from semantic_layer.catalog_view import public_catalog
from tests.semantic_fixtures import ENROLLMENTS, STUDENT_ENROLLMENTS, catalog, dataset


def test_lists_public_datasets_without_sql_or_columns():
    view = public_catalog(catalog())
    [ds] = view["datasets"]
    assert ds["id"] == "dataset.enrollments.v1"
    text = json.dumps(view)
    assert "base_sql" not in text and "CDM_LMS" not in text
    assert "COURSE_ROLE = 'S'" not in text
    assert all("column" not in d and "expr" not in d for d in ds["dimensions"] + ds["measures"])


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


def test_filters_expose_name_and_description_only():
    [f] = public_catalog(catalog())["datasets"][0]["filters"]
    assert f == {"name": "students", "description": ""}
