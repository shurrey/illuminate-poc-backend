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
    entities=[
        {"name": "person_course", "column": "ID", "type": "primary"},
        {"name": "course", "column": "COURSE_ID", "type": "foreign"},
    ],
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

COURSES = Dataset(
    id="dataset.courses.v1",
    display_name="Courses",
    description="test",
    grain="one row per course",
    domain="test",
    source="test",
    base_sql="SELECT c.ID AS COURSE_ID, c.NAME AS COURSE_NAME, c.START_DATE "
             "FROM {{ database }}.CDM_LMS.COURSE c",
    complete=True,
    entities=[{"name": "course", "column": "COURSE_ID", "type": "primary"}],
    dimensions=[
        {"name": "course_name", "column": "COURSE_NAME", "type": "categorical"},
        {"name": "course_start", "column": "START_DATE", "type": "time", "grains": ["month"]},
        {"name": "course_role", "column": "COURSE_NAME", "type": "categorical"},
    ],
    measures=[{"name": "courses", "agg": "count_distinct", "expr": "COURSE_ID"}],
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
