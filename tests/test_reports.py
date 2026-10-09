from datetime import date
from pathlib import Path

import pytest

from semantic_layer.catalog import load_catalog
from semantic_layer.reports import (REPORTS_DIR, ReportError, load_reports, merged_contract, resolve_defaults,
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
    assert defaults["dates"] == {"start": "2026-09-09", "end": "2026-10-09"}
    assert "node" not in defaults


def test_duplicate_report_ids_are_refused(tmp_path):
    for name in ("a.yaml", "b.yaml"):
        (tmp_path / name).write_text((FIXTURES / "sample.yaml").read_text())
    with pytest.raises(ReportError, match="duplicate"):
        load_reports(tmp_path)


@pytest.mark.parametrize("report_id", sorted(load_reports(REPORTS_DIR)))
def test_every_canonical_report_validates(report_id):
    assert validate_report(load_reports(REPORTS_DIR)[report_id], CATALOG) == []
