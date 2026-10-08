import pytest
import yaml

from semantic_layer.catalog import CatalogError, load_catalog
from tests.semantic_fixtures import ENROLLMENTS, STUDENT_ENROLLMENTS


def _write(root, datasets=(), metrics=()):
    (root / "datasets" / "test").mkdir(parents=True)
    (root / "metrics").mkdir()
    for i, ds in enumerate(datasets):
        (root / "datasets" / "test" / f"d{i}.yaml").write_text(yaml.safe_dump(ds))
    (root / "metrics" / "test.yaml").write_text(yaml.safe_dump({"metrics": list(metrics)}))


def _metric():
    return STUDENT_ENROLLMENTS.model_dump(mode="json")


def test_loads_datasets_and_metrics(tmp_path):
    _write(tmp_path, [ENROLLMENTS.model_dump()], [_metric()])
    cat = load_catalog(tmp_path)
    assert list(cat.datasets) == ["dataset.enrollments.v1"]
    assert list(cat.metrics) == ["metric.student_enrollments.v1"]


def test_missing_directories_load_as_empty(tmp_path):
    cat = load_catalog(tmp_path)
    assert cat.datasets == {} and cat.metrics == {}


def test_duplicate_dataset_id_is_an_error(tmp_path):
    _write(tmp_path, [ENROLLMENTS.model_dump(), ENROLLMENTS.model_dump()])
    with pytest.raises(CatalogError, match="duplicate dataset id"):
        load_catalog(tmp_path)


def test_invalid_definition_names_its_file(tmp_path):
    _write(tmp_path, [ENROLLMENTS.model_dump() | {"grain": None}])
    with pytest.raises(CatalogError, match="d0.yaml"):
        load_catalog(tmp_path)


def test_duplicate_metric_id_is_an_error(tmp_path):
    _write(tmp_path, [ENROLLMENTS.model_dump()], [_metric(), _metric()])
    with pytest.raises(CatalogError, match="duplicate metric id"):
        load_catalog(tmp_path)
