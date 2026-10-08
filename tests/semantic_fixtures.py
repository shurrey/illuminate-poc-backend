"""Small in-memory catalogs for compiler and validator tests."""

from semantic_layer.schema import Catalog, Dataset, SemanticMetric

ENROLLMENTS = Dataset(
    id="dataset.enrollments.v1",
    display_name="Enrollments",
    description="test",
    grain="one row per enrollment",
    domain="test",
    source="test",
    base_sql="SELECT pc.ID, pc.PERSON_ID, pc.COURSE_ID, pc.COURSE_ROLE, pc.ENROLLMENT_TIME, p.EMAIL "
             "FROM {{ database }}.CDM_LMS.PERSON_COURSE pc JOIN {{ database }}.CDM_LMS.PERSON p ON p.ID = pc.PERSON_ID",
    entities=[{"name": "person_course", "column": "ID", "type": "primary"}],
    dimensions=[
        {"name": "course_role", "column": "COURSE_ROLE", "type": "categorical"},
        {"name": "enrolled_at", "column": "ENROLLMENT_TIME", "type": "time", "grains": ["day", "month"]},
        {"name": "email", "column": "EMAIL", "type": "categorical"},
    ],
    measures=[
        {"name": "enrollments", "agg": "count", "expr": "ID"},
        {"name": "people", "agg": "count_distinct", "expr": "PERSON_ID"},
        {"name": "per_person", "agg": "ratio", "numerator": "enrollments", "denominator": "people"},
    ],
    filters=[{"name": "students", "sql": "COURSE_ROLE = 'S'"}],
    pii_columns=["EMAIL", "ID", "PERSON_ID"],
)

STUDENT_ENROLLMENTS = SemanticMetric(
    id="metric.student_enrollments.v1",
    display_name="Student enrollments",
    description="test",
    owner="Blackboard",
    authority="vendor-canonical",
    last_reviewed="2026-10-08",
    measure="dataset.enrollments.v1:enrollments",
    default_filters=["students"],
)


def catalog(*datasets: Dataset, metrics: tuple[SemanticMetric, ...] = (STUDENT_ENROLLMENTS,)) -> Catalog:
    datasets = datasets or (ENROLLMENTS,)
    return Catalog(datasets={d.id: d for d in datasets}, metrics={m.id: m for m in metrics})


def dataset(**overrides) -> Dataset:
    return Dataset(**(ENROLLMENTS.model_dump() | overrides))
