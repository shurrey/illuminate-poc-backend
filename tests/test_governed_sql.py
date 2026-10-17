import pytest
import sqlglot
import sqlglot.expressions as exp

import licenses
import snowflake_client
from semantic_layer.contract import QueryContract
from semantic_layer.governed_sql import Spliced, SpliceError, splice
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
    assert isinstance(spliced, Spliced)
    assert _cte_names(spliced.sql) == ["governed_1__DS_ENROLLMENTS_V1", "governed_1"]
    assert _unqualified_tables(_cte(spliced.sql, "governed_1")) == {"governed_1__DS_ENROLLMENTS_V1"}
    assert spliced.governed_ctes == frozenset({"governed_1__DS_ENROLLMENTS_V1", "governed_1"})


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


def _guard(spliced, tenant=TENANT_A, **kwargs):
    return snowflake_client.validate_and_execute(spliced.sql, tenant=tenant, governed_ctes=spliced.governed_ctes,
                                                 **kwargs)


def test_guard_accepts_governed_bodies_with_stars_and_inner_pii_that_freehand_rules_refuse(executed):
    spliced = _splice("SELECT course_role, people FROM governed_1 ORDER BY people DESC LIMIT 10",
                      {"governed_1": STAR_PEOPLE}, cat=catalog(STAR))
    assert snowflake_client.validate_and_execute(spliced.sql, tenant=TENANT_A)["error"]
    assert "error" not in _guard(spliced)
    assert executed == [spliced.sql.strip()]


def test_guard_accepts_a_two_part_join_and_an_outer_cte(executed):
    spliced = _splice("WITH x AS (SELECT course_role, student_enrollments FROM governed_1)"
                      " SELECT x.course_role, x.student_enrollments, b.people"
                      " FROM x JOIN governed_2 b ON x.course_role = b.course_role",
                      {"governed_1": STUDENTS, "governed_2": PEOPLE})
    assert "error" not in _guard(spliced)
    assert len(executed) == 1


def test_guard_refuses_outer_freehand_pii_for_a_viewer_and_allows_it_for_an_admin(executed):
    spliced = _splice("SELECT g.course_role, p.EMAIL FROM governed_1 g JOIN CDM_LMS.PERSON p ON p.ID = g.people",
                      {"governed_1": PEOPLE})
    assert "personally identifiable" in _guard(spliced)["error"]
    assert "error" not in _guard(spliced, allow_identity=True)


def test_guard_refuses_an_outer_star_over_a_governed_part(executed):
    spliced = _splice("SELECT * FROM governed_1", {"governed_1": PEOPLE})
    assert "Star projections" in _guard(spliced)["error"]
    assert executed == []


def test_guard_refuses_outer_sql_reading_a_parts_helper_cte(executed):
    spliced = _splice("SELECT g.course_role FROM governed_1 g", {"governed_1": PEOPLE})
    sql = spliced.sql.replace("SELECT\n  g.course_role\nFROM governed_1 AS g",
                              "SELECT\n  d.course_role\nFROM governed_1 AS g JOIN governed_1__DS_ENROLLMENTS_V1 AS d ON TRUE")
    assert sql != spliced.sql
    result = snowflake_client.validate_and_execute(sql, tenant=TENANT_A, governed_ctes=spliced.governed_ctes)
    assert "governed_1" in result["error"]
    assert executed == []


def test_a_core_tenants_outer_sql_naming_learn_is_refused(monkeypatch, executed):
    install_tenants(monkeypatch, TENANT_CORE, database=DB)
    monkeypatch.setattr(snowflake_client, "tenant_database", lambda tenant: DB)
    spliced = _splice("SELECT g.course_role, COUNT(*) AS n FROM governed_1 g JOIN LEARN.USERS u ON TRUE GROUP BY 1",
                      {"governed_1": PEOPLE})
    assert _guard(spliced, tenant=TENANT_CORE) == {"error": licenses.LEARN_REFUSAL}
    assert executed == []
