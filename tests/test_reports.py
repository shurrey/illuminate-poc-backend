from datetime import date
from pathlib import Path

import pytest

from semantic_layer.catalog import load_catalog
from semantic_layer.reports import (REPORTS_DIR, ReportError, ReportValueError, load_reports, merged_contract, resolve_defaults,
                                    validate_report)

FIXTURES = Path(__file__).parent / "fixtures" / "reports"
CATALOG = load_catalog()


@pytest.fixture
def report():
    return load_reports(FIXTURES)["report.sample.v1"]


def _visual(report, vid):
    return next(v for p in report.pages for v in p.visuals if v.id == vid)


def test_the_fixture_report_loads_and_validates(report):
    assert [v.id for v in report.pages[0].visuals] == ["students", "sessions_by_day", "about", "roster", "roster_by_term"]
    assert validate_report(report, CATALOG) == []


def test_validation_names_the_visual_and_query_that_fail(report):
    broken = report.model_copy(deep=True)
    broken.pages[0].visuals[0].queries["current"] = {"measures": ["dataset.course_student_activity.v1:nope"]}
    [problem] = validate_report(broken, CATALOG)
    assert problem.startswith("students/current:")


def test_validation_flags_an_encode_column_the_query_does_not_return(report):
    broken = report.model_copy(deep=True)
    broken.pages[0].visuals[0].encode["value"] = "not_a_column"
    [problem] = validate_report(broken, CATALOG)
    assert "not_a_column" in problem


def test_term_and_date_values_merge_into_the_contract(report):
    contract, ignored = merged_contract(report, _visual(report, "students"), "current",
                                        {"term": ["Fall 2026"], "dates": {"start": "2026-08-01", "end": "2026-12-31"}}, CATALOG)
    assert ignored == []
    assert [(f.dimension, f.op, f.values) for f in contract.filters] == [("dataset.courses.v1:term_name", "in", ["Fall 2026"])]
    assert (contract.time_range.dimension, str(contract.time_range.start)) == ("dataset.courses.v1:course_start", "2026-08-01")


def test_a_hierarchy_filter_reaches_an_enrollment_dataset(report):
    contract, ignored = merged_contract(report, _visual(report, "students"), "current", {"node": ["Nursing"]}, CATALOG)
    assert ignored == [] and contract.filters[0].dimension == "dataset.course_filters_ih.v1:ih_level_1"


def test_filters_a_dataset_cannot_reach_are_ignored_and_reported(report):
    contract, ignored = merged_contract(report, _visual(report, "sessions_by_day"), "main",
                                        {"term": ["Fall 2026"], "node": ["Nursing"], "dates": {"start": "2026-09-01"}}, CATALOG)
    assert ignored == ["term", "node"]
    assert contract.filters == [] and contract.time_range.dimension == "session_date"


def test_empty_values_add_nothing(report):
    contract, ignored = merged_contract(report, _visual(report, "students"), "current", {"term": [], "dates": {}}, CATALOG)
    assert (contract.filters, contract.time_range, ignored) == ([], None, [])


TERMS = [
    {"term_name": "Q4: 2026", "term_start": "2026-04-01", "term_end": "2026-06-30"},
    {"term_name": "FY: 2026", "term_start": "2025-07-01", "term_end": "2026-06-30"},
    {"term_name": "Q3: 2026", "term_start": "2026-01-01", "term_end": "2026-03-31"},
    {"term_name": "Undated", "term_start": None, "term_end": None},
]


def test_current_term_is_every_term_spanning_today(report):
    assert resolve_defaults(report, date(2026, 5, 1), TERMS)["term"] == ["Q4: 2026", "FY: 2026"]


def test_between_terms_current_term_is_every_term_with_the_latest_end(report):
    defaults = resolve_defaults(report, date(2026, 10, 9), TERMS)
    assert defaults["term"] == ["Q4: 2026", "FY: 2026"]
    assert defaults["dates"] == {"start": "2026-09-10", "end": "2026-10-09"}
    assert "node" not in defaults


def test_duplicate_report_ids_are_refused(tmp_path):
    for name in ("a.yaml", "b.yaml"):
        (tmp_path / name).write_text((FIXTURES / "sample.yaml").read_text())
    with pytest.raises(ReportError, match="duplicate"):
        load_reports(tmp_path)


@pytest.mark.parametrize("report_id", sorted(load_reports(REPORTS_DIR)))
def test_every_canonical_report_validates(report_id):
    assert validate_report(load_reports(REPORTS_DIR)[report_id], CATALOG) == []


from semantic_layer.reports import Report, ReportValueError


def _report(filters, visuals):
    return Report(id="report.probe.v1", title="Probe", area="leading", description="", filters=filters,
                  pages=[{"title": "P", "visuals": visuals}])


TERM = {"id": "term", "label": "Term", "control": "multi_select", "dimension": "dataset.courses.v1:term_name"}
DATES = {"id": "dates", "label": "Dates", "control": "date_range", "time_dimension": "event_time"}
LOG = {"id": "log", "type": "kpi", "title": "Events",
       "queries": {"main": {"measures": ["dataset.activity_log.v1:events"]}}}
GRT_KPI = {"id": "grading", "type": "kpi", "title": "Attempts",
           "queries": {"main": {"measures": ["dataset.grade_response_time.v1:attempts"]}}}


def test_a_reachable_filter_is_applied_whatever_order_the_filters_merge_in():
    report = _report([TERM, DATES], [LOG])
    contract, ignored = merged_contract(report, report.visual("log"), "main",
                                        {"term": ["Q4: 2026"], "dates": {"start": "2026-09-01"}}, CATALOG)
    assert ignored == []
    assert contract.filters[0].dimension == "dataset.courses.v1:term_name" and contract.time_range.dimension == "event_time"


def test_string_values_are_converted_to_the_dimensions_type():
    due = {"id": "due", "label": "Has due date", "control": "select", "dimension": "has_due_date"}
    days = {"id": "days", "label": "Days", "control": "select", "dimension": "response_days_value"}
    report = _report([due, days], [GRT_KPI])
    contract, ignored = merged_contract(report, report.visual("grading"), "main", {"due": ["true"], "days": ["3"]}, CATALOG)
    assert ignored == []
    assert [f.values for f in contract.filters] == [[True], [3]]


def test_a_value_of_the_wrong_type_is_an_error_not_an_ignored_filter():
    days = {"id": "days", "label": "Days", "control": "select", "dimension": "response_days_value"}
    report = _report([days], [GRT_KPI])
    with pytest.raises(ReportValueError, match="days"):
        merged_contract(report, report.visual("grading"), "main", {"days": ["soon"]}, CATALOG)
    dated = _report([DATES], [LOG])
    with pytest.raises(ReportValueError, match="dates"):
        merged_contract(dated, dated.visual("log"), "main", {"dates": ["2026-01-01"]}, CATALOG)


def test_validation_flags_a_filter_dimension_that_does_not_exist():
    typo = {**TERM, "dimension": "dataset.courses.v1:term_nmae"}
    assert any("term" in p and "term_nmae" in p for p in validate_report(_report([typo], [GRT_KPI]), CATALOG))


def test_validation_flags_a_filter_no_visual_can_apply():
    slots = {"id": "slot", "label": "Slot", "control": "select", "dimension": "dataset.collab_sessions_by_slot.v1:slot_label"}
    assert any(p.startswith("filter slot:") for p in validate_report(_report([slots], [GRT_KPI]), CATALOG))


def test_validation_flags_a_transform_reading_a_query_that_does_not_exist():
    pop = {**GRT_KPI, "transform": {"kind": "period_over_period", "value": "main", "baseline": "previous", "field": "attempts"}}
    assert any("previous" in p for p in validate_report(_report([], [pop]), CATALOG))


COMPARISON = {"id": "comparison", "label": "Comparison", "control": "date_range",
              "time_dimension": "dataset.activity_log.v1:event_time", "default": "previous_30_days"}
PRIMARY = {**DATES, "time_dimension": "dataset.activity_log.v1:event_time", "default": "last_30_days"}
BOTH = {"dates": {"start": "2026-09-01", "end": "2026-09-30"}, "comparison": {"start": "2026-08-01", "end": "2026-08-31"}}


def _pair():
    visual = {"id": "users", "type": "kpi", "title": "Users",
              "queries": {"current": {"measures": ["dataset.activity_log.v1:events"]},
                          "previous": {"measures": ["dataset.activity_log.v1:events"], "date_filter": "comparison"}}}
    return _report([PRIMARY, COMPARISON], [visual])


def test_a_comparison_query_takes_only_the_comparison_range():
    report = _pair()
    contract, ignored = merged_contract(report, report.visual("users"), "previous", BOTH, CATALOG)
    assert (str(contract.time_range.start), str(contract.time_range.end), ignored) == ("2026-08-01", "2026-08-31", [])


def test_a_query_without_a_date_filter_takes_only_the_first_date_range():
    report = _pair()
    contract, _ = merged_contract(report, report.visual("users"), "current", BOTH, CATALOG)
    assert (str(contract.time_range.start), str(contract.time_range.end)) == ("2026-09-01", "2026-09-30")


def test_previous_30_days_is_the_30_days_before_the_last_30():
    defaults = resolve_defaults(_pair(), date(2026, 10, 9), [])
    assert defaults["comparison"] == {"start": "2026-08-11", "end": "2026-09-09"}
    assert defaults["dates"] == {"start": "2026-09-10", "end": "2026-10-09"}


def test_the_default_primary_and_comparison_windows_are_the_same_length():
    d = resolve_defaults(_pair(), date(2026, 10, 9), [])
    length = lambda r: (date.fromisoformat(r["end"]) - date.fromisoformat(r["start"])).days + 1
    assert length(d["dates"]) == length(d["comparison"]) == 30


def test_validation_flags_a_date_filter_that_is_not_a_date_range():
    report = _pair()
    report.pages[0].visuals[0].queries["previous"]["date_filter"] = "nope"
    assert any("nope" in p for p in validate_report(report, CATALOG))


IH1 = {"id": "ih1", "label": "Institutional hierarchy level 1", "control": "select",
       "dimensions": [{"ref": "dataset.course_filters_ih.v1:ih_level_1"}, {"ref": "ih_nodes", "op": "contains"}]}
SESSIONS = {"id": "sessions", "type": "kpi", "title": "Sessions",
            "queries": {"main": {"measures": ["dataset.lms_sessions.v1:sessions"]}}}
STUDENTS = {"id": "students", "type": "kpi", "title": "Students",
            "queries": {"main": {"measures": ["dataset.course_student_activity.v1:students"]}}}


def test_a_filter_applies_the_first_alternative_each_query_can_reach():
    report = _report([IH1], [STUDENTS, SESSIONS])
    course, ignored_c = merged_contract(report, report.visual("students"), "main", {"ih1": ["Nursing"]}, CATALOG)
    platform, ignored_p = merged_contract(report, report.visual("sessions"), "main", {"ih1": ["Nursing"]}, CATALOG)
    assert (ignored_c, ignored_p) == ([], [])
    assert [(f.dimension, f.op, f.values) for f in course.filters] == [("dataset.course_filters_ih.v1:ih_level_1", "in", ["Nursing"])]
    assert [(f.dimension, f.op, f.values) for f in platform.filters] == [("ih_nodes", "contains", ["Nursing"])]


def test_a_contains_alternative_with_several_values_is_ignored_and_reported():
    report = _report([IH1], [SESSIONS])
    _, ignored = merged_contract(report, report.visual("sessions"), "main", {"ih1": ["A", "B"]}, CATALOG)
    assert ignored == ["ih1"]


def test_validation_accepts_alternatives_and_flags_unknown_parents():
    assert validate_report(_report([IH1], [STUDENTS, SESSIONS]), CATALOG) == []
    child = {"id": "ih2", "label": "Level 2", "control": "select", "depends_on": ["nope"],
             "dimensions": [{"ref": "dataset.course_filters_ih.v1:ih_level_2"}]}
    assert any("nope" in p for p in validate_report(_report([IH1, child], [STUDENTS]), CATALOG))


def test_side_by_side_and_per_weekday_average_are_known_transforms():
    for kind in ("side_by_side", "per_weekday_average"):
        _report([], [{**SESSIONS, "transform": {"kind": kind, "query": "main"}}])


def test_lms_sessions_have_an_access_modality():
    from semantic_layer.compiler import compile_query
    from semantic_layer.contract import QueryContract
    sql = compile_query(QueryContract(measures=["dataset.lms_sessions.v1:sessions"], dimensions=["access_modality"]), CATALOG, "DB").sql
    assert "ACCESS_MODALITY" in sql


def _levels(op="path"):
    return [{"id": f"ih{n}", "label": f"Level {n}", "control": "select", "exclude_values": ["-", "All Nodes"],
             "dimensions": [{"ref": f"dataset.course_filters_ih.v1:ih_level_{n}"}, {"ref": "ih_nodes", "op": op}],
             **({"depends_on": [f"ih{p}" for p in range(1, n)]} if n > 1 else {})} for n in (1, 2)]


def test_a_path_filter_matches_the_whole_path_prefix_from_level_one():
    report = _report(_levels(), [SESSIONS])
    contract, ignored = merged_contract(report, report.visual("sessions"), "main", {"ih1": ["Nursing"], "ih2": ["Art"]}, CATALOG)
    assert ignored == []
    assert [(f.dimension, f.op, f.values) for f in contract.filters] == [
        ("ih_nodes", "contains", [";||Nursing||"]), ("ih_nodes", "contains", [";||Nursing||Art||"])]


def test_a_path_filter_compiles_to_a_delimited_containment():
    from semantic_layer.compiler import compile_query
    report = _report(_levels(), [SESSIONS])
    contract, _ = merged_contract(report, report.visual("sessions"), "main", {"ih1": ["Nursing"]}, CATALOG)
    assert "';||nursing||'" in compile_query(contract, CATALOG, "DB").sql.lower()


def test_platform_hierarchy_paths_start_with_a_separator():
    from semantic_layer.compiler import build_ctes
    sql = build_ctes(CATALOG, ["dataset.lms_sessions.v1"], "DB")["DS_LMS_SESSIONS_V1"]
    assert "';' || LISTAGG(DISTINCT ih.HIERARCHY_NAME_SEQ, ';')" in sql


def test_a_date_range_can_apply_as_an_overlap_of_two_dimensions():
    courses = {"id": "courses", "type": "kpi", "title": "Active courses",
               "queries": {"main": {"measures": ["dataset.courses.v1:courses"],
                                    "time_overlap": {"start": "course_start", "end": "course_end"}}}}
    report = _report([{**PRIMARY, "time_dimension": "dataset.courses.v1:course_start"}], [courses])
    contract, ignored = merged_contract(report, report.visual("courses"), "main",
                                        {"dates": {"start": "2026-09-01", "end": "2026-09-30"}}, CATALOG)
    assert ignored == [] and contract.time_range is None
    assert [(f.dimension, f.op, f.values) for f in contract.filters] == [
        ("course_start", "lte", ["2026-09-30"]), ("course_end", "gte", ["2026-09-01"])]
    assert validate_report(report, CATALOG) == []


def test_average_by_and_part_of_whole_are_known_transforms():
    for kind in ("average_by", "part_of_whole"):
        _report([], [{**SESSIONS, "transform": {"kind": kind, "query": "main"}}])


def test_a_query_can_ignore_a_filter_its_visual_otherwise_takes():
    tool = {"id": "tool", "label": "Tool", "control": "select", "dimension": "dataset.course_tool_activity.v1:tool_name"}
    pie = {"id": "pie", "type": "pie", "title": "Using tools",
           "queries": {"all": {"measures": ["dataset.courses.v1:courses"], "filters_ignored": ["tool"]},
                       "using": {"measures": ["dataset.course_tool_activity.v1:courses"]}},
           "transform": {"kind": "part_of_whole", "whole": "all", "part": "using", "field": "courses", "labels": ["a", "b"]}}
    report = _report([tool], [pie])
    whole, _ = merged_contract(report, report.visual("pie"), "all", {"tool": ["Content Folder"]}, CATALOG)
    part, _ = merged_contract(report, report.visual("pie"), "using", {"tool": ["Content Folder"]}, CATALOG)
    assert (whole.filters, [f.dimension for f in part.filters]) == ([], ["dataset.course_tool_activity.v1:tool_name"])
    assert validate_report(report, CATALOG) == []


def test_a_query_can_take_both_an_overlap_and_a_time_range():
    using = {"id": "using", "type": "kpi", "title": "Using",
             "queries": {"main": {"measures": ["dataset.course_tool_use.v1:courses"], "time_dimension": "event_time",
                                  "time_overlap": {"start": "course_start_date", "end": "course_end_date"}}}}
    report = _report([{**PRIMARY, "time_dimension": "dataset.course_tool_use.v1:event_time"}], [using])
    contract, _ = merged_contract(report, report.visual("using"), "main", {"dates": {"start": "2026-09-01", "end": "2026-09-30"}}, CATALOG)
    assert [(f.dimension, f.op) for f in contract.filters] == [("course_start_date", "lte"), ("course_end_date", "gte")]
    assert (contract.time_range.dimension, str(contract.time_range.start)) == ("event_time", "2026-09-01")


def test_tool_activity_counts_days_and_person_days():
    from semantic_layer.compiler import compile_query
    from semantic_layer.contract import QueryContract
    sql = compile_query(QueryContract(measures=["dataset.course_tool_activity.v1:days", "dataset.course_tool_activity.v1:person_days"],
                                      dimensions=["day_of_week"]), CATALOG, "DB").sql
    assert "COUNT(DISTINCT ACTIVITY_DATE)" in sql and "PERSON_ID" in sql


KPI = {"id": "kpi", "label": "Expected grading time (days)", "control": "number", "default": 21}
INSIDE = {"id": "inside", "type": "kpi", "title": "Inside",
          "queries": {"main": {"measures": ["dataset.grade_response_time.v1:graded_attempts"],
                               "param_filters": [{"dimension": "response_days_value", "op": "lte", "param": "kpi"}]}}}


def test_a_parameter_filter_takes_the_chosen_value_or_the_default():
    report = _report([KPI], [INSIDE])
    chosen, _ = merged_contract(report, report.visual("inside"), "main", {"kpi": ["7"]}, CATALOG)
    default, _ = merged_contract(report, report.visual("inside"), "main", {}, CATALOG)
    assert [(f.dimension, f.op, f.values) for f in chosen.filters] == [("response_days_value", "lte", [7])]
    assert default.filters[0].values == [21]
    assert resolve_defaults(report, date(2026, 10, 9), [])["kpi"] == [21]


def test_validation_flags_an_unknown_parameter():
    bad = {**INSIDE, "queries": {"main": {**INSIDE["queries"]["main"],
                                          "param_filters": [{"dimension": "response_days_value", "op": "lte", "param": "nope"}]}}}
    assert any("nope" in p for p in validate_report(_report([KPI], [bad]), CATALOG))


def test_courses_have_primary_node_levels_and_grading_has_a_day_bucket():
    from semantic_layer.compiler import compile_query
    from semantic_layer.contract import QueryContract
    sql = compile_query(QueryContract(measures=["dataset.grade_response_time.v1:graded_attempts"],
                                      dimensions=["dataset.courses.v1:ih_level_1", "dataset.courses.v1:ih_level_2", "response_days_bucket"]),
                        CATALOG, "DB").sql
    assert "PRIMARY_IND" in sql and "RESPONSE_DAYS_BUCKET" in sql


def test_validation_reads_a_join_list_of_queries():
    joined = {**SESSIONS, "transform": {"kind": "join", "queries": ["main", "ghost"], "on": ["x"]}}
    assert any("ghost" in p for p in validate_report(_report([], [joined]), CATALOG))


def test_encode_options_are_not_mistaken_for_columns():
    kpi = {**SESSIONS, "encode": {"value": "sessions", "unit": "ratio"}}
    bar = {**SESSIONS, "id": "bar", "type": "bar", "encode": {"x": "sessions", "y": ["sessions", "missing"], "stacked": True}}
    problems = validate_report(_report([], [kpi, bar]), CATALOG)
    assert problems == ["bar/encode: columns ['missing'] are not returned by its queries"]


def test_inside_the_grading_time_includes_the_expected_day_itself():
    report = load_reports()["report.assessment_grades.v1"]
    ops = {(v.id, q): [f.op for f in merged_contract(report, v, q, {"kpi": ["21"]}, CATALOG)[0].filters
                       if f.dimension.endswith("response_days_value")]
           for v in report.visuals() for q in v.queries if v.queries[q].get("param_filters")}
    assert ops and all(o == ["lte"] for o in ops.values())


def test_response_days_exist_only_for_graded_attempts():
    from semantic_layer.compiler import build_ctes
    cte = build_ctes(CATALOG, ["dataset.grade_response_time.v1"], "DB")["DS_GRADE_RESPONSE_TIME_V1"]
    for column in ("RESPONSE_HOURS", "RESPONSE_DAYS", "RESPONSE_DAYS_CAPPED"):
        assert f"IFF(gr.GRADED_IND = 1, " in cte.split(f"AS {column},")[0].rsplit("\n", 1)[-1]


def test_joined_node_and_term_queries_read_the_same_ordered_window():
    report = load_reports()["report.assessment_grades.v1"]
    v = report.visual("by_node_term")
    orders = [v.queries[q].get("order_by") for q in ("main", "inside")]
    assert orders[0] and orders[0] == orders[1]


@pytest.mark.parametrize("report_id", sorted(load_reports(REPORTS_DIR)))
def test_table_ratio_columns_declare_their_unit(report_id):
    report = load_reports(REPORTS_DIR)[report_id]
    for v in report.visuals():
        if v.type != "table":
            continue
        ratios = set((v.transform or {}).get("ratios", {}))
        for q in v.queries.values():
            for ref in q.get("measures", []):
                ds, name = ref.split(":")
                if CATALOG.datasets[ds].measure(name).unit == "ratio":
                    ratios.add(name)
        assert ratios <= set((v.encode or {}).get("units", {})), f"{v.id}: {ratios}"


CSA_VALUES = {"dates": {"start": "2022-09-01", "end": "2022-09-30"}, "comparison": {"start": "2022-08-01", "end": "2022-08-31"},
              "in_course": ["Yes"], "term": ["Fall 2022"], "ih1": ["Inst"], "min_attendees": ["2"]}


def _collab_contracts():
    report = load_reports()["report.collaboration_session_activity.v1"]
    return {(v.id, q): merged_contract(report, v, q, CSA_VALUES, CATALOG) for v in report.visuals() for q in v.queries}


def test_collaboration_filters_apply_to_every_query_without_being_ignored():
    for key, (contract, ignored) in _collab_contracts().items():
        dims = {f.dimension for f in contract.filters}
        assert ignored == [], key
        assert {"in_course", "dataset.collab_session_courses.v1:term_name"} <= dims, key


def test_collaboration_in_course_reads_each_datasets_own_column():
    from semantic_layer.compiler import compile_query
    report = load_reports()["report.collaboration_session_activity.v1"]
    contract, _ = merged_contract(report, report.visual("chat_kpi"), "current", {"in_course": ["Yes"]}, CATALOG)
    sql = compile_query(contract, CATALOG, "DB").sql
    assert "DS_COLLAB_SESSIONS_V1" not in sql and "IN_COURSE IN ('Yes')" in sql


@pytest.mark.parametrize("ds", ["dataset.collab_events.v1", "dataset.collab_attendance.v1"])
def test_collab_events_and_attendance_keep_only_sessions_that_started_and_are_not_deleted(ds):
    from semantic_layer.compiler import build_ctes
    from semantic_layer.render import cte_name
    cte = build_ctes(CATALOG, [ds], "DB")[cte_name(ds)]
    assert "ROW_DELETED_TIME IS NULL" in cte and "START_TIME IS NOT NULL" in cte


def test_minimum_attendees_narrows_only_the_attendance_statistics():
    narrowed = {key for key, (c, _) in _collab_contracts().items()
                if any(f.dimension.endswith("attendee_count") and f.op == "gte" and f.values == [2] for f in c.filters)}
    assert narrowed and all(v.startswith("attendance_") and v.endswith("_kpi") for v, _ in narrowed)


IH_FILTERS = [{"id": f"ih{n}", "label": f"L{n}", "control": "select", "dimension": f"dataset.courses.v1:ih_level_{n}"}
              for n in range(1, 5)]
BY_NODE = {"id": "by_node", "type": "bar", "title": "By node",
           "queries": {"main": {"measures": ["dataset.course_readiness.v1:pct_ready"],
                                "child_of": {"filters": ["ih1", "ih2", "ih3", "ih4"],
                                             "dimensions": [f"dataset.courses.v1:ih_level_{n}" for n in range(1, 5)],
                                             "as": "node"},
                                "order_by": [{"field": "node", "direction": "asc"}]}},
           "encode": {"x": "node", "y": "pct_ready"}}


@pytest.mark.parametrize("chosen, level", [({}, 1), ({"ih1": ["Arts"]}, 2), ({"ih1": ["Arts"], "ih2": ["Music"]}, 3),
                                           ({f"ih{n}": ["x"] for n in range(1, 5)}, 4), ({"ih2": ["Music"]}, 1)])
def test_child_of_groups_by_the_level_below_the_deepest_chosen_one(chosen, level):
    from semantic_layer.reports import output_renames
    report = _report(IH_FILTERS, [BY_NODE])
    contract, _ = merged_contract(report, report.visual("by_node"), "main", chosen, CATALOG)
    assert contract.dimensions[0] == f"dataset.courses.v1:ih_level_{level}"
    assert [o.field for o in contract.order_by] == [f"ih_level_{level}"]
    assert output_renames(report, report.visual("by_node"), "main", chosen) == {f"ih_level_{level}": "node"}


def test_a_child_of_visual_validates_against_its_alias_and_its_lists_must_match():
    assert validate_report(_report(IH_FILTERS, [BY_NODE]), CATALOG) == []
    bad = {**BY_NODE, "queries": {"main": {**BY_NODE["queries"]["main"],
                                           "child_of": {"filters": ["ih1", "nope"], "dimensions": ["dataset.courses.v1:ih_level_1"], "as": "node"}}}}
    problems = validate_report(_report(IH_FILTERS, [bad]), CATALOG)
    assert any("child_of" in p for p in problems)


CA_VALUES = {"dates": {"start": "2026-01-01", "end": "2026-06-30"}, "term": ["Q4: 2026"], "duration": ["Fixed"],
             "ih1": ["Arts"], "course": ["BIO-101"]}


def test_course_administration_applies_every_filter_to_every_query():
    report = load_reports()["report.course_administration.v1"]
    for v in report.visuals():
        for q in v.queries:
            contract, ignored = merged_contract(report, v, q, CA_VALUES, CATALOG)
            assert ignored == [], (v.id, q)
            assert contract.time_range.dimension == "dataset.courses.v1:course_creation_date", (v.id, q)


def test_course_administration_node_bars_show_the_children_of_the_chosen_node():
    report = load_reports()["report.course_administration.v1"]
    contract, _ = merged_contract(report, report.visual("readiness_by_node"), "main", CA_VALUES, CATALOG)
    assert contract.dimensions[0] == "dataset.courses.v1:ih_level_2"


@pytest.mark.parametrize("today, last, before", [
    (date(2026, 10, 10), ("2026-09-01", "2026-09-30"), ("2026-08-01", "2026-08-31")),
    (date(2026, 1, 15), ("2025-12-01", "2025-12-31"), ("2025-11-01", "2025-11-30")),
    (date(2026, 3, 1), ("2026-02-01", "2026-02-28"), ("2026-01-01", "2026-01-31")),
])
def test_month_defaults_are_the_last_completed_month_and_the_one_before(today, last, before):
    report = _report([{"id": "m", "label": "M", "control": "date_range", "time_dimension": "event_time", "default": "last_month"},
                      {"id": "p", "label": "P", "control": "date_range", "time_dimension": "event_time", "default": "month_before_last"}], [])
    defaults = resolve_defaults(report, today, [])
    assert (defaults["m"]["start"], defaults["m"]["end"]) == last and (defaults["p"]["start"], defaults["p"]["end"]) == before


def test_ai_month_kpis_compare_whole_months_whatever_the_date_range():
    report = load_reports()["report.ai_design_assistant_adoption.v1"]
    values = {**resolve_defaults(report, date(2026, 10, 10), []), "dates": {"start": "2025-01-01", "end": "2025-03-31"}}
    for v in ("kpi_courses", "kpi_users", "kpi_items"):
        cur, _ = merged_contract(report, report.visual(v), "current", values, CATALOG)
        prev, _ = merged_contract(report, report.visual(v), "previous", values, CATALOG)
        assert (str(cur.time_range.start), str(cur.time_range.end)) == ("2026-09-01", "2026-09-30")
        assert (str(prev.time_range.start), str(prev.time_range.end)) == ("2026-08-01", "2026-08-31")


def test_ai_report_applies_its_filters_and_hides_instructors_from_viewers():
    report = load_reports()["report.ai_design_assistant_adoption.v1"]
    values = {"dates": {"start": "2026-01-01", "end": "2026-09-30"}, "term": ["Q4: 2026"], "ih1": ["Arts"],
              "course": ["BIO-101"], "item_type": ["Assignment"], "duration": ["Fixed"]}
    for v in report.visuals():
        for q in v.queries:
            assert merged_contract(report, v, q, values, CATALOG)[1] == [], (v.id, q)
    instructors = report.visual("instructors")
    contract, _ = merged_contract(report, instructors, "main", values, CATALOG)
    ds = CATALOG.datasets["dataset.course_item_ai_usage.v1"]
    assert all(ds.is_pii(ds.dimension(d.rpartition(":")[2]).column) for d in contract.dimensions)


def test_ai_usage_over_time_covers_the_whole_history_within_its_row_limit():
    report = load_reports()["report.ai_design_assistant_adoption.v1"]
    for vid in ("items_over_time", "share_over_time"):
        q = report.visual(vid).queries["main"]
        assert q["dimensions"] == ["created_date__month"] and q["limit"] >= 400


def test_ai_items_with_no_creator_are_labelled():
    from semantic_layer.compiler import build_ctes
    assert "'Unknown creator'" in build_ctes(CATALOG, ["dataset.course_item_ai_usage.v1"], "DB")["DS_COURSE_ITEM_AI_USAGE_V1"]


METRIC = {"id": "metric", "label": "Key metric", "control": "choice", "options": ["User count", "Time spent (minutes)"],
          "default": ["User count"]}
BY_TOOL = {"id": "by_tool", "type": "bar", "title": "Tools",
           "queries": {"main": {"dimensions": ["tool_name"],
                                "measure_from": {"filter": "metric", "as": "value", "measures": {
                                    "User count": "dataset.course_tool_activity.v1:people",
                                    "Time spent (minutes)": "dataset.course_tool_activity.v1:minutes"}},
                                "order_by": [{"field": "value", "direction": "desc"}]}},
           "encode": {"x": "tool_name", "y": "value"}}


@pytest.mark.parametrize("values, measure", [({}, "people"), ({"metric": ["User count"]}, "people"),
                                             ({"metric": ["Time spent (minutes)"]}, "minutes")])
def test_measure_from_uses_the_chosen_metric_and_returns_it_under_its_alias(values, measure):
    from semantic_layer.reports import output_renames
    report = _report([METRIC], [BY_TOOL])
    contract, ignored = merged_contract(report, report.visual("by_tool"), "main", values, CATALOG)
    assert contract.measures == [f"dataset.course_tool_activity.v1:{measure}"] and ignored == []
    assert [o.field for o in contract.order_by] == [measure] and contract.filters == []
    assert output_renames(report, report.visual("by_tool"), "main", values) == {measure: "value"}
    assert resolve_defaults(report, date(2026, 10, 10), [])["metric"] == ["User count"]


def test_an_unknown_metric_is_a_value_error_and_validation_checks_choices():
    report = _report([METRIC], [BY_TOOL])
    with pytest.raises(ReportValueError):
        merged_contract(report, report.visual("by_tool"), "main", {"metric": ["Nope"]}, CATALOG)
    assert validate_report(report, CATALOG) == []
    partial = {**BY_TOOL, "queries": {"main": {**BY_TOOL["queries"]["main"], "measure_from": {
        "filter": "metric", "as": "value", "measures": {"User count": "dataset.course_tool_activity.v1:people"}}}}}
    assert any("measure_from" in p for p in validate_report(_report([METRIC], [partial]), CATALOG))
    with pytest.raises(Exception):
        _report([{**METRIC, "default": ["Missing"]}], [])
