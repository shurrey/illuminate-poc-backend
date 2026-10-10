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


def test_unqualified_tables_must_name_a_cte_in_scope():
    from semantic_layer.compiler import _check_tables

    nested = "WITH DS_A AS (WITH inner_cte AS (SELECT 1 AS X FROM DB.CDM_LMS.T) SELECT X FROM inner_cte) SELECT X FROM DS_A"
    _check_tables(nested, "DB")
    leaked = ("WITH DS_A AS (WITH inner_cte AS (SELECT 1 AS X FROM DB.CDM_LMS.T) SELECT X FROM inner_cte), "
              "DS_B AS (SELECT X FROM inner_cte) SELECT X FROM DS_B")
    with pytest.raises(CompileError, match="inner_cte"):
        _check_tables(leaked, "DB")


def test_generator_is_the_only_table_function_allowed():
    from semantic_layer.compiler import _check_tables

    _check_tables("SELECT SEQ4() FROM TABLE(GENERATOR(ROWCOUNT => 10))", "DB")
    with pytest.raises(CompileError):
        _check_tables("SELECT * FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))", "DB")


def test_a_dataset_can_require_a_time_range_on_one_of_its_dimensions():
    from tests.semantic_fixtures import dataset

    ds = dataset(required_time_range="enrolled_at")
    cat = catalog(ds)
    with pytest.raises(CompileError, match="time_range on enrolled_at"):
        _compile(cat, measures=["dataset.enrollments.v1:enrollments"])
    with pytest.raises(CompileError, match="start"):
        _compile(cat, measures=["dataset.enrollments.v1:enrollments"], time_range={"dimension": "enrolled_at", "end": "2026-06-30"})
    assert _compile(cat, measures=["dataset.enrollments.v1:enrollments"],
                    time_range={"dimension": "enrolled_at", "start": "2026-01-01"}).sql


def test_a_required_time_range_must_name_a_time_dimension():
    from pydantic import ValidationError
    from tests.semantic_fixtures import dataset

    with pytest.raises(ValidationError):
        dataset(required_time_range="course_role")


@pytest.mark.parametrize("dimension,values,message", [
    ("active", ["abc"], "number"),
    ("course_ended", [True], "number"),
    ("last_access", ["not a date"], "date"),
])
def test_filter_values_must_match_the_dimension_type(dimension, values, message):
    from semantic_layer.catalog import load_catalog

    with pytest.raises(CompileError, match=message):
        compile_query(QueryContract(measures=["dataset.course_student_activity.v1:students"],
                                    filters=[{"dimension": dimension, "op": "eq", "values": values}]),
                      load_catalog(), "DB")


def test_well_typed_filter_values_compile():
    from semantic_layer.catalog import load_catalog

    compile_query(QueryContract(measures=["dataset.course_student_activity.v1:students"],
                                filters=[{"dimension": "active", "op": "eq", "values": [1]},
                                         {"dimension": "last_access", "op": "gte", "values": ["2026-01-01"]}]),
                  load_catalog(), "DB")


def test_a_quoted_table_name_only_matches_a_cte_of_exactly_that_name():
    from semantic_layer.compiler import _check_tables

    _check_tables('WITH "a" AS (SELECT 1 AS X FROM DB.CDM_LMS.T) SELECT X FROM "a"', "DB")
    with pytest.raises(CompileError):
        _check_tables('WITH a AS (SELECT 1 AS X FROM DB.CDM_LMS.T) SELECT X FROM "a"', "DB")


@pytest.mark.parametrize("sql", [
    "SELECT SEQ4() FROM TABLE(GENERATOR(ROWCOUNT => 100000000))",
    "SELECT SEQ4() FROM TABLE(GENERATOR(TIMELIMIT => 60))",
])
def test_generators_are_bounded(sql):
    from semantic_layer.compiler import _check_tables

    with pytest.raises(CompileError):
        _check_tables(sql, "DB")


def test_a_required_time_range_must_be_on_the_dataset_itself():
    from tests.semantic_fixtures import dataset

    ds = dataset(required_time_range="enrolled_at")
    with pytest.raises(CompileError, match="time_range"):
        _compile(catalog(ds), measures=["dataset.enrollments.v1:enrollments"],
                 time_range={"dimension": "dataset.other.v1:enrolled_at", "start": "2026-01-01"})


def _real(**contract):
    from semantic_layer.catalog import load_catalog
    return compile_query(QueryContract(**contract), load_catalog(), "DB").sql


CSA_STUDENTS = ["dataset.course_student_activity.v1:students"]


def _flat(sql: str) -> str:
    return " ".join(sql.replace("(", "( ").split()).replace("( ", "(")


def test_a_filter_on_a_dataset_that_cannot_join_compiles_as_a_semi_join():
    sql = _real(measures=CSA_STUDENTS,
                filters=[{"dimension": "dataset.course_filters_ih.v1:ih_level_1", "op": "eq", "values": ["Nursing"]}])
    outer = _outer(sql)
    assert "COURSE_ID IN (SELECT COURSE_ID FROM DS_COURSE_FILTERS_IH_V1" in _flat(outer)
    assert "JOIN DS_COURSE_FILTERS_IH_V1" not in outer
    assert "DS_COURSE_FILTERS_IH_V1 AS (" in sql


def test_a_semi_join_uses_the_entity_that_is_primary_in_the_filtered_dataset():
    sql = _flat(_outer(_real(measures=CSA_STUDENTS,
        filters=[{"dimension": "dataset.sis_enrollment_attributes.v1:program", "op": "in", "values": ["BSN"]}])))
    assert "PERSON_COURSE_ID IN (SELECT PERSON_COURSE_ID FROM DS_SIS_ENROLLMENT_ATTRIBUTES_V1" in sql


def test_a_dataset_that_cannot_join_still_cannot_supply_a_dimension():
    with pytest.raises(CompileError, match="cannot supply dimensions|cannot be reached"):
        _real(measures=CSA_STUDENTS, dimensions=["dataset.course_filters_ih.v1:ih_level_1"])


def test_a_filter_on_a_dataset_sharing_no_entity_is_still_refused():
    with pytest.raises(CompileError, match="cannot be reached"):
        _real(measures=CSA_STUDENTS,
              filters=[{"dimension": "dataset.collab_sessions_by_slot.v1:slot_label", "op": "eq", "values": ["8 AM"]}])


def test_enrollment_measures_group_by_the_new_course_grain_dimensions():
    sql = _real(measures=CSA_STUDENTS, dimensions=[
        "dataset.courses.v1:course_duration", "dataset.courses.v1:delivery_method",
        "dataset.courses.v1:course_weeks", "dataset.courses.v1:course_creation_date__month"])
    outer = _outer(sql)
    assert "LEFT JOIN DS_COURSES_V1" in outer
    for column in ("COURSE_DURATION", "DELIVERY_METHOD", "COURSE_WEEKS", "COURSE_CREATION_DATE"):
        assert column in outer


GRT = "dataset.grade_response_time.v1"


def test_grading_definitions_compile_and_has_due_date_filters():
    sql = _outer(_real(
        measures=[f"{GRT}:{m}" for m in ("ungraded_attempts", "min_response_days", "max_response_days", "share_ungraded")],
        dimensions=["gradebook_name", "response_days_capped", "due_time__week"],
        filters=[{"dimension": "has_due_date", "op": "eq", "values": [True]}]))
    assert "HAS_DUE_DATE = TRUE" in sql and "GRADEBOOK_NAME" in sql and "RESPONSE_DAYS_CAPPED" in sql


def test_platform_definitions_compile():
    assert "LAST_ACCESSED_DATE" in _outer(_real(measures=["dataset.lms_sessions.v1:sessions"], dimensions=["session_end_date__day"]))
    assert "COUNT(*)" in _outer(_real(measures=["dataset.collab_sessions_by_slot.v1:session_slots"], dimensions=["slot_label"]))


def test_a_semi_join_matches_every_shared_entity_when_the_targets_key_is_not_shared():
    sql = _flat(_outer(_real(measures=["dataset.lms_course_logins.v1:people"],
        filters=[{"dimension": "dataset.sis_enrollment_attributes.v1:program", "op": "in", "values": ["BSN"]}])))
    assert "(COURSE_ID, PERSON_ID) IN (SELECT COURSE_ID, PERSON_ID FROM DS_SIS_ENROLLMENT_ATTRIBUTES_V1" in sql


@pytest.mark.parametrize("ds", ["dataset.lms_sessions_by_slot.v1", "dataset.collab_sessions_by_slot.v1"])
def test_slots_are_cut_in_the_institutions_timezone_and_count_the_days_present(ds):
    from semantic_layer.catalog import load_catalog
    from semantic_layer.render import cte_name
    cat = load_catalog()
    assert "CONVERT_TIMEZONE" in build_ctes(cat, [ds], "DB")[cte_name(ds)]
    assert "COUNT(DISTINCT SLOT_DATE)" in _outer(_real(measures=[f"{ds}:days"], dimensions=["day_of_week"]))


def test_course_tool_activity_is_in_local_time_with_weekday_and_three_hour_groups():
    from semantic_layer.catalog import load_catalog
    cte = build_ctes(load_catalog(), ["dataset.course_tool_activity.v1"], "DB")["DS_COURSE_TOOL_ACTIVITY_V1"]
    assert "CONVERT_TIMEZONE" in cte
    sql = _outer(_real(measures=["dataset.course_tool_activity.v1:minutes"],
                       dimensions=["activity_date__day", "day_of_week", "hour_group", "hour_group_start"]))
    assert all(c in sql for c in ("ACTIVITY_DATE", "DAY_NAME", "HOUR_GROUP", "HOUR_GROUP_START"))


CS = "dataset.collab_sessions.v1"
TERM_FILTER = {"dimension": "dataset.collab_session_courses.v1:term_name", "op": "in", "values": ["Fall 2022"]}


def test_collab_session_statistics_aggregate_session_rows_without_a_join():
    sql = _outer(_real(measures=[f"{CS}:{m}" for m in (
        "sessions", "rooms", "total_minutes", "avg_minutes", "median_minutes", "max_minutes", "min_minutes",
        "avg_attendees", "median_attendees", "max_attendees", "min_attendees")],
        dimensions=["start_date__day"], filters=[{"dimension": "in_course", "op": "in", "values": ["Yes"]}]))
    assert "MEDIAN(MINUTES)" in sql and "MIN(ATTENDEE_COUNT)" in sql and "IN_COURSE IN ('Yes')" in sql
    assert "JOIN" not in sql


@pytest.mark.parametrize("measure", [f"{CS}:sessions", "dataset.collab_events.v1:events",
                                     "dataset.collab_attendance.v1:attendees"])
def test_a_term_filter_narrows_collab_data_by_session_through_the_session_course_bridge(measure):
    sql = _flat(_outer(_real(measures=[measure], filters=[TERM_FILTER])))
    assert "SESSION_ID IN (SELECT SESSION_ID FROM DS_COLLAB_SESSION_COURSES_V1" in sql
    assert "JOIN" not in sql


def test_collab_events_count_hands_and_shown_polls_by_local_day():
    from semantic_layer.catalog import load_catalog
    cte = build_ctes(load_catalog(), ["dataset.collab_events.v1"], "DB")["DS_COLLAB_EVENTS_V1"]
    assert "CONVERT_TIMEZONE" in cte and "session_instance_uid" in cte and "NETSTATS" not in cte
    sql = _outer(_real(measures=["dataset.collab_events.v1:" + m for m in ("events", "sessions", "hands_raised", "polls_shown", "sessions_with_polls")],
                       dimensions=["event_date__day", "event_group", "event_label"],
                       filters=[{"dimension": "in_course", "op": "in", "values": ["Yes"]}]))
    assert "COUNT(DISTINCT SESSION_ID)" in sql and "EVENT_GROUP" in sql
    assert "COUNT(DISTINCT IFF(POLL_SHOWN = 1, SESSION_ID, NULL))" in sql


CR = "dataset.course_readiness.v1"


def test_course_readiness_counts_each_course_once_and_reads_course_attributes_by_key():
    sql = _outer(_real(measures=[f"{CR}:{m}" for m in ("courses", "ready_courses", "pct_ready", "pct_not_ready")],
                       dimensions=["readiness", "instructor_enrolled", "students_enrolled", "items_updated", "available",
                                   "dataset.courses.v1:term_name", "dataset.courses.v1:ih_level_1"]))
    assert "LEFT JOIN DS_COURSES_V1" in sql and "COUNT(DISTINCT" in sql
    from semantic_layer.catalog import load_catalog
    cte = build_ctes(load_catalog(), [CR], "DB")["DS_COURSE_READINESS_V1"]
    assert "ZEROIFNULL" in cte and "ROW_DELETED_TIME IS NULL" in cte


AI = "dataset.course_item_ai_usage.v1"


def test_ai_usage_counts_courses_once_and_keeps_creators_identifiable():
    from semantic_layer.catalog import load_catalog
    cat = load_catalog()
    sql = _outer(_real(measures=[f"{AI}:{m}" for m in ("items", "ai_items", "pct_ai_items", "courses", "ai_courses",
                                                         "ai_creators", "available_ai_items", "unavailable_ai_items")],
                       dimensions=["item_type_name", "ai_used", "course_uses_ai", "created_date__week", "dataset.courses.v1:ih_level_1"]))
    assert "COUNT(DISTINCT IFF(AI_USED = 'Yes', COURSE_ID, NULL))" in sql.replace("b.", "") and "LEFT JOIN DS_COURSES_V1" in sql
    ds = cat.datasets[AI]
    assert all(ds.is_pii(ds.dimension(n).column) for n in ("creator_name", "creator_email"))
    assert "CONVERT_TIMEZONE" in build_ctes(cat, [AI], "DB")[f"DS_COURSE_ITEM_AI_USAGE_V1"]
