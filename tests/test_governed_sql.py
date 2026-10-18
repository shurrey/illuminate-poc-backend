import dataclasses

import pytest
import sqlglot
import sqlglot.expressions as exp

import licenses
import semantic_layer.governed_sql as governed_sql
import snowflake_client
from semantic_layer.contract import QueryContract
from semantic_layer.governed_sql import SplicedSql, SpliceError, label, splice
from tests.guard_fakes import guard_ctx, run
from tests.semantic_fixtures import COURSES, ENROLLMENTS, catalog, dataset
from tests.tenant_fakes import TENANT_A, TENANT_CORE, install_tenants

DB = "MY_DB"
STUDENTS = QueryContract(metrics=["metric.student_enrollments.v1"], dimensions=["course_role"])
PEOPLE = QueryContract(measures=["dataset.enrollments.v1:people"], dimensions=["course_role"])
MIXED = QueryContract(metrics=["metric.student_enrollments.v1"], measures=["dataset.courses.v1:courses"],
                      dimensions=["course_role"])
CATALOG = catalog(ENROLLMENTS, COURSES)


def _splice(outer, governed, cat=CATALOG, allow_identity=False):
    return splice(outer, governed, cat, DB, allow_identity=allow_identity)


def _cte_names(sql):
    return [cte.alias for cte in sqlglot.parse_one(sql, dialect="snowflake").args["with"].expressions]


def _cte(sql, name):
    return next(c for c in sqlglot.parse_one(sql, dialect="snowflake").args["with"].expressions if c.alias == name)


def _unqualified_tables(node):
    return {t.name for t in node.find_all(exp.Table) if not t.args.get("db")}


def test_one_part_becomes_a_top_level_cte_after_its_prefixed_helpers():
    spliced = _splice("SELECT course_role, student_enrollments FROM governed_1", {"governed_1": STUDENTS})
    assert isinstance(spliced, SplicedSql)
    assert _cte_names(spliced.sql) == ["governed_1__DS_ENROLLMENTS_V1", "governed_1"]
    assert _unqualified_tables(_cte(spliced.sql, "governed_1")) == {"governed_1__DS_ENROLLMENTS_V1"}
    assert spliced.part_names == frozenset({"governed_1"})
    assert spliced.helper_names == frozenset({"governed_1__DS_ENROLLMENTS_V1"})


def test_part_records_its_contract_compiled_query_and_output_definitions():
    part = _splice("SELECT course_role, courses FROM governed_1", {"governed_1": MIXED}).parts["governed_1"]
    assert part.contract == MIXED
    assert "DS_COURSES_V1" in part.compiled.sql
    assert part.outputs == {
        "COURSE_ROLE": "dimension:course_role",
        "STUDENT_ENROLLMENTS": "metric.student_enrollments.v1",
        "COURSES": "dataset.courses.v1:courses",
    }


def test_two_parts_over_the_same_dataset_do_not_collide():
    spliced = _splice(
        "SELECT a.course_role, a.student_enrollments, b.people FROM governed_1 a JOIN governed_2 b"
        " ON a.course_role = b.course_role",
        {"governed_1": STUDENTS, "governed_2": PEOPLE})
    names = _cte_names(spliced.sql)
    assert names == ["governed_1__DS_ENROLLMENTS_V1", "governed_1", "governed_2__DS_ENROLLMENTS_V1", "governed_2"]
    assert _unqualified_tables(_cte(spliced.sql, "governed_2")) == {"governed_2__DS_ENROLLMENTS_V1"}


def test_multi_dataset_part_renames_its_group_ctes_and_keeps_their_column_qualifiers():
    spliced = _splice("SELECT courses FROM governed_1", {"governed_1": MIXED})
    assert _cte_names(spliced.sql) == ["governed_1__DS_ENROLLMENTS_V1", "governed_1__DS_COURSES_V1",
                                      "governed_1__g1", "governed_1__g2", "governed_1"]
    body = _cte(spliced.sql, "governed_1").this
    assert {(t.name, t.alias) for t in body.find_all(exp.Table)} == {("governed_1__g1", "g1"), ("governed_1__g2", "g2")}
    assert {c.table for c in body.find_all(exp.Column)} == {"g1", "g2"}


def test_spliced_sql_parses_and_every_real_table_is_qualified_with_the_tenant_database():
    spliced = _splice("WITH x AS (SELECT course_role, courses FROM governed_1)"
                      " SELECT x.course_role, COUNT(*) AS n FROM x JOIN CDM_LMS.COURSE c ON c.NAME = x.course_role"
                      " GROUP BY 1", {"governed_1": MIXED})
    tree = sqlglot.parse_one(spliced.sql, dialect="snowflake")
    ctes = {c.alias for c in tree.find_all(exp.CTE)}
    for cte in tree.args["with"].expressions:
        if cte.alias.startswith("governed_1"):
            for table in cte.find_all(exp.Table):
                if table.args.get("db"):
                    assert table.catalog == DB and table.db.startswith("CDM_"), table.sql()
                else:
                    assert table.name in ctes, table.sql()
    assert _cte_names(spliced.sql)[-1] == "x"


def test_a_cte_nested_inside_a_dataset_body_is_not_renamed():
    inner = dataset(id="dataset.inner.v1",
                    base_sql="WITH g1 AS (SELECT ID AS COURSE_ID, NAME AS COURSE_ROLE FROM {{ database }}.CDM_LMS.COURSE)"
                             " SELECT COURSE_ID AS ID, COURSE_ID AS PERSON_ID, COURSE_ID, COURSE_ROLE,"
                             " NULL AS ENROLLMENT_TIME, NULL AS EMAIL FROM g1")
    contract = QueryContract(measures=["dataset.inner.v1:people", "dataset.courses.v1:courses"],
                             dimensions=["course_role"])
    spliced = _splice("SELECT people FROM governed_1", {"governed_1": contract}, cat=catalog(inner, COURSES))
    body = _cte(spliced.sql, "governed_1__DS_INNER_V1").this
    assert _unqualified_tables(body) == {"g1"}
    assert "governed_1__g1" in _cte_names(spliced.sql)


@pytest.mark.parametrize("outer", [
    "WITH governed_1 AS (SELECT 1 AS n) SELECT n FROM governed_1",
    "WITH GOVERNED_1 AS (SELECT 1 AS n) SELECT n FROM governed_1",
    "SELECT n FROM (WITH governed_1__x AS (SELECT 1 AS n) SELECT n FROM governed_1__x) JOIN governed_1 ON TRUE",
])
def test_outer_sql_that_defines_a_governed_cte_is_refused(outer):
    with pytest.raises(SpliceError, match="governed_"):
        _splice(outer, {"governed_1": STUDENTS})


def test_a_declared_part_the_outer_sql_never_uses_is_refused():
    with pytest.raises(SpliceError, match="governed_2"):
        _splice("SELECT course_role FROM governed_1", {"governed_1": STUDENTS, "governed_2": PEOPLE})


def test_a_referenced_part_that_was_not_declared_is_refused():
    with pytest.raises(SpliceError, match="governed_3"):
        _splice("SELECT a.course_role FROM governed_1 a JOIN governed_3 b ON TRUE", {"governed_1": STUDENTS})


def test_more_than_five_parts_is_refused():
    names = [f"governed_{i}" for i in range(1, 7)]
    outer = "SELECT 1 AS n FROM " + ", ".join(names)
    with pytest.raises(SpliceError, match="5"):
        _splice(outer, {n: STUDENTS for n in names})


@pytest.mark.parametrize("name", ["governed_0", "governed_100", "Governed_1", "governed", "other"])
def test_part_names_must_be_governed_and_a_number(name):
    with pytest.raises(SpliceError, match="name"):
        _splice(f"SELECT course_role FROM {name}", {name: STUDENTS})


def test_a_viewers_part_that_selects_an_identity_dimension_gets_the_compile_refusal():
    contract = QueryContract(metrics=["metric.student_enrollments.v1"], dimensions=["email"])
    with pytest.raises(SpliceError, match="governed_1.*personally identifiable and cannot be selected"):
        _splice("SELECT email FROM governed_1", {"governed_1": contract})


def test_an_unmapped_output_name_is_a_splice_error(monkeypatch):
    from semantic_layer import governed_sql

    monkeypatch.setattr(governed_sql, "_output_name", lambda ref: "something_else")
    with pytest.raises(SpliceError, match="course_role"):
        _splice("SELECT course_role FROM governed_1", {"governed_1": STUDENTS})


def test_an_admins_part_may_select_an_identity_dimension():
    contract = QueryContract(metrics=["metric.student_enrollments.v1"], dimensions=["email"])
    spliced = _splice("SELECT email FROM governed_1", {"governed_1": contract}, allow_identity=True)
    assert spliced.parts["governed_1"].outputs["EMAIL"] == "dimension:email"


# The guard over spliced statements.

STAR = dataset(id="dataset.star_enrollments.v1",
               base_sql="SELECT pc.*, p.EMAIL FROM {{ database }}.CDM_LMS.PERSON_COURSE pc"
                        " JOIN {{ database }}.CDM_LMS.PERSON p ON p.ID = pc.PERSON_ID")
STAR_PEOPLE = QueryContract(measures=["dataset.star_enrollments.v1:people"], dimensions=["course_role"])


@pytest.fixture
def executed(monkeypatch):
    calls = []
    monkeypatch.setattr(snowflake_client, "query_sql", lambda sql, params=None, **k: calls.append(sql) or {"columns": [], "rows": []})
    monkeypatch.setattr(snowflake_client, "tenant_database", lambda tenant: DB)
    return calls


def _ctx(tenant=TENANT_A, cat=CATALOG, **kwargs):
    return guard_ctx(tenant, catalog=cat, database=DB, **kwargs)


def _guard(outer, governed, tenant=TENANT_A, cat=CATALOG, **kwargs):
    """The guard in Spliced mode, splicing outer and governed itself."""
    return run(None, _ctx(tenant, cat, **kwargs), snowflake_client.Spliced(outer, governed))


def _guard_as_spliced(monkeypatch, sql, spliced, **kwargs):
    """The guard checking sql against spliced, standing in for the splice it would build."""
    monkeypatch.setattr(snowflake_client, "splice", lambda *a, **k: dataclasses.replace(spliced, sql=sql))
    return run(None, _ctx(**kwargs), snowflake_client.Spliced("SELECT 1", {}))


def test_guard_accepts_governed_bodies_with_stars_and_inner_pii_that_freehand_rules_refuse(executed):
    outer = "SELECT course_role, people FROM governed_1 ORDER BY people DESC LIMIT 10"
    spliced = _splice(outer, {"governed_1": STAR_PEOPLE}, cat=catalog(STAR))
    assert run(spliced.sql, _ctx())["error"]
    assert "error" not in _guard(outer, {"governed_1": STAR_PEOPLE}, cat=catalog(STAR))
    assert executed == [spliced.sql.strip()]


def test_guard_accepts_a_two_part_join_and_an_outer_cte(executed):
    assert "error" not in _guard("WITH x AS (SELECT course_role, student_enrollments FROM governed_1)"
                                 " SELECT x.course_role, x.student_enrollments, b.people"
                                 " FROM x JOIN governed_2 b ON x.course_role = b.course_role",
                                 {"governed_1": STUDENTS, "governed_2": PEOPLE})
    assert len(executed) == 1


def test_guard_refuses_outer_freehand_pii_for_a_viewer_and_allows_it_for_an_admin(executed):
    outer = "SELECT g.course_role, p.EMAIL FROM governed_1 g JOIN CDM_LMS.PERSON p ON p.ID = g.people"
    assert "personally identifiable" in _guard(outer, {"governed_1": PEOPLE})["error"]
    assert "error" not in _guard(outer, {"governed_1": PEOPLE}, allow_identity=True)


def test_guard_refuses_an_outer_star_over_a_governed_part(executed):
    assert "Star projections" in _guard("SELECT * FROM governed_1", {"governed_1": PEOPLE})["error"]
    assert executed == []


def test_guard_refuses_outer_sql_reading_a_parts_helper_cte(executed, monkeypatch):
    spliced = _splice("SELECT g.course_role FROM governed_1 g", {"governed_1": PEOPLE})
    sql = spliced.sql.replace("SELECT\n  g.course_role\nFROM governed_1 AS g",
                              "SELECT\n  d.course_role\nFROM governed_1 AS g JOIN governed_1__DS_ENROLLMENTS_V1 AS d ON TRUE")
    assert sql != spliced.sql
    assert "governed_1" in _guard_as_spliced(monkeypatch, sql, spliced)["error"]
    assert executed == []


def test_the_reviewers_probe_is_refused_for_a_viewer_under_a_genuine_splice(executed, monkeypatch):
    spliced = _splice("SELECT course_role, people FROM governed_1", {"governed_1": PEOPLE})
    probe = ("WITH governed_1 AS (SELECT n FROM (SELECT p.EMAIL AS n FROM CDM_LMS.PERSON p) t)"
             " SELECT n FROM governed_1")
    assert "error" in _guard_as_spliced(monkeypatch, probe, spliced)
    assert executed == []


def test_a_genuine_splice_passes_and_its_helper_tampered_does_not(executed, monkeypatch):
    spliced = _splice("SELECT course_role, people FROM governed_1", {"governed_1": PEOPLE})
    assert "error" not in _guard("SELECT course_role, people FROM governed_1", {"governed_1": PEOPLE})
    tampered = spliced.sql.replace("CDM_LMS.PERSON AS p", "CDM_LMS.PERSON AS p TABLESAMPLE (10)")
    assert tampered != spliced.sql
    assert "error" in _guard_as_spliced(monkeypatch, tampered, spliced)
    assert len(executed) == 1


def test_a_core_tenants_outer_sql_naming_learn_is_refused(monkeypatch, executed):
    install_tenants(monkeypatch, TENANT_CORE, database=DB)
    monkeypatch.setattr(snowflake_client, "tenant_database", lambda tenant: DB)
    outer = "SELECT g.course_role, COUNT(*) AS n FROM governed_1 g JOIN LEARN.USERS u ON TRUE GROUP BY 1"
    assert _guard(outer, {"governed_1": PEOPLE}, tenant=TENANT_CORE) == {"error": licenses.LEARN_REFUSAL}
    assert executed == []


REASON = "Ungoverned: raw SQL"
RAW = "MY_DB.PUBLIC.RAW_T"


def _label(outer, columns, governed=None):
    return label(_splice(outer, governed or {"governed_1": STUDENTS}), columns, reason=REASON)


def test_a_passed_through_column_is_governed_with_its_definition():
    result = _label("SELECT course_role, student_enrollments FROM governed_1", ["COURSE_ROLE", "STUDENT_ENROLLMENTS"])
    assert result == {"governance": "full", "rows": "governed", "columns": {
        "COURSE_ROLE": {"governed": True, "definition": "dimension:course_role"},
        "STUDENT_ENROLLMENTS": {"governed": True, "definition": "metric.student_enrollments.v1"}}}


def test_an_aliased_column_is_governed():
    result = _label("SELECT g.course_role AS role FROM governed_1 g", ["ROLE"])
    assert result["columns"]["ROLE"] == {"governed": True, "definition": "dimension:course_role"}


def test_result_names_match_case_insensitively():
    result = _label("SELECT course_role FROM governed_1", ["course_role"])
    assert result["columns"]["course_role"]["governed"] is True


def test_a_governed_column_passed_through_an_outer_cte_is_governed():
    result = _label("WITH x AS (SELECT course_role AS r FROM governed_1) SELECT r FROM x", ["R"])
    assert result["columns"]["R"] == {"governed": True, "definition": "dimension:course_role"}


def test_an_aggregate_over_a_governed_column_is_ungoverned():
    result = _label("SELECT SUM(student_enrollments) AS total FROM governed_1", ["TOTAL"])
    assert result == {"governance": "none", "rows": "governed", "columns": {"TOTAL": {"governed": False, "reason": REASON}}}


@pytest.mark.parametrize("expression", ["student_enrollments + 1", "CAST(student_enrollments AS VARCHAR)",
                                        "COALESCE(student_enrollments, 0)", "CASE WHEN course_role = 'x' THEN 1 END",
                                        "ROW_NUMBER() OVER (ORDER BY student_enrollments)", "1", "COUNT(*)"])
def test_a_computed_column_is_ungoverned(expression):
    assert _label(f"SELECT {expression} AS v FROM governed_1", ["V"])["columns"]["V"] == {
        "governed": False, "reason": REASON}


def test_a_join_with_a_raw_table_labels_each_side():
    result = _label(f"SELECT g.course_role, r.note FROM governed_1 g JOIN {RAW} r ON r.id = g.course_role",
                    ["COURSE_ROLE", "NOTE"])
    assert result["governance"] == "partial"
    assert result["columns"]["COURSE_ROLE"]["governed"] is True
    assert result["columns"]["NOTE"] == {"governed": False, "reason": REASON}


def test_a_same_named_column_is_labelled_by_the_table_it_is_qualified_with():
    result = _label(f"SELECT g.course_role AS governed_role, r.course_role AS raw_role "
                    f"FROM governed_1 g JOIN {RAW} r ON r.id = g.course_role", ["GOVERNED_ROLE", "RAW_ROLE"])
    assert result["columns"]["GOVERNED_ROLE"]["governed"] is True
    assert result["columns"]["RAW_ROLE"]["governed"] is False


def test_a_column_from_an_outer_cte_over_a_raw_table_is_ungoverned():
    result = _label(f"WITH x AS (SELECT note FROM {RAW}) SELECT x.note, g.course_role FROM x, governed_1 g",
                    ["NOTE", "COURSE_ROLE"])
    assert result["columns"]["NOTE"] == {"governed": False, "reason": REASON}
    assert result["columns"]["COURSE_ROLE"]["governed"] is True


def test_a_column_that_is_both_a_governed_column_and_a_raw_one_through_union_is_ungoverned():
    result = _label(f"SELECT course_role AS c FROM governed_1 UNION ALL SELECT note FROM {RAW}", ["C"])
    assert result["columns"]["C"] == {"governed": False, "reason": REASON}


def test_a_union_of_the_same_governed_column_is_governed():
    result = _label("SELECT course_role AS c FROM governed_1 UNION ALL SELECT course_role FROM governed_1", ["C"])
    assert result["columns"]["C"]["governed"] is True


def test_an_outer_star_over_a_part_expands_to_governed_columns_without_crashing():
    result = _label("SELECT * FROM governed_1", ["COURSE_ROLE", "STUDENT_ENROLLMENTS"])
    assert result["governance"] == "full"


def test_a_result_column_the_query_does_not_produce_is_ungoverned():
    result = _label("SELECT course_role FROM governed_1", ["COURSE_ROLE", "OTHER"])
    assert result["governance"] == "partial"
    assert result["columns"]["OTHER"] == {"governed": False, "reason": REASON}


def test_two_parts_label_with_their_own_definitions():
    result = _label("SELECT a.student_enrollments, b.people FROM governed_1 a JOIN governed_2 b "
                    "ON a.course_role = b.course_role", ["STUDENT_ENROLLMENTS", "PEOPLE"],
                    {"governed_1": STUDENTS, "governed_2": PEOPLE})
    assert result["columns"]["PEOPLE"]["definition"] == "dataset.enrollments.v1:people"
    assert (result["governance"], result["rows"]) == ("partial", "modified")


def test_a_lineage_failure_leaves_every_column_ungoverned_with_the_untraced_reason(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(governed_sql, "lineage", boom)
    result = _label("SELECT course_role, student_enrollments FROM governed_1", ["COURSE_ROLE", "STUDENT_ENROLLMENTS"])
    assert result == {"governance": "none", "rows": "modified", "columns": {
        c: {"governed": False, "reason": "Column origin could not be traced"}
        for c in ("COURSE_ROLE", "STUDENT_ENROLLMENTS")}}


@pytest.mark.parametrize("results", [["a", "A"], ["A", "a"]])
def test_selects_whose_names_differ_only_by_case_are_all_ungoverned(results):
    result = _label('SELECT COUNT(*) AS "a", course_role AS "A" FROM governed_1 GROUP BY course_role', results)
    assert result["governance"] == "none"
    assert not any(c["governed"] for c in result["columns"].values())


def test_an_exact_case_quoted_alias_is_matched():
    result = _label('SELECT course_role AS "Role" FROM governed_1', ["Role"])
    assert result["columns"]["Role"]["governed"] is True


def test_a_join_to_a_raw_table_with_only_governed_columns_selected_is_partial_with_rows_modified():
    result = _label(f"SELECT g.course_role FROM governed_1 g JOIN {RAW} r ON TRUE", ["COURSE_ROLE"])
    assert result["columns"]["COURSE_ROLE"]["governed"] is True
    assert (result["governance"], result["rows"]) == ("partial", "modified")


def test_a_where_in_subquery_over_a_raw_table_is_partial_with_rows_modified():
    result = _label(f"SELECT course_role FROM governed_1 WHERE course_role IN (SELECT note FROM {RAW})",
                    ["COURSE_ROLE"])
    assert (result["governance"], result["rows"]) == ("partial", "modified")


def test_a_join_to_a_non_governed_cte_is_rows_modified():
    result = _label(f"WITH x AS (SELECT note FROM {RAW}) SELECT g.course_role FROM governed_1 g JOIN x ON TRUE",
                    ["COURSE_ROLE"])
    assert (result["governance"], result["rows"]) == ("partial", "modified")


def test_filters_distinct_and_limit_over_governed_parts_keep_full():
    result = _label("SELECT DISTINCT g.course_role FROM governed_1 g WHERE g.course_role = 'v' LIMIT 5",
                    ["COURSE_ROLE"])
    assert (result["governance"], result["rows"]) == ("full", "governed")


def test_a_scalar_subquery_returning_a_governed_column_is_ungoverned():
    result = _label("SELECT (SELECT MAX(course_role) FROM governed_1) AS m FROM governed_1", ["M"])
    assert result["columns"]["M"]["governed"] is False


def test_a_window_over_a_governed_column_is_ungoverned():
    result = _label("SELECT MAX(student_enrollments) OVER (PARTITION BY course_role) AS m FROM governed_1", ["M"])
    assert result["columns"]["M"]["governed"] is False


def test_group_by_a_governed_dimension_with_count_elsewhere_is_partial():
    result = _label("SELECT g.course_role, COUNT(*) AS n FROM governed_1 g GROUP BY g.course_role",
                    ["COURSE_ROLE", "N"])
    assert result["governance"] == "partial"
    assert result["columns"]["COURSE_ROLE"]["governed"] is True


# Part limits: a part compiles without a LIMIT unless its contract sets one.

def _part_limits(sql, name="governed_1"):
    return list(_cte(sql, name).find_all(exp.Limit))


def test_a_part_without_an_explicit_limit_compiles_without_one():
    spliced = _splice("SELECT course_role, student_enrollments FROM governed_1", {"governed_1": STUDENTS})
    assert _part_limits(spliced.sql) == []
    assert label(spliced, ["COURSE_ROLE", "STUDENT_ENROLLMENTS"], reason=REASON)["rows"] == "governed"


def test_a_part_with_an_explicit_limit_and_no_order_is_refused():
    contract = QueryContract(metrics=["metric.student_enrollments.v1"], dimensions=["course_role"], limit=5)
    with pytest.raises(SpliceError, match="A governed part with a limit needs an order"):
        _splice("SELECT course_role FROM governed_1", {"governed_1": contract})


def test_an_explicit_default_sized_limit_still_counts_as_set():
    contract = QueryContract(metrics=["metric.student_enrollments.v1"], dimensions=["course_role"], limit=100)
    with pytest.raises(SpliceError, match="needs an order"):
        _splice("SELECT course_role FROM governed_1", {"governed_1": contract})


def test_a_part_with_a_limit_and_an_order_keeps_its_limit_and_modifies_the_rows():
    contract = QueryContract(metrics=["metric.student_enrollments.v1"], dimensions=["course_role"], limit=5,
                             order_by=[{"field": "student_enrollments"}])
    spliced = _splice("SELECT course_role, student_enrollments FROM governed_1", {"governed_1": contract})
    assert [lim.expression.name for lim in _part_limits(spliced.sql)] == ["5"]
    result = label(spliced, ["COURSE_ROLE", "STUDENT_ENROLLMENTS"], reason=REASON)
    assert (result["governance"], result["rows"]) == ("partial", "modified")


# Rows stay governed only for a single FROM <one part> with filters, grouping on its columns, DISTINCT,
# ordering and limits.

TWO = {"governed_1": STUDENTS, "governed_2": STUDENTS}


@pytest.mark.parametrize("outer, governed", [
    ("SELECT a.course_role FROM governed_1 a CROSS JOIN governed_2 b", TWO),
    ("SELECT a.course_role FROM governed_1 a, governed_2 b", TWO),
    ("SELECT a.course_role FROM governed_1 a JOIN governed_2 b ON TRUE", TWO),
    ("SELECT a.course_role FROM governed_1 a JOIN governed_2 b ON a.course_role = b.course_role", TWO),
    ("SELECT a.course_role FROM governed_1 a FULL OUTER JOIN governed_2 b ON a.course_role <> b.course_role", TWO),
    ("SELECT a.course_role FROM governed_1 a JOIN governed_1 b ON TRUE", None),
    ("SELECT course_role FROM governed_1 UNION ALL SELECT course_role FROM governed_1", None),
    ("SELECT course_role FROM governed_1 UNION SELECT course_role FROM governed_1", None),
    ("SELECT course_role FROM governed_1 EXCEPT SELECT course_role FROM governed_1", None),
    ("SELECT g.course_role FROM governed_1 g JOIN (VALUES (1)) AS v ON TRUE", None),
    ("SELECT g.course_role FROM governed_1 g JOIN (SELECT 1 AS x) v ON TRUE", None),
    ("SELECT course_role FROM governed_1 MATCH_RECOGNIZE (ORDER BY course_role MEASURES 1 AS m"
     " ALL ROWS PER MATCH PATTERN (a) DEFINE a AS TRUE)", None),
    ("SELECT course_role FROM governed_1 PIVOT (SUM(student_enrollments) FOR course_role IN ('a'))", None),
    ("SELECT course_role FROM governed_1 UNPIVOT (v FOR k IN (student_enrollments))", None),
    ("SELECT g.course_role FROM governed_1 g, LATERAL (SELECT 1 AS x) l", None),
    ("SELECT course_role FROM governed_1 WHERE course_role IN (SELECT course_role FROM governed_1)", None),
    ("SELECT course_role, (SELECT COUNT(*) FROM governed_1) AS n FROM governed_1", None),
    ("SELECT course_role FROM governed_1 GROUP BY course_role HAVING COUNT(*) > 1", None),
    ("SELECT UPPER(course_role) AS course_role FROM governed_1 GROUP BY UPPER(course_role)", None),
    ("SELECT course_role FROM governed_1 GROUP BY ROLLUP (course_role)", None),
    ("SELECT course_role FROM governed_1 GROUP BY GROUPING SETS ((course_role), ())", None),
    ("WITH x AS (SELECT course_role FROM governed_1) SELECT course_role FROM x", None),
    ("SELECT course_role FROM (SELECT course_role FROM governed_1)", None),
    ("SELECT course_role FROM governed_1 SAMPLE (10)", None),
])
def test_a_join_set_operation_or_other_source_modifies_the_rows(outer, governed):
    result = _label(outer, ["COURSE_ROLE"], governed)
    assert result["rows"] == "modified"
    assert result["governance"] != "full"


@pytest.mark.parametrize("outer", [
    "SELECT course_role FROM governed_1",
    "SELECT g.course_role FROM governed_1 AS g",
    "SELECT DISTINCT g.course_role FROM governed_1 g WHERE g.course_role = 'v' ORDER BY g.course_role LIMIT 5",
    "SELECT course_role FROM governed_1 QUALIFY ROW_NUMBER() OVER (ORDER BY student_enrollments DESC) = 1",
    "SELECT g.course_role FROM governed_1 g GROUP BY g.course_role",
])
def test_one_part_filtered_grouped_or_limited_keeps_governed_rows(outer):
    result = _label(outer, ["COURSE_ROLE"])
    assert (result["governance"], result["rows"]) == ("full", "governed")


def _real_catalog():
    from semantic_layer.catalog import default_catalog
    return default_catalog()


_REAL = _real_catalog()


def _real_part(ds, **contract) -> QueryContract:
    window = {"time_range": {"dimension": ds.required_time_range, "start": "2026-01-01"}} if ds.required_time_range else {}
    return QueryContract(**contract, **window)


_REAL_PARTS = ([(m, _real_part(_REAL.datasets[_REAL.metrics[m].measure.partition(":")[0]], metrics=[m]),
                 _REAL.metrics[m].short_name) for m in sorted(_REAL.metrics)]
               + [(f"{ds.id}:{ds.measures[0].name}", _real_part(ds, measures=[f"{ds.id}:{ds.measures[0].name}"]),
                   ds.measures[0].name) for ds in _REAL.datasets.values() if ds.measures])


@pytest.mark.parametrize("ref, contract, column", _REAL_PARTS, ids=[ref for ref, _, _ in _REAL_PARTS])
def test_every_real_metric_and_measure_splices_as_a_part_that_passes_a_viewers_guard(ref, contract, column, executed):
    spliced = splice(f"SELECT {column} FROM governed_1", {"governed_1": contract}, _REAL, DB, allow_identity=False)
    sqlglot.parse_one(spliced.sql, dialect="snowflake")
    assert _guard(f"SELECT {column} FROM governed_1", {"governed_1": contract}, cat=_REAL) == \
        {"columns": [], "rows": [], "truncated": False}
    assert executed == [spliced.sql.strip()]
