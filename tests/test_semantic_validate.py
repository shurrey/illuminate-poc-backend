from semantic_layer.validate import (
    load_pii_columns, load_snapshot, output_columns, validate_dataset, validate_metric,
)
from tests.semantic_fixtures import ENROLLMENTS, STUDENT_ENROLLMENTS, catalog, dataset

SNAPSHOT = load_snapshot()
PII = load_pii_columns()


def test_valid_dataset_has_no_errors_and_reports_outputs():
    assert validate_dataset(ENROLLMENTS, catalog(), SNAPSHOT, PII) == []
    assert output_columns(ENROLLMENTS, catalog(), SNAPSHOT) == [
        "ID", "PERSON_ID", "COURSE_ID", "COURSE_ROLE", "ENROLLMENT_TIME", "EMAIL"]


def test_unknown_source_column_is_reported():
    bad = dataset(base_sql=ENROLLMENTS.base_sql.replace("pc.COURSE_ROLE", "pc.TERM_ID AS COURSE_ROLE"))
    errors = validate_dataset(bad, catalog(bad), SNAPSHOT, PII)
    assert len(errors) == 1 and "TERM_ID" in errors[0]


def test_definition_columns_must_be_dataset_outputs():
    bad = dataset(dimensions=[{"name": "term", "column": "TERM_NAME", "type": "categorical"}],
                  measures=[{"name": "m", "agg": "sum", "expr": "CREDITS"}],
                  filters=[{"name": "f", "sql": "STATUS = 'X'"}])
    errors = "\n".join(validate_dataset(bad, catalog(bad), SNAPSHOT, PII))
    assert "dimension term" in errors and "TERM_NAME" in errors
    assert "measure m" in errors and "CREDITS" in errors
    assert "filter f" in errors and "STATUS" in errors


def test_undeclared_pii_output_is_reported():
    bad = dataset(pii_columns=["ID", "PERSON_ID"])
    assert any("pii_columns" in e and "EMAIL" in e for e in validate_dataset(bad, catalog(bad), SNAPSHOT, PII))


def test_validation_through_refs_uses_dependency_outputs():
    base = dataset(id="dataset.base.v1")
    top = dataset(depends_on=["dataset.base.v1"],
                  base_sql="SELECT ID, PERSON_ID, COURSE_ID, COURSE_ROLE, ENROLLMENT_TIME, EMAIL FROM {{ ref('dataset.base.v1') }}")
    assert validate_dataset(top, catalog(base, top), SNAPSHOT, PII) == []


def test_metric_must_point_at_existing_measure_and_filters():
    assert validate_metric(STUDENT_ENROLLMENTS, catalog()) == []
    bad = type(STUDENT_ENROLLMENTS)(**(STUDENT_ENROLLMENTS.model_dump() | {"measure": "dataset.enrollments.v1:nope", "default_filters": ["zzz"]}))
    errors = "\n".join(validate_metric(bad, catalog()))
    assert "no measure 'nope'" in errors and "no filter 'zzz'" in errors


def test_snapshot_pii_flags_cover_person_identifiers():
    assert {"CDM_LMS.PERSON.EMAIL", "CDM_LMS.PERSON.BIRTH_DATE", "CDM_LMS.PERSON_COURSE.PERSON_ID"} <= PII
    assert "CDM_LMS.PERSON_COURSE.COURSE_ROLE" not in PII


def test_output_from_a_dictionary_flagged_column_must_be_declared():
    bad = dataset(pii_columns=["EMAIL", "ID"])
    errors = validate_dataset(bad, catalog(bad), SNAPSHOT, PII)
    assert any("PERSON_ID" in e and "pii_columns" in e for e in errors)


def test_pii_is_traced_through_expressions_and_refs():
    base = dataset(id="dataset.base.v1")
    top = dataset(
        depends_on=["dataset.base.v1"],
        base_sql="SELECT b.ID, b.PERSON_ID, b.COURSE_ID, b.COURSE_ROLE, b.ENROLLMENT_TIME, "
                 "LOWER(b.EMAIL) AS EMAIL, YEAR(p.BIRTH_DATE) AS BIRTH_YEAR "
                 "FROM {{ ref('dataset.base.v1') }} b JOIN {{ database }}.CDM_LMS.PERSON p ON p.ID = b.PERSON_ID",
    )
    errors = validate_dataset(top, catalog(base, top), SNAPSHOT, PII)
    assert any("BIRTH_YEAR" in e for e in errors)
    assert not any("'EMAIL'" in e for e in errors)


def test_dimension_types_must_match_the_output_column_type():
    bad = dataset(dimensions=[
        {"name": "role_flag", "column": "COURSE_ID", "type": "boolean"},
        {"name": "when", "column": "COURSE_ROLE", "type": "time", "grains": ["day"]},
    ])
    errors = "\n".join(validate_dataset(bad, catalog(bad), SNAPSHOT, PII))
    assert "role_flag" in errors and "boolean" in errors
    assert "when" in errors and "time" in errors


def test_matching_dimension_types_pass():
    ok = dataset(dimensions=[
        {"name": "enrolled_at", "column": "ENROLLMENT_TIME", "type": "time", "grains": ["day"]},
        {"name": "course_id", "column": "COURSE_ID", "type": "numeric"},
        {"name": "course_role", "column": "COURSE_ROLE", "type": "categorical"},
    ])
    assert validate_dataset(ok, catalog(ok), SNAPSHOT, PII) == []
