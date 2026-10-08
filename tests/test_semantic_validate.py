from semantic_layer.validate import load_snapshot, output_columns, validate_dataset, validate_metric
from tests.semantic_fixtures import ENROLLMENTS, STUDENT_ENROLLMENTS, catalog, dataset

SNAPSHOT = load_snapshot()


def test_valid_dataset_has_no_errors_and_reports_outputs():
    assert validate_dataset(ENROLLMENTS, catalog(), SNAPSHOT) == []
    assert output_columns(ENROLLMENTS, catalog(), SNAPSHOT) == [
        "ID", "PERSON_ID", "COURSE_ID", "COURSE_ROLE", "ENROLLMENT_TIME", "EMAIL"]


def test_unknown_source_column_is_reported():
    bad = dataset(base_sql=ENROLLMENTS.base_sql.replace("pc.COURSE_ROLE", "pc.TERM_ID AS COURSE_ROLE"))
    errors = validate_dataset(bad, catalog(bad), SNAPSHOT)
    assert len(errors) == 1 and "TERM_ID" in errors[0]


def test_definition_columns_must_be_dataset_outputs():
    bad = dataset(dimensions=[{"name": "term", "column": "TERM_NAME", "type": "categorical"}],
                  measures=[{"name": "m", "agg": "sum", "expr": "CREDITS"}],
                  filters=[{"name": "f", "sql": "STATUS = 'X'"}], pii_columns=["EMAIL"])
    errors = "\n".join(validate_dataset(bad, catalog(bad), SNAPSHOT))
    assert "dimension term" in errors and "TERM_NAME" in errors
    assert "measure m" in errors and "CREDITS" in errors
    assert "filter f" in errors and "STATUS" in errors


def test_undeclared_pii_output_is_reported():
    bad = dataset(pii_columns=[])
    assert any("pii_columns" in e and "EMAIL" in e for e in validate_dataset(bad, catalog(bad), SNAPSHOT))


def test_validation_through_refs_uses_dependency_outputs():
    base = dataset(id="dataset.base.v1")
    top = dataset(depends_on=["dataset.base.v1"],
                  base_sql="SELECT ID, PERSON_ID, COURSE_ID, COURSE_ROLE, ENROLLMENT_TIME, EMAIL FROM {{ ref('dataset.base.v1') }}")
    assert validate_dataset(top, catalog(base, top), SNAPSHOT) == []


def test_metric_must_point_at_existing_measure_and_filters():
    assert validate_metric(STUDENT_ENROLLMENTS, catalog()) == []
    bad = type(STUDENT_ENROLLMENTS)(**(STUDENT_ENROLLMENTS.model_dump() | {"measure": "dataset.enrollments.v1:nope", "default_filters": ["zzz"]}))
    errors = "\n".join(validate_metric(bad, catalog()))
    assert "no measure 'nope'" in errors and "no filter 'zzz'" in errors
