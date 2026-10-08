# Semantic Layer Phase 4 (Joins, Core Datasets, Metrics) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let queries span datasets safely, port the seven core bbd-analytics products the dashboard and chat need first, and re-express the governed metrics over them.

**Architecture:** Cross-dataset dimensions join many-to-one through declared entities. Measures from several datasets aggregate separately and combine on the shared dimensions. Each dataset is YAML that ports one bbd-analytics object and cites its source file. The parametrised definitions tests cover every new dataset and metric automatically.

**Tech Stack:** Python 3.11, sqlglot, Pydantic, Snowflake (illuminate-mcp for live runs).

**Spec:** `docs/superpowers/specs/2026-10-08-semantic-layer-bbd-parity-design.md` §3, §4, §5.2. Roadmap: Phase 4.

## Global Constraints

- bbd-analytics is read-only. Defects found while porting are corrected in the dataset, described in one line there, and added to spec §4.1 in the phase's docs PR.
- Ported SQL drops `tenant_id` and `inferred_ind` (absent in the POC CDM).
- Every dataset PR records a live illuminate-mcp run covering every non-PII dimension and every measure. illuminate-mcp limits PERSON_COURSE to about 120 days of enrollments by injecting a filter into the first WHERE clause that uses `pc`, so a dataset's first CTE should be one that reads PERSON_COURSE as `pc`.
- Dataset and metric units are definitions, not code. Their test is the parametrised definitions suite plus the live run; there is no separate red step.
- One unit = one PR from the latest `origin/main`, squash-merged by the executor.

## Review Focus

1. A dimension requested through a join toward a finer grain fails with an explanation rather than multiplying rows (4a `test_joining_toward_a_finer_grain_is_rejected`).
2. Two metrics from different datasets with no dimensions return one row, not a cartesian product of detail rows (4a `test_measures_from_two_datasets_without_dimensions_cross_join`; live run in 4i).
3. Counting people is not PII, but returning a person's value is (4f `test_counts_of_pii_columns_are_not_pii`, `test_value_returning_aggregates_of_pii_stay_pii`).
4. Ungraded students do not drag average grades to zero or count as failing (4h measure definitions; live run).
5. Week arithmetic on dates from `course_filters` works in Snowflake (4e live run).

---

### Task 1 (PR 4a): cross-dataset queries through declared entities

**Files:** `semantic_layer/compiler.py`, `semantic_layer/contract.py`, `tests/semantic_fixtures.py`, `tests/test_semantic_compiler.py`, `tests/test_semantic_joins.py`, `tests/test_semantic_schema.py`

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b feat/semantic-joins origin/main
```

- [ ] **Step 2: Write the tests**

```diff
diff --git a/tests/semantic_fixtures.py b/tests/semantic_fixtures.py
index c01fff8..6894e7b 100644
--- a/tests/semantic_fixtures.py
+++ b/tests/semantic_fixtures.py
@@ -11,7 +11,10 @@ ENROLLMENTS = Dataset(
     source="test",
     base_sql="SELECT pc.ID, pc.PERSON_ID, pc.COURSE_ID, pc.COURSE_ROLE, pc.ENROLLMENT_TIME, p.EMAIL "
              "FROM {{ database }}.CDM_LMS.PERSON_COURSE pc JOIN {{ database }}.CDM_LMS.PERSON p ON p.ID = pc.PERSON_ID",
-    entities=[{"name": "person_course", "column": "ID", "type": "primary"}],
+    entities=[
+        {"name": "person_course", "column": "ID", "type": "primary"},
+        {"name": "course", "column": "COURSE_ID", "type": "foreign"},
+    ],
     dimensions=[
         {"name": "course_role", "column": "COURSE_ROLE", "type": "categorical"},
         {"name": "enrolled_at", "column": "ENROLLMENT_TIME", "type": "time", "grains": ["day", "month"]},
@@ -26,6 +29,24 @@ ENROLLMENTS = Dataset(
     pii_columns=["EMAIL", "ID", "PERSON_ID"],
 )
 
+COURSES = Dataset(
+    id="dataset.courses.v1",
+    display_name="Courses",
+    description="test",
+    grain="one row per course",
+    domain="test",
+    source="test",
+    base_sql="SELECT c.ID AS COURSE_ID, c.NAME AS COURSE_NAME, c.START_DATE "
+             "FROM {{ database }}.CDM_LMS.COURSE c",
+    entities=[{"name": "course", "column": "COURSE_ID", "type": "primary"}],
+    dimensions=[
+        {"name": "course_name", "column": "COURSE_NAME", "type": "categorical"},
+        {"name": "course_start", "column": "START_DATE", "type": "time", "grains": ["month"]},
+        {"name": "course_role", "column": "COURSE_NAME", "type": "categorical"},
+    ],
+    measures=[{"name": "courses", "agg": "count_distinct", "expr": "COURSE_ID"}],
+)
+
 STUDENT_ENROLLMENTS = SemanticMetric(
     id="metric.student_enrollments.v1",
     display_name="Student enrollments",
diff --git a/tests/test_semantic_compiler.py b/tests/test_semantic_compiler.py
index 26f2e3e..5aa6237 100644
--- a/tests/test_semantic_compiler.py
+++ b/tests/test_semantic_compiler.py
@@ -101,7 +101,7 @@ def test_pii_dimension_can_be_filtered():
     ({"measures": ["dataset.enrollments.v1:nope"]}, "unknown measure"),
     ({"measures": ["dataset.enrollments.v1:enrollments"], "dimensions": ["nope"]}, "unknown dimension"),
     ({"measures": ["dataset.enrollments.v1:enrollments"], "dimensions": ["enrolled_at__year"]}, "grain"),
-    ({"measures": ["dataset.enrollments.v1:enrollments"], "filters": [{"dimension": "nope", "op": "is_null"}]}, "unknown filter dimension"),
+    ({"measures": ["dataset.enrollments.v1:enrollments"], "filters": [{"dimension": "nope", "op": "is_null"}]}, "unknown dimension 'nope'"),
     ({"metrics": ["metric.student_enrollments.v1"], "measures": ["dataset.enrollments.v1:enrollments"],
       "dimensions": []}, None),
 ])
@@ -113,13 +113,6 @@ def test_contract_errors(contract, match):
         _compile(**contract)
 
 
-def test_measures_from_two_datasets_are_rejected_until_joins_exist():
-    other = dataset(id="dataset.other.v1")
-    with pytest.raises(CompileError, match="more than one dataset"):
-        _compile(catalog(ENROLLMENTS, other),
-                 measures=["dataset.enrollments.v1:enrollments", "dataset.other.v1:enrollments"])
-
-
 def test_internal_datasets_cannot_be_queried():
     with pytest.raises(CompileError, match="internal"):
         _compile(catalog(dataset(visibility="internal")), measures=["dataset.enrollments.v1:enrollments"])
@@ -217,7 +210,7 @@ def test_scoped_count_star_counts_matching_rows():
 
 
 def test_grain_suffix_is_not_a_filter_dimension():
-    with pytest.raises(CompileError, match="unknown filter dimension 'enrolled_at__month'"):
+    with pytest.raises(CompileError, match="'enrolled_at__month' has a grain suffix; filters take the plain dimension name"):
         _compile(measures=["dataset.enrollments.v1:enrollments"],
                  filters=[{"dimension": "enrolled_at__month", "op": "not_null"}])
 
diff --git a/tests/test_semantic_joins.py b/tests/test_semantic_joins.py
new file mode 100644
index 0000000..ea6fd98
--- /dev/null
+++ b/tests/test_semantic_joins.py
@@ -0,0 +1,96 @@
+import pytest
+import sqlglot
+
+from semantic_layer.compiler import CompileError, compile_query
+from semantic_layer.contract import QueryContract
+from tests.semantic_fixtures import COURSES, ENROLLMENTS, catalog, dataset
+
+CAT = catalog(ENROLLMENTS, COURSES)
+
+
+def _compile(cat=CAT, **contract):
+    q = compile_query(QueryContract(**contract), cat, "DB")
+    sqlglot.parse_one(q.sql, read="snowflake")
+    return q
+
+
+def test_dimension_from_a_many_to_one_dataset_is_joined():
+    q = _compile(measures=["dataset.enrollments.v1:enrollments"], dimensions=["course_name"])
+    assert "LEFT JOIN DS_COURSES_V1 AS j1\n  ON b.COURSE_ID = j1.COURSE_ID" in q.sql
+    assert "j1.COURSE_NAME AS course_name" in q.sql
+    assert "COUNT(b.ID) AS enrollments" in q.sql
+    assert q.provenance.datasets == ["dataset.enrollments.v1", "dataset.courses.v1"]
+
+
+def test_joining_toward_a_finer_grain_is_rejected():
+    with pytest.raises(CompileError, match="multiply"):
+        _compile(measures=["dataset.courses.v1:courses"], dimensions=["dataset.enrollments.v1:course_role"])
+
+
+def test_own_dimension_wins_over_a_joined_one_with_the_same_name():
+    q = _compile(measures=["dataset.enrollments.v1:enrollments"], dimensions=["course_role"])
+    assert "DS_COURSES_V1" not in q.sql
+    assert "COURSE_ROLE AS course_role" in q.sql
+
+
+def test_qualified_dimension_selects_the_joined_dataset():
+    q = _compile(measures=["dataset.enrollments.v1:enrollments"], dimensions=["dataset.courses.v1:course_role"])
+    assert "j1.COURSE_NAME AS course_role" in q.sql
+
+
+def test_unknown_or_unreachable_qualified_dimension_is_an_error():
+    with pytest.raises(CompileError, match="no dimension"):
+        _compile(measures=["dataset.enrollments.v1:enrollments"], dimensions=["dataset.courses.v1:nope"])
+    other = dataset(id="dataset.other.v1", entities=[])
+    with pytest.raises(CompileError, match="cannot be reached"):
+        _compile(catalog(ENROLLMENTS, COURSES, other), measures=["dataset.enrollments.v1:enrollments"],
+                 dimensions=["dataset.other.v1:course_role"])
+
+
+def test_ambiguous_dimension_across_joined_datasets_is_an_error():
+    twin = dataset(id="dataset.twin.v1", base_sql=COURSES.base_sql, entities=COURSES.model_dump()["entities"],
+                   dimensions=COURSES.model_dump()["dimensions"], measures=COURSES.model_dump()["measures"],
+                   pii_columns=[])
+    with pytest.raises(CompileError, match="ambiguous"):
+        _compile(catalog(ENROLLMENTS, COURSES, twin), measures=["dataset.enrollments.v1:enrollments"],
+                 dimensions=["course_name"])
+
+
+def test_filters_and_time_ranges_resolve_through_joins():
+    q = _compile(measures=["dataset.enrollments.v1:enrollments"],
+                 filters=[{"dimension": "course_name", "op": "eq", "values": ["Biology"]}],
+                 time_range={"dimension": "course_start", "start": "2026-01-01"})
+    assert "j1.COURSE_NAME = 'Biology'" in q.sql
+    assert "CAST(j1.START_DATE AS DATE) >= '2026-01-01'" in q.sql
+
+
+def test_metric_filters_are_qualified_to_the_measure_dataset_in_a_join():
+    q = _compile(metrics=["metric.student_enrollments.v1"], dimensions=["course_name"])
+    assert "COUNT(CASE WHEN b.COURSE_ROLE = 'S' THEN b.ID END) AS student_enrollments" in q.sql
+
+
+def test_measures_from_two_datasets_aggregate_separately_then_join_on_dimensions():
+    q = _compile(measures=["dataset.enrollments.v1:enrollments", "dataset.courses.v1:courses"],
+                 dimensions=["course_name"], order_by=[{"field": "courses"}], limit=10)
+    outer = q.sql[q.sql.rindex("\nSELECT"):]
+    assert "FULL OUTER JOIN g2" in outer
+    assert "g1.course_name IS NOT DISTINCT FROM g2.course_name" in outer
+    assert "COALESCE(g1.course_name, g2.course_name) AS course_name" in outer
+    assert "g1.enrollments" in outer and "g2.courses" in outer
+    assert outer.rstrip().endswith("LIMIT 10")
+
+
+def test_measures_from_two_datasets_without_dimensions_cross_join():
+    q = _compile(measures=["dataset.enrollments.v1:enrollments", "dataset.courses.v1:courses"])
+    assert "CROSS JOIN g2" in q.sql
+
+
+def test_every_dimension_must_be_reachable_from_every_measure_dataset():
+    with pytest.raises(CompileError, match="enrolled_at"):
+        _compile(measures=["dataset.enrollments.v1:enrollments", "dataset.courses.v1:courses"],
+                 dimensions=["enrolled_at__day"])
+
+
+def test_empty_grain_suffix_is_rejected():
+    with pytest.raises(CompileError, match="grain"):
+        _compile(measures=["dataset.enrollments.v1:enrollments"], dimensions=["course_role__"])
diff --git a/tests/test_semantic_schema.py b/tests/test_semantic_schema.py
index d3403f1..c859c66 100644
--- a/tests/test_semantic_schema.py
+++ b/tests/test_semantic_schema.py
@@ -71,3 +71,9 @@ def test_contract_limit_bounds(limit):
 def test_time_range_rejects_inverted_bounds():
     with pytest.raises(ValidationError, match="after end"):
         TimeRange(dimension="d", start="2026-02-01", end="2026-01-01")
+
+
+@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
+def test_filter_values_must_be_finite(value):
+    with pytest.raises(ValidationError):
+        ContractFilter(dimension="d", op="eq", values=[value])
```

- [ ] **Step 3: Run them to verify they fail**

Run: `$PY -m pytest -q tests/test_semantic_joins.py`
Expected: join tests fail (no join support).

- [ ] **Step 4: Implement**

```diff
diff --git a/semantic_layer/compiler.py b/semantic_layer/compiler.py
index bf67ac8..49113dd 100644
--- a/semantic_layer/compiler.py
+++ b/semantic_layer/compiler.py
@@ -110,14 +110,68 @@ def _aggregate(ds: Dataset, m: Measure, scope: Optional[exp.Expression]) -> exp.
     return exp.Anonymous(this=m.agg.upper(), expressions=[arg])
 
 
-def _resolve_dimension(ds: Dataset, ref: str) -> tuple[DatasetDimension, Optional[str]]:
-    name, _, grain = ref.partition("__")
-    dim = ds.dimension(name)
+def _split_ref(ref: str) -> tuple[Optional[str], str, Optional[str]]:
+    """'[dataset.x.v1:]name[__grain]' -> (dataset id or None, name, grain or None)."""
+    qualifier, sep, rest = ref.rpartition(":")
+    name, suffix, grain = rest.partition("__")
+    if suffix and not grain:
+        raise CompileError(f"dimension {ref!r} has an empty grain after '__'")
+    return (qualifier if sep else None), name, (grain or None)
+
+
+def _output_name(ref: str) -> str:
+    """The result column for a dimension reference: its name and grain, without a dataset qualifier."""
+    return ref.rpartition(":")[2]
+
+
+def _joins_from(base: Dataset, catalog: Catalog) -> dict[str, tuple[Dataset, str, str]]:
+    """Datasets reachable many-to-one from base: id -> (dataset, base column, its primary column)."""
+    out: dict[str, tuple[Dataset, str, str]] = {}
+    for other in catalog.datasets.values():
+        if other.id == base.id:
+            continue
+        for mine in base.entities:
+            theirs = next((e for e in other.entities if e.name == mine.name and e.type == "primary"), None)
+            if theirs is not None and other.id not in out:
+                out[other.id] = (other, mine.column, theirs.column)
+    return out
+
+
+def _resolve(base: Dataset, ref: str, catalog: Catalog, joins: dict) -> tuple[Dataset, DatasetDimension, Optional[str]]:
+    ds_id, name, grain = _split_ref(ref)
+    if ds_id is not None and ds_id != base.id:
+        target = catalog.datasets.get(ds_id)
+        if target is None:
+            raise CompileError(f"unknown dataset {ds_id} in dimension {ref!r}")
+        if ds_id not in joins:
+            reverse = _joins_from(target, catalog)
+            if base.id in reverse:
+                raise CompileError(f"joining {ds_id} to {base.id} would multiply {base.id} rows")
+            raise CompileError(f"{ds_id} cannot be reached from {base.id} through a shared entity")
+    elif ds_id is not None or base.dimension(name) is not None:
+        target = base
+    else:
+        hits = [d for d, _, _ in joins.values() if d.dimension(name) is not None]
+        if not hits:
+            raise CompileError(f"unknown dimension {name!r} on {base.id} or the datasets it joins to")
+        if len(hits) > 1:
+            ids = ", ".join(sorted(h.id for h in hits))
+            raise CompileError(f"dimension {name!r} is ambiguous ({ids}); qualify it as '<dataset id>:{name}'")
+        target = hits[0]
+    dim = target.dimension(name)
     if dim is None:
-        raise CompileError(f"unknown dimension {name!r} on {ds.id}")
+        raise CompileError(f"{target.id} has no dimension {name!r}")
     if grain and grain not in dim.grains:
         raise CompileError(f"dimension {name!r} does not support grain {grain!r}; allowed: {dim.grains}")
-    return dim, grain or None
+    return target, dim, grain
+
+
+def _qualified(node: exp.Expression, table: Optional[str]) -> exp.Expression:
+    if table:
+        for col in node.find_all(exp.Column):
+            if not col.table:
+                col.set("table", exp.to_identifier(table))
+    return node
 
 
 def _unquoted_upper(ident: Optional[exp.Identifier]) -> str:
@@ -142,8 +196,8 @@ def _check_tables(sql: str, database: str) -> None:
             )
 
 
-def compile_query(contract: QueryContract, catalog: Catalog, database: str) -> CompiledQuery:
-    selections: list[tuple[str, Dataset, Measure, Optional[SemanticMetric]]] = []
+def _selections(contract: QueryContract, catalog: Catalog) -> list[tuple[str, Dataset, Measure, Optional[SemanticMetric]]]:
+    out = []
     for metric_id in contract.metrics:
         metric = catalog.metrics.get(metric_id)
         if metric is None:
@@ -152,72 +206,138 @@ def compile_query(contract: QueryContract, catalog: Catalog, database: str) -> C
         measure = ds.measure(metric.measure_name) if ds else None
         if measure is None:
             raise CompileError(f"metric {metric_id} points at missing measure {metric.measure}")
-        selections.append((metric.short_name, ds, measure, metric))
+        out.append((metric.short_name, ds, measure, metric))
     for ref in contract.measures:
         ds_id, _, name = ref.partition(":")
         ds = catalog.datasets.get(ds_id)
         measure = ds.measure(name) if ds else None
         if measure is None:
             raise CompileError(f"unknown measure {ref}")
-        selections.append((name, ds, measure, None))
+        out.append((name, ds, measure, None))
+    for _, ds, _, _ in out:
+        if ds.visibility != "public":
+            raise CompileError(f"{ds.id} is internal and cannot be queried directly")
+    return out
+
+
+def _group_query(base: Dataset, selections: list, contract: QueryContract, catalog: Catalog) -> tuple[exp.Select, list[str]]:
+    """One aggregate SELECT over base (plus many-to-one joins) and the dataset ids it reads."""
+    joins = _joins_from(base, catalog)
+    dims = [(ref, *_resolve(base, ref, catalog, joins)) for ref in contract.dimensions]
+    filters = []
+    for f in contract.filters:
+        if "__" in f.dimension.rpartition(":")[2]:
+            raise CompileError(
+                f"filter dimension {f.dimension!r} has a grain suffix; filters take the plain dimension name"
+            )
+        filters.append((f, *_resolve(base, f.dimension, catalog, joins)[:2]))
+    time_range = None
+    if contract.time_range:
+        target, dim, _ = _resolve(base, contract.time_range.dimension, catalog, joins)
+        if dim.type != "time":
+            raise CompileError(f"time_range needs a time dimension; {contract.time_range.dimension!r} is not one")
+        time_range = (target, dim)
+
+    joined = []
+    for target in [t for _, t, _, _ in dims] + [t for _, t, _ in filters] + ([time_range[0]] if time_range else []):
+        if target.id != base.id and target.id not in joined:
+            joined.append(target.id)
+    alias = {base.id: "b" if joined else None} | {ds_id: f"j{i}" for i, ds_id in enumerate(joined, start=1)}
 
-    dataset_ids = sorted({ds.id for _, ds, _, _ in selections})
-    if len(dataset_ids) > 1:
-        raise CompileError(f"measures from more than one dataset are not supported yet: {dataset_ids}")
-    ds = selections[0][1]
-    if ds.visibility != "public":
-        raise CompileError(f"{ds.id} is internal and cannot be queried directly")
+    def column(target: Dataset, name: str) -> exp.Column:
+        return exp.column(name, table=alias[target.id])
 
     select: list[exp.Expression] = []
-    aliases: list[str] = []
-    for ref in contract.dimensions:
-        dim, grain = _resolve_dimension(ds, ref)
-        if ds.is_pii(dim.column):
+    for ref, target, dim, grain in dims:
+        if target.is_pii(dim.column):
             raise CompileError(f"dimension {dim.name!r} is personally identifiable and cannot be selected")
-        col = exp.column(dim.column)
+        col = column(target, dim.column)
         node = exp.Anonymous(this="DATE_TRUNC", expressions=[exp.Literal.string(grain), col]) if grain else col
-        select.append(exp.alias_(node, ref))
-        aliases.append(ref)
+        select.append(exp.alias_(node, _output_name(ref)))
 
-    for alias, _, measure, metric in selections:
+    for out_name, _, measure, metric in selections:
         scope = None
         if metric and metric.default_filters:
             parts = []
             for fname in metric.default_filters:
-                f = ds.filter(fname)
+                f = base.filter(fname)
                 if f is None:
                     raise CompileError(f"metric {metric.id} uses unknown filter {fname!r}")
-                parts.append(_parse(f.sql, f"{ds.id} filter {fname}"))
+                parts.append(_parse(f.sql, f"{base.id} filter {fname}"))
             scope = exp.and_(*parts)
-        select.append(exp.alias_(_aggregate(ds, measure, scope), alias))
-        aliases.append(alias)
-    if len(set(aliases)) != len(aliases):
-        raise CompileError(f"output names collide: {aliases}")
+        select.append(exp.alias_(_qualified(_aggregate(base, measure, scope), alias[base.id]), out_name))
 
     where: list[exp.Expression] = []
-    for f in contract.filters:
-        dim = ds.dimension(f.dimension)
-        if dim is None:
-            raise CompileError(f"unknown filter dimension {f.dimension!r} on {ds.id}")
-        col = exp.column(dim.column)
+    for f, target, dim in filters:
+        col = column(target, dim.column)
         if dim.type == "time":
             col = exp.Cast(this=col, to=exp.DataType.build("DATE"))
         where.append(_condition(col, f))
-    if contract.time_range:
-        dim = ds.dimension(contract.time_range.dimension)
-        if dim is None or dim.type != "time":
-            raise CompileError(f"time_range needs a time dimension; {contract.time_range.dimension!r} is not one")
-        day = exp.Cast(this=exp.column(dim.column), to=exp.DataType.build("DATE"))
+    if time_range:
+        day = exp.Cast(this=column(*time_range[:1], time_range[1].column), to=exp.DataType.build("DATE"))
         if contract.time_range.start:
             where.append(exp.GTE(this=day, expression=exp.Literal.string(contract.time_range.start.isoformat())))
         if contract.time_range.end:
             where.append(exp.LTE(this=day.copy(), expression=exp.Literal.string(contract.time_range.end.isoformat())))
 
-    query = exp.select(*select).from_(cte_name(ds.id))
+    source = exp.to_table(cte_name(base.id))
+    query = exp.select(*select).from_(exp.alias_(source, "b", table=True) if joined else source)
+    for ds_id in joined:
+        other, mine, theirs = joins[ds_id]
+        on = exp.EQ(this=exp.column(mine, table="b"), expression=exp.column(theirs, table=alias[ds_id]))
+        query = query.join(exp.alias_(exp.to_table(cte_name(ds_id)), alias[ds_id], table=True), on=on, join_type="left")
     if where:
         query = query.where(exp.and_(*where))
     if contract.dimensions:
         query = query.group_by(*[exp.Literal.number(i + 1) for i in range(len(contract.dimensions))])
+    return query, [base.id] + joined
+
+
+def _combine(groups: list[str], contract: QueryContract, measure_names: list[list[str]]) -> exp.Select:
+    """Join per-dataset aggregate CTEs g1..gN on the shared dimensions (null-safe)."""
+    dims = [_output_name(d) for d in contract.dimensions]
+    select = [
+        exp.alias_(exp.Coalesce(this=exp.column(d, table=groups[0]), expressions=[exp.column(d, table=g) for g in groups[1:]]), d)
+        for d in dims
+    ]
+    for g, names in zip(groups, measure_names):
+        select += [exp.column(n, table=g) for n in names]
+    query = exp.select(*select).from_(groups[0])
+    for i, g in enumerate(groups[1:], start=1):
+        if not dims:
+            query = query.join(g, join_type="cross")
+            continue
+        conditions = []
+        for d in dims:
+            prior = [exp.column(d, table=p) for p in groups[:i]]
+            left = prior[0] if len(prior) == 1 else exp.Coalesce(this=prior[0], expressions=prior[1:])
+            conditions.append(exp.NullSafeEQ(this=left, expression=exp.column(d, table=g)))
+        query = query.join(g, on=exp.and_(*conditions), join_type="full outer")
+    return query
+
+
+def compile_query(contract: QueryContract, catalog: Catalog, database: str) -> CompiledQuery:
+    selections = _selections(contract, catalog)
+    aliases = [_output_name(d) for d in contract.dimensions] + [name for name, _, _, _ in selections]
+    if len({a.lower() for a in aliases}) != len(aliases):
+        raise CompileError(f"output names collide: {aliases}")
+
+    bases: dict[str, list] = {}
+    for sel in selections:
+        bases.setdefault(sel[1].id, []).append(sel)
+    groups = [(_group_query(catalog.datasets[ds_id], sels, contract, catalog), sels) for ds_id, sels in bases.items()]
+
+    read = list(dict.fromkeys(ds_id for (_, used), _ in groups for ds_id in used))
+    ctes = build_ctes(catalog, read, database)
+    with_parts = [f"{name} AS (\n{sql}\n)" for name, sql in ctes.items()]
+
+    if len(groups) == 1:
+        query = groups[0][0][0]
+    else:
+        names = [f"g{i}" for i in range(1, len(groups) + 1)]
+        with_parts += [f"{n} AS (\n{q.sql(dialect='snowflake', pretty=True)}\n)" for n, ((q, _), _) in zip(names, groups)]
+        query = _combine(names, contract, [[name for name, _, _, _ in sels] for _, sels in groups])
+
     lowered = {a.lower() for a in aliases}
     for ob in contract.order_by:
         if ob.field.lower() not in lowered:
@@ -225,9 +345,7 @@ def compile_query(contract: QueryContract, catalog: Catalog, database: str) -> C
         query = query.order_by(exp.Ordered(this=exp.column(ob.field), desc=ob.direction == "desc"))
     query = query.limit(contract.limit)
 
-    ctes = build_ctes(catalog, [ds.id], database)
-    with_clause = ",\n".join(f"{name} AS (\n{sql}\n)" for name, sql in ctes.items())
-    sql = f"WITH {with_clause}\n{query.sql(dialect='snowflake', pretty=True)}"
+    sql = "WITH " + ",\n".join(with_parts) + "\n" + query.sql(dialect="snowflake", pretty=True)
     _check_tables(sql, database)
 
     return CompiledQuery(
diff --git a/semantic_layer/contract.py b/semantic_layer/contract.py
index 87ba9a8..472cd37 100644
--- a/semantic_layer/contract.py
+++ b/semantic_layer/contract.py
@@ -5,14 +5,14 @@ from __future__ import annotations
 from datetime import date
 from typing import Literal, Optional, Union
 
-from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator
+from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StrictBool, StrictInt, model_validator
 
 FilterOp = Literal[
     "eq", "neq", "in", "not_in", "gt", "gte", "lt", "lte",
     "between", "is_null", "not_null", "contains",
 ]
 # StrictBool precedes StrictInt so JSON true stays a bool rather than becoming 1.
-FilterValue = Union[StrictBool, StrictInt, float, str]
+FilterValue = Union[StrictBool, StrictInt, FiniteFloat, str]
 
 _ARITY = {
     "is_null": (0, 0), "not_null": (0, 0),
```

- [ ] **Step 5: Verify**

Run: `$PY -m pytest -q tests`
Expected: all pass.

Live run recorded during planning: n/a (no dataset added).

- [ ] **Step 6: Commit, open the PR, merge** (PR body: the summary above plus the live-run line).

### Task 2 (PR 4b): dataset course_filters_ih (bbd-analytics FILTERS_IH)

**Files:** `canonical/datasets/course/course_filters_ih.yaml`

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b feat/dataset-course-filters-ih origin/main
```

- [ ] **Step 2: Implement**

```diff
diff --git a/canonical/datasets/course/course_filters_ih.yaml b/canonical/datasets/course/course_filters_ih.yaml
new file mode 100644
index 0000000..bdcfcd6
--- /dev/null
+++ b/canonical/datasets/course/course_filters_ih.yaml
@@ -0,0 +1,126 @@
+id: dataset.course_filters_ih.v1
+display_name: Reportable courses by hierarchy level
+description: >
+  Reportable courses (top-level, started, with a student enrollment) with each institutional
+  hierarchy node split into levels 1-4, the course design mode, and the SIS delivery method.
+  Courses without a hierarchy node appear once with level values '-'.
+grain: one row per course x institutional hierarchy node x course role
+domain: course
+source: bbdata-bbd-analytics snowflake/migrations/bbd_analytics/repeatable/R__0001_TFV_FILTERS_IH.sql
+base_sql: |
+  WITH student_activity AS (
+      SELECT DISTINCT pc.COURSE_ID, c.FIRST_COURSE_WEEK, c.LAST_COURSE_WEEK,
+             c.FIRST_COURSE_TIME, c.LAST_COURSE_TIME
+      FROM {{ database }}.CDM_LMS.PERSON_COURSE pc
+      JOIN {{ database }}.CDM_LMS.COURSE c ON pc.COURSE_ID = c.ID
+      WHERE pc.STUDENT_IND = TRUE
+  ),
+  course_start_end AS (
+      SELECT co.ID AS COURSE_ID, co.NAME AS COURSE_NAME, co.COURSE_NUMBER,
+             co.CREATED_TIME::DATE AS COURSE_CREATION_DATE,
+             co.DESIGN_MODE,
+             te.NAME AS TERM_NAME,
+             CASE WHEN co.START_TIME IS NOT NULL THEN DATE_TRUNC('week', CONVERT_TIMEZONE(NVL(i.STAGE:timezone::STRING, 'UTC'), co.START_TIME))
+                  WHEN te.START_TIME IS NOT NULL THEN DATE_TRUNC('week', CONVERT_TIMEZONE(NVL(i.STAGE:timezone::STRING, 'UTC'), te.START_TIME))
+             END AS START_WEEK_DATE,
+             CASE WHEN co.END_TIME > CURRENT_DATE() THEN DATE_TRUNC('week', CONVERT_TIMEZONE(NVL(i.STAGE:timezone::STRING, 'UTC'), CURRENT_DATE()))
+                  WHEN te.END_TIME > CURRENT_DATE() THEN DATE_TRUNC('week', CONVERT_TIMEZONE(NVL(i.STAGE:timezone::STRING, 'UTC'), CURRENT_DATE()))
+                  WHEN co.END_TIME <= CURRENT_DATE() THEN DATE_TRUNC('week', CONVERT_TIMEZONE(NVL(i.STAGE:timezone::STRING, 'UTC'), co.END_TIME))
+                  WHEN te.END_TIME <= CURRENT_DATE() THEN DATE_TRUNC('week', CONVERT_TIMEZONE(NVL(i.STAGE:timezone::STRING, 'UTC'), te.END_TIME))
+             END AS END_WEEK_DATE,
+             COALESCE(co.START_TIME, te.START_TIME) AS START_DATE,
+             COALESCE(co.END_TIME, te.END_TIME) AS END_DATE,
+             IFF(COALESCE(co.END_TIME, te.END_TIME) IS NOT NULL, 'Fixed', 'Continuous') AS COURSE_DURATION,
+             IFF(COALESCE(co.END_TIME, te.END_TIME) <= CURRENT_DATE(), 1, 0) AS COURSE_ENDED,
+             IFF(COALESCE(co.START_TIME, te.START_TIME) > CURRENT_DATE(), 'N', 'Y') AS COURSE_REPORTABLE
+      FROM {{ database }}.CDM_LMS.COURSE co
+      LEFT JOIN {{ database }}.CDM_LMS.TERM te ON te.ID = co.TERM_ID
+      JOIN {{ database }}.CDM_LMS.INSTANCE i ON i.ID = co.INSTANCE_ID
+      WHERE co.COURSE_PARENT_ID IS NULL
+  ),
+  course_institution_hierarchy AS (
+      SELECT c.ID AS COURSE_ID, NVL(ih.HIERARCHY_ID_SEQ, '||0||') AS HIERARCHY_ID_SEQ
+      FROM {{ database }}.CDM_LMS.COURSE c
+      LEFT JOIN {{ database }}.CDM_LMS.INSTITUTION_HIERARCHY_COURSE ihc
+        ON c.ID = ihc.COURSE_ID
+       AND NOT (ihc.ROW_DELETED_TIME IS NOT NULL AND c.ROW_DELETED_TIME IS NULL)
+      LEFT JOIN {{ database }}.CDM_LMS.INSTITUTION_HIERARCHY ih ON ih.ID = ihc.INSTITUTION_HIERARCHY_ID
+  ),
+  institution_hierarchy_nodes AS (
+      SELECT DISTINCT TRY_TO_NUMBER(node.VALUE) AS ID, cih.COURSE_ID
+      FROM course_institution_hierarchy cih, LATERAL SPLIT_TO_TABLE(cih.HIERARCHY_ID_SEQ, '||') node
+  ),
+  institution_hierarchy_all AS (
+      SELECT ihn.COURSE_ID,
+             IFF(ihn.ID = 0, '||0||', ih.HIERARCHY_ID_SEQ) AS HIERARCHY_ID_SEQ,
+             COALESCE(IFF(ihn.ID = 0, '-', ih.HIERARCHY_NAME_SEQ), 'All Nodes') AS IH_NODE_NAME,
+             NVL(IFF(ihn.ID = 0, '||0||', ih.HIERARCHY_ID_SEQ), '||-1||') AS IH_PATH,
+             IFF(LEN(SPLIT_PART(IH_NODE_NAME, '||', 1)) > 0, SPLIT_PART(IH_NODE_NAME, '||', 1), SPLIT_PART(IH_NODE_NAME, '||', 2)) AS IH_LVL1,
+             NULLIF(SPLIT_PART(IH_NODE_NAME, '||', 3), '') AS IH_LVL2,
+             NULLIF(SPLIT_PART(IH_NODE_NAME, '||', 4), '') AS IH_LVL3,
+             TRIM(IFF(LEN(SPLIT_PART(IH_NODE_NAME, '||', 5)) > 0,
+                      RIGHT(IH_NODE_NAME, LEN(IH_NODE_NAME) - 6 - LEN(SPLIT_PART(IH_NODE_NAME, '||', 2))
+                            - LEN(SPLIT_PART(IH_NODE_NAME, '||', 3)) - LEN(SPLIT_PART(IH_NODE_NAME, '||', 4))),
+                      '-'), '||') AS IH_LVL4
+      FROM institution_hierarchy_nodes ihn
+      LEFT JOIN {{ database }}.CDM_LMS.INSTITUTION_HIERARCHY ih ON ih.ID = ihn.ID
+  ),
+  sis_attributes AS (
+      SELECT c.ID AS COURSE_ID, sis_s.DELIVERY_METHOD_SOURCE_DESC
+      FROM {{ database }}.CDM_LMS.COURSE c
+      JOIN {{ database }}.CDM_MAP.COURSE c_map ON c_map.LMS_COURSE_ID = c.ID
+      JOIN {{ database }}.CDM_SIS.CLASS_SECTION sis_s ON sis_s.ID = c_map.SIS_CLASS_SECTION_ID
+  )
+  SELECT DISTINCT
+      co.COURSE_ID,
+      co.COURSE_NAME,
+      co.COURSE_NUMBER,
+      co.DESIGN_MODE,
+      sis.DELIVERY_METHOD_SOURCE_DESC,
+      ih.IH_PATH,
+      ih.IH_NODE_NAME,
+      ih.IH_LVL1,
+      COALESCE(ih.IH_LVL2, '-') AS IH_LVL2,
+      COALESCE(ih.IH_LVL3, '-') AS IH_LVL3,
+      COALESCE(ih.IH_LVL4, '-') AS IH_LVL4,
+      IFF(YEAR(co.COURSE_CREATION_DATE) < 1970, TO_DATE('1970-01-01'), co.COURSE_CREATION_DATE) AS COURSE_CREATION_DATE,
+      NVL(co.TERM_NAME, '-') AS TERM_NAME,
+      co.COURSE_ENDED,
+      COALESCE(IFF(YEAR(co.START_WEEK_DATE) < 1970, TO_DATE('1970-01-01'), co.START_WEEK_DATE), sa.FIRST_COURSE_WEEK) AS START_WEEK,
+      COALESCE(IFF(YEAR(co.END_WEEK_DATE) < 1970, TO_DATE('1970-01-01'), co.END_WEEK_DATE), sa.LAST_COURSE_WEEK) AS END_WEEK,
+      COALESCE(IFF(YEAR(co.START_DATE) < 1970, TO_DATE('1970-01-01'), co.START_DATE), sa.FIRST_COURSE_TIME)::DATE AS START_DATE,
+      COALESCE(IFF(YEAR(co.END_DATE) < 1970, TO_DATE('1970-01-01'), co.END_DATE), sa.LAST_COURSE_TIME)::DATE AS END_DATE,
+      DATEDIFF(week, START_WEEK, END_WEEK) + 1 AS COURSE_WEEKS,
+      co.COURSE_DURATION,
+      pc.COURSE_ROLE
+  FROM course_start_end co
+  JOIN student_activity sa ON sa.COURSE_ID = co.COURSE_ID
+  LEFT JOIN {{ database }}.CDM_LMS.PERSON_COURSE pc
+    ON pc.COURSE_ID = co.COURSE_ID AND pc.COURSE_ROLE IS NOT NULL
+  LEFT JOIN institution_hierarchy_all ih ON ih.COURSE_ID = co.COURSE_ID
+  LEFT JOIN sis_attributes sis ON co.COURSE_ID = sis.COURSE_ID
+  WHERE co.COURSE_REPORTABLE = 'Y'
+entities:
+  - {name: course, column: COURSE_ID, type: foreign}
+dimensions:
+  - {name: course_name, column: COURSE_NAME, type: categorical, synonyms: [course, class]}
+  - {name: course_number, column: COURSE_NUMBER, type: categorical, synonyms: [course code]}
+  - {name: design_mode, column: DESIGN_MODE, type: categorical, description: "Learn course view code (e.g. U = Ultra, C = Classic)", synonyms: [course view, ultra or classic]}
+  - {name: delivery_method, column: DELIVERY_METHOD_SOURCE_DESC, type: categorical, description: "SIS class-section delivery method; null when the course has no SIS section", synonyms: [modality, online or in person]}
+  - {name: ih_node_name, column: IH_NODE_NAME, type: categorical, description: "Full hierarchy path names; 'All Nodes' or '-' when the course has none"}
+  - {name: ih_level_1, column: IH_LVL1, type: categorical, synonyms: [institution, campus]}
+  - {name: ih_level_2, column: IH_LVL2, type: categorical, synonyms: [college, school]}
+  - {name: ih_level_3, column: IH_LVL3, type: categorical, synonyms: [department]}
+  - {name: ih_level_4, column: IH_LVL4, type: categorical, description: "Level 4 and everything below it"}
+  - {name: term_name, column: TERM_NAME, type: categorical, synonyms: [term, semester]}
+  - {name: course_role, column: COURSE_ROLE, type: categorical, synonyms: [role]}
+  - {name: course_duration, column: COURSE_DURATION, type: categorical}
+  - {name: course_ended, column: COURSE_ENDED, type: numeric, description: "1 when the course or term end date has passed, else 0"}
+  - {name: course_creation_date, column: COURSE_CREATION_DATE, type: time, grains: [day, week, month, quarter, year]}
+  - {name: start_date, column: START_DATE, type: time, grains: [day, week, month, quarter, year]}
+  - {name: end_date, column: END_DATE, type: time, grains: [day, week, month, quarter, year]}
+measures:
+  - {name: courses, agg: count_distinct, expr: COURSE_ID, unit: courses}
+  - {name: ended_courses, agg: count_distinct, expr: "IFF(COURSE_ENDED = 1, COURSE_ID, NULL)", unit: courses}
+filters:
+  - {name: ongoing, sql: "COURSE_ENDED = 0"}
```

- [ ] **Step 3: Verify**

Run: `$PY -m pytest -q tests`
Expected: all pass.

Live run recorded during planning: every dimension (16) and measure (2) in one query: 5 rows, 18 columns, 3.9 s.

- [ ] **Step 4: Commit, open the PR, merge** (PR body: the summary above plus the live-run line).

### Task 3 (PR 4c): dataset course_filters_all (bbd-analytics FILTERS_ALL_COURSES)

**Files:** `canonical/datasets/course/course_filters_all.yaml`

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b feat/dataset-course-filters-all origin/main
```

- [ ] **Step 2: Implement**

```diff
diff --git a/canonical/datasets/course/course_filters_all.yaml b/canonical/datasets/course/course_filters_all.yaml
new file mode 100644
index 0000000..36ca2f0
--- /dev/null
+++ b/canonical/datasets/course/course_filters_all.yaml
@@ -0,0 +1,101 @@
+id: dataset.course_filters_all.v1
+display_name: All courses by hierarchy level
+description: >
+  Every top-level course, including courses that have not started and courses with no
+  enrollments, with hierarchy levels 1-4 and the roles enrolled (preview and bbsupport test
+  users excluded). start_date and end_date are the Monday one week before the course or term
+  start/end week, as in bbd-analytics. Deviation: bbd-analytics drops courses with no
+  enrollments despite its "all courses" intent; they are kept here with a null course_role.
+grain: one row per course x institutional hierarchy node x course role
+domain: course
+source: bbdata-bbd-analytics snowflake/migrations/bbd_analytics/repeatable/R__TFV_FILTERS_ALL_COURSES.sql
+base_sql: |
+  WITH real_enrollments AS (
+      SELECT pc.COURSE_ID, pc.COURSE_ROLE
+      FROM {{ database }}.CDM_LMS.PERSON_COURSE pc
+      JOIN {{ database }}.CDM_LMS.PERSON pe ON pc.PERSON_ID = pe.ID
+      WHERE pc.COURSE_ROLE IS NOT NULL
+        AND pe.LAST_NAME NOT LIKE '%PreviewUser'
+        AND pe.LAST_NAME NOT LIKE 'bbsupport%'
+  ),
+  course_start_end AS (
+      SELECT co.ID AS COURSE_ID, co.NAME AS COURSE_NAME, co.COURSE_NUMBER,
+             CONVERT_TIMEZONE(NVL(i.STAGE:timezone::STRING, 'UTC'), co.CREATED_TIME)::DATE AS COURSE_CREATION_DATE,
+             te.NAME AS TERM_NAME,
+             CASE WHEN co.START_DATE IS NOT NULL THEN DATEADD(day, -7, DATE_TRUNC('week', co.START_DATE))
+                  WHEN te.START_DATE IS NOT NULL THEN DATEADD(day, -7, DATE_TRUNC('week', te.START_DATE))
+             END AS START_DATE,
+             IFF(COALESCE(co.END_DATE, te.END_DATE) IS NOT NULL, 'Fixed', 'Continuous') AS COURSE_DURATION,
+             IFF(COALESCE(co.END_DATE, te.END_DATE) <= CURRENT_DATE(), 1, 0) AS COURSE_ENDED
+      FROM {{ database }}.CDM_LMS.COURSE co
+      LEFT JOIN {{ database }}.CDM_LMS.TERM te ON te.ID = co.TERM_ID
+      LEFT JOIN {{ database }}.CDM_LMS.INSTANCE i ON i.ID = co.INSTANCE_ID
+      WHERE co.COURSE_PARENT_ID IS NULL
+  ),
+  course_institution_hierarchy AS (
+      SELECT c.ID AS COURSE_ID, NVL(ih.HIERARCHY_ID_SEQ, '||0||') AS HIERARCHY_ID_SEQ
+      FROM {{ database }}.CDM_LMS.COURSE c
+      LEFT JOIN {{ database }}.CDM_LMS.INSTITUTION_HIERARCHY_COURSE ihc
+        ON c.ID = ihc.COURSE_ID
+       AND NOT (ihc.ROW_DELETED_TIME IS NOT NULL AND c.ROW_DELETED_TIME IS NULL)
+      LEFT JOIN {{ database }}.CDM_LMS.INSTITUTION_HIERARCHY ih ON ih.ID = ihc.INSTITUTION_HIERARCHY_ID
+  ),
+  institution_hierarchy_nodes AS (
+      SELECT DISTINCT TRY_TO_NUMBER(node.VALUE) AS ID, cih.COURSE_ID
+      FROM course_institution_hierarchy cih, LATERAL SPLIT_TO_TABLE(cih.HIERARCHY_ID_SEQ, '||') node
+  ),
+  institution_hierarchy_all AS (
+      SELECT ihn.COURSE_ID,
+             COALESCE(IFF(ihn.ID = 0, '-', ih.HIERARCHY_NAME_SEQ), 'All Nodes') AS IH_NODE_NAME,
+             NVL(IFF(ihn.ID = 0, '||0||', ih.HIERARCHY_ID_SEQ), '||-1||') AS IH_PATH,
+             IFF(LEN(SPLIT_PART(IH_NODE_NAME, '||', 1)) > 0, SPLIT_PART(IH_NODE_NAME, '||', 1), SPLIT_PART(IH_NODE_NAME, '||', 2)) AS IH_LVL1,
+             NULLIF(SPLIT_PART(IH_NODE_NAME, '||', 3), '') AS IH_LVL2,
+             NULLIF(SPLIT_PART(IH_NODE_NAME, '||', 4), '') AS IH_LVL3,
+             TRIM(IFF(LEN(SPLIT_PART(IH_NODE_NAME, '||', 5)) > 0,
+                      RIGHT(IH_NODE_NAME, LEN(IH_NODE_NAME) - 6 - LEN(SPLIT_PART(IH_NODE_NAME, '||', 2))
+                            - LEN(SPLIT_PART(IH_NODE_NAME, '||', 3)) - LEN(SPLIT_PART(IH_NODE_NAME, '||', 4))),
+                      '-'), '||') AS IH_LVL4
+      FROM institution_hierarchy_nodes ihn
+      LEFT JOIN {{ database }}.CDM_LMS.INSTITUTION_HIERARCHY ih ON ih.ID = ihn.ID
+  )
+  SELECT DISTINCT
+      co.COURSE_ID,
+      co.COURSE_NAME,
+      co.COURSE_NUMBER,
+      ih.IH_PATH,
+      ih.IH_NODE_NAME,
+      ih.IH_LVL1,
+      COALESCE(ih.IH_LVL2, '-') AS IH_LVL2,
+      COALESCE(ih.IH_LVL3, '-') AS IH_LVL3,
+      COALESCE(ih.IH_LVL4, '-') AS IH_LVL4,
+      IFF(YEAR(co.COURSE_CREATION_DATE) < 1970, TO_DATE('1970-01-01'), co.COURSE_CREATION_DATE) AS COURSE_CREATION_DATE,
+      NVL(co.TERM_NAME, '-') AS TERM_NAME,
+      co.COURSE_ENDED,
+      IFF(YEAR(co.START_DATE) < 1970, TO_DATE('1970-01-01'), co.START_DATE) AS START_DATE,
+      co.COURSE_DURATION,
+      re.COURSE_ROLE
+  FROM course_start_end co
+  LEFT JOIN real_enrollments re ON re.COURSE_ID = co.COURSE_ID
+  LEFT JOIN institution_hierarchy_all ih ON ih.COURSE_ID = co.COURSE_ID
+entities:
+  - {name: course, column: COURSE_ID, type: foreign}
+dimensions:
+  - {name: course_name, column: COURSE_NAME, type: categorical, synonyms: [course, class]}
+  - {name: course_number, column: COURSE_NUMBER, type: categorical, synonyms: [course code]}
+  - {name: ih_node_name, column: IH_NODE_NAME, type: categorical}
+  - {name: ih_level_1, column: IH_LVL1, type: categorical, synonyms: [institution, campus]}
+  - {name: ih_level_2, column: IH_LVL2, type: categorical, synonyms: [college, school]}
+  - {name: ih_level_3, column: IH_LVL3, type: categorical, synonyms: [department]}
+  - {name: ih_level_4, column: IH_LVL4, type: categorical}
+  - {name: term_name, column: TERM_NAME, type: categorical, synonyms: [term, semester]}
+  - {name: course_role, column: COURSE_ROLE, type: categorical, description: "Null for a course with no enrollments", synonyms: [role]}
+  - {name: course_duration, column: COURSE_DURATION, type: categorical}
+  - {name: course_ended, column: COURSE_ENDED, type: numeric, description: "1 when the course or term end date has passed, else 0"}
+  - {name: course_creation_date, column: COURSE_CREATION_DATE, type: time, grains: [day, week, month, quarter, year]}
+  - {name: start_date, column: START_DATE, type: time, grains: [day, week, month, quarter, year], description: "Monday one week before the course start week"}
+measures:
+  - {name: courses, agg: count_distinct, expr: COURSE_ID, unit: courses}
+  - {name: courses_without_enrollments, agg: count_distinct, expr: "IFF(COURSE_ROLE IS NULL, COURSE_ID, NULL)", unit: courses}
+  - {name: courses_not_started, agg: count_distinct, expr: "IFF(START_DATE > CURRENT_DATE(), COURSE_ID, NULL)", unit: courses}
+filters:
+  - {name: ongoing, sql: "COURSE_ENDED = 0"}
```

- [ ] **Step 3: Verify**

Run: `$PY -m pytest -q tests`
Expected: all pass.

Live run recorded during planning: every dimension (13) and measure (3): 5 rows, 4.4 s; courses with no enrollments present (courses_without_enrollments > 0).

- [ ] **Step 4: Commit, open the PR, merge** (PR body: the summary above plus the live-run line).

### Task 4 (PR 4d): dataset course_catalog (bbd-analytics COURSE_FILTER)

**Files:** `canonical/datasets/course/course_catalog.yaml`

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b feat/dataset-course-catalog origin/main
```

- [ ] **Step 2: Implement**

```diff
diff --git a/canonical/datasets/course/course_catalog.yaml b/canonical/datasets/course/course_catalog.yaml
new file mode 100644
index 0000000..4396f3b
--- /dev/null
+++ b/canonical/datasets/course/course_catalog.yaml
@@ -0,0 +1,180 @@
+id: dataset.course_catalog.v1
+display_name: Course catalog with status flags
+description: >
+  Every course, including child courses and courses not yet started, with its hierarchy levels,
+  dates (creation, start, end, real end, first and last student activity), roles enrolled (preview
+  and bbsupport users excluded), design mode, and status flags. Deviations: course_weeks uses the
+  week-truncated definition shared with course_filters; design mode is looked up by the course's
+  canonical code rather than its source code.
+grain: one row per course x institutional hierarchy node x course role
+domain: course
+source: bbdata-bbd-analytics snowflake/migrations/bbd_analytics/repeatable/R__TFV_COURSE_FILTER.sql
+base_sql: |
+  WITH course_roles AS (
+      SELECT DISTINCT pc.COURSE_ID, pc.COURSE_ROLE, pc.COURSE_ROLE_SOURCE_DESC
+      FROM {{ database }}.CDM_LMS.PERSON_COURSE pc
+      JOIN {{ database }}.CDM_LMS.PERSON pe ON pe.ID = pc.PERSON_ID
+      WHERE pe.LAST_NAME NOT LIKE '%PreviewUser'
+        AND pe.LAST_NAME NOT LIKE 'bbsupport%'
+  ),
+  student_activity AS (
+      SELECT pc.COURSE_ID,
+             MIN(ca.FIRST_ACCESSED_TIME) AS FIRST_COURSE_DAY,
+             MAX(ca.FIRST_ACCESSED_TIME) AS LAST_COURSE_DAY
+      FROM {{ database }}.CDM_LMS.PERSON_COURSE pc
+      JOIN {{ database }}.CDM_LMS.COURSE_ACTIVITY ca
+        ON pc.COURSE_ID = ca.COURSE_ID AND pc.PERSON_ID = ca.PERSON_ID
+      WHERE pc.STUDENT_IND = TRUE
+      GROUP BY pc.COURSE_ID
+  ),
+  act_as_instructor AS (
+      SELECT DISTINCT COURSE_ID FROM {{ database }}.CDM_LMS.PERSON_COURSE WHERE ACT_AS_INSTRUCTOR_IND
+  ),
+  parent_course AS (
+      SELECT DISTINCT COURSE_PARENT_ID AS COURSE_ID
+      FROM {{ database }}.CDM_LMS.COURSE
+      WHERE COURSE_PARENT_ID IS NOT NULL
+  ),
+  course_start_end AS (
+      SELECT co.INSTANCE_ID, co.ID AS COURSE_ID, co.NAME AS COURSE_NAME, co.COURSE_NUMBER,
+             co.CREATED_TIME AS COURSE_CREATION_DATE,
+             te.NAME AS TERM_NAME,
+             COALESCE(co.START_TIME, te.START_TIME) AS START_DAY,
+             CASE WHEN co.END_TIME > CURRENT_TIMESTAMP() THEN CURRENT_TIMESTAMP()
+                  WHEN te.END_TIME > CURRENT_TIMESTAMP() THEN CURRENT_TIMESTAMP()
+                  WHEN co.END_TIME <= CURRENT_TIMESTAMP() THEN co.END_TIME
+                  WHEN te.END_TIME <= CURRENT_TIMESTAMP() THEN te.END_TIME
+             END AS END_DAY,
+             COALESCE(co.END_TIME, te.END_TIME) AS END_REAL_DAY,
+             IFF(COALESCE(co.END_DATE, te.END_DATE) IS NOT NULL, 'Fixed', 'Continuous') AS COURSE_DURATION,
+             IFF(COALESCE(co.END_DATE, te.END_DATE) <= CURRENT_TIMESTAMP(), 1, 0) AS COURSE_ENDED,
+             IFF(COALESCE(co.START_TIME, te.START_TIME) > CURRENT_TIMESTAMP(), 'N', 'Y') AS COURSE_REPORTABLE,
+             IFF(co.COURSE_PARENT_ID IS NULL, 0, 1) AS CHILD_COURSE_IND,
+             NVL(cd.CANON_DESC, 'No explicit mode') AS DESIGN_MODE
+      FROM {{ database }}.CDM_LMS.COURSE co
+      LEFT JOIN {{ database }}.CDM_LMS.TERM te ON te.ID = co.TERM_ID
+      LEFT JOIN {{ database }}.CDM_META.CANON_DEFINITION cd
+        ON cd.CANON_CODE = co.DESIGN_MODE AND cd.SOURCE_DOMAIN = 'LMS' AND cd.NAME = 'DESIGN_MODE'
+  ),
+  course_institution_hierarchy AS (
+      SELECT c.ID AS COURSE_ID,
+             NVL(ih.HIERARCHY_ID_SEQ, '||0||') AS HIERARCHY_ID_SEQ,
+             NVL(ihc.PRIMARY_IND, TRUE) AS IH_PRIMARY_IND
+      FROM {{ database }}.CDM_LMS.COURSE c
+      LEFT JOIN {{ database }}.CDM_LMS.INSTITUTION_HIERARCHY_COURSE ihc
+        ON c.ID = ihc.COURSE_ID
+       AND NOT (ihc.ROW_DELETED_TIME IS NOT NULL AND c.ROW_DELETED_TIME IS NULL)
+      LEFT JOIN {{ database }}.CDM_LMS.INSTITUTION_HIERARCHY ih ON ih.ID = ihc.INSTITUTION_HIERARCHY_ID
+  ),
+  institution_hierarchy_nodes AS (
+      SELECT DISTINCT TRY_TO_NUMBER(node.VALUE) AS ID, cih.COURSE_ID
+      FROM course_institution_hierarchy cih, LATERAL SPLIT_TO_TABLE(cih.HIERARCHY_ID_SEQ, '||') node
+  ),
+  institution_hierarchy_all AS (
+      SELECT ihn.COURSE_ID,
+             COALESCE(IFF(ihn.ID = 0, '-', ih.HIERARCHY_NAME_SEQ), 'All Nodes') AS IH_NODE_NAME,
+             NVL(IFF(ihn.ID = 0, '||0||', ih.HIERARCHY_ID_SEQ), '||-1||') AS IH_PATH,
+             IFF(LEN(SPLIT_PART(IH_NODE_NAME, '||', 1)) > 0, SPLIT_PART(IH_NODE_NAME, '||', 1), SPLIT_PART(IH_NODE_NAME, '||', 2)) AS IH_LVL1,
+             NULLIF(SPLIT_PART(IH_NODE_NAME, '||', 3), '') AS IH_LVL2,
+             NULLIF(SPLIT_PART(IH_NODE_NAME, '||', 4), '') AS IH_LVL3,
+             TRIM(IFF(LEN(SPLIT_PART(IH_NODE_NAME, '||', 5)) > 0,
+                      RIGHT(IH_NODE_NAME, LEN(IH_NODE_NAME) - 6 - LEN(SPLIT_PART(IH_NODE_NAME, '||', 2))
+                            - LEN(SPLIT_PART(IH_NODE_NAME, '||', 3)) - LEN(SPLIT_PART(IH_NODE_NAME, '||', 4))),
+                      '-'), '||') AS IH_LVL4
+      FROM institution_hierarchy_nodes ihn
+      LEFT JOIN {{ database }}.CDM_LMS.INSTITUTION_HIERARCHY ih ON ih.ID = ihn.ID
+  )
+  SELECT DISTINCT
+      co.COURSE_ID,
+      co.COURSE_NAME,
+      co.COURSE_NUMBER,
+      ih.IH_PATH,
+      ih.IH_NODE_NAME,
+      ih.IH_LVL1,
+      COALESCE(ih.IH_LVL2, '-') AS IH_LVL2,
+      COALESCE(ih.IH_LVL3, '-') AS IH_LVL3,
+      COALESCE(ih.IH_LVL4, '-') AS IH_LVL4,
+      NVL(co.TERM_NAME, '-') AS TERM_NAME,
+      CONCAT(co.COURSE_NUMBER, ' - ', co.COURSE_NAME) AS COURSE_ID_NAME,
+      co.COURSE_ENDED,
+      co.COURSE_REPORTABLE,
+      IFF(co.COURSE_CREATION_DATE < '1970-01-01'::DATE, '1970-01-01'::DATE,
+          CONVERT_TIMEZONE(NVL(i.STAGE:timezone::STRING, 'UTC'), co.COURSE_CREATION_DATE))::DATE AS COURSE_CREATION_DATE,
+      IFF(COALESCE(co.START_DAY, sa.FIRST_COURSE_DAY) < '1970-01-01'::DATE, '1970-01-01'::DATE,
+          CONVERT_TIMEZONE(NVL(i.STAGE:timezone::STRING, 'UTC'), COALESCE(co.START_DAY, sa.FIRST_COURSE_DAY)))::DATE AS START_DATE,
+      IFF(COALESCE(co.END_DAY, sa.LAST_COURSE_DAY) < '1970-01-01'::DATE, '1970-01-01'::DATE,
+          CONVERT_TIMEZONE(NVL(i.STAGE:timezone::STRING, 'UTC'), COALESCE(co.END_DAY, sa.LAST_COURSE_DAY)))::DATE AS END_DATE,
+      IFF(COALESCE(co.END_REAL_DAY, sa.LAST_COURSE_DAY) < '1970-01-01'::DATE, '1970-01-01'::DATE,
+          CONVERT_TIMEZONE(NVL(i.STAGE:timezone::STRING, 'UTC'), COALESCE(co.END_REAL_DAY, sa.LAST_COURSE_DAY)))::DATE AS END_REAL_DATE,
+      DATE_TRUNC('week', START_DATE) AS START_WEEK,
+      DATE_TRUNC('week', END_DATE) AS END_WEEK,
+      DATE_TRUNC('week', END_REAL_DATE) AS END_REAL_WEEK,
+      DATEDIFF(day, START_DATE, END_DATE) / 7 AS COURSE_WEEKS_DECIMAL,
+      DATEDIFF(week, START_WEEK, END_WEEK) + 1 AS COURSE_WEEKS,
+      IFF(sa.FIRST_COURSE_DAY < '1970-01-01'::DATE, '1970-01-01'::DATE,
+          CONVERT_TIMEZONE(NVL(i.STAGE:timezone::STRING, 'UTC'), sa.FIRST_COURSE_DAY))::DATE AS START_STUDENT_ACTIVITY_DATE,
+      IFF(sa.LAST_COURSE_DAY < '1970-01-01'::DATE, '1970-01-01'::DATE,
+          CONVERT_TIMEZONE(NVL(i.STAGE:timezone::STRING, 'UTC'), sa.LAST_COURSE_DAY))::DATE AS END_STUDENT_ACTIVITY_DATE,
+      co.COURSE_DURATION,
+      pc.COURSE_ROLE,
+      NVL(t.TRANSLATION_VALUE, pc.COURSE_ROLE_SOURCE_DESC) AS COURSE_ROLE_DESCRIPTION,
+      IFF(aai.COURSE_ID IS NOT NULL, 1, 0) AS ACT_AS_INSTRUCTOR_IND,
+      IFF(COALESCE(co.START_DAY, sa.FIRST_COURSE_DAY) <= CURRENT_DATE(), 1, 0) AS COURSE_STARTED_IND,
+      IFF(sa.COURSE_ID IS NOT NULL, 1, 0) AS STUDENT_ACTIVITY_IND,
+      co.CHILD_COURSE_IND,
+      IFF(pa.COURSE_ID IS NULL, 0, 1) AS PARENT_COURSE_IND,
+      co.DESIGN_MODE,
+      NVL(cih.IH_PRIMARY_IND, FALSE) AS IH_PRIMARY_IND
+  FROM course_start_end co
+  LEFT JOIN {{ database }}.CDM_LMS.INSTANCE i ON co.INSTANCE_ID = i.ID
+  LEFT JOIN student_activity sa ON sa.COURSE_ID = co.COURSE_ID
+  LEFT JOIN course_roles pc ON pc.COURSE_ID = co.COURSE_ID
+  LEFT JOIN {{ database }}.CDM_LMS.TRANSLATION t
+    ON pc.COURSE_ROLE_SOURCE_DESC = t.TRANSLATION_KEY AND t.LOCALE = 'en_US'
+  LEFT JOIN institution_hierarchy_all ih ON ih.COURSE_ID = co.COURSE_ID
+  LEFT JOIN act_as_instructor aai ON aai.COURSE_ID = co.COURSE_ID
+  LEFT JOIN parent_course pa ON pa.COURSE_ID = co.COURSE_ID
+  LEFT JOIN course_institution_hierarchy cih
+    ON cih.COURSE_ID = co.COURSE_ID AND cih.HIERARCHY_ID_SEQ = ih.IH_PATH
+entities:
+  - {name: course, column: COURSE_ID, type: foreign}
+dimensions:
+  - {name: course_id_name, column: COURSE_ID_NAME, type: categorical, description: "'<course number> - <course name>'", synonyms: [course]}
+  - {name: course_name, column: COURSE_NAME, type: categorical}
+  - {name: course_number, column: COURSE_NUMBER, type: categorical, synonyms: [course code]}
+  - {name: ih_node_name, column: IH_NODE_NAME, type: categorical}
+  - {name: ih_level_1, column: IH_LVL1, type: categorical, synonyms: [institution, campus]}
+  - {name: ih_level_2, column: IH_LVL2, type: categorical, synonyms: [college, school]}
+  - {name: ih_level_3, column: IH_LVL3, type: categorical, synonyms: [department]}
+  - {name: ih_level_4, column: IH_LVL4, type: categorical}
+  - {name: ih_primary, column: IH_PRIMARY_IND, type: boolean, description: "True when this node is the course's primary hierarchy node"}
+  - {name: term_name, column: TERM_NAME, type: categorical, synonyms: [term, semester]}
+  - {name: course_role, column: COURSE_ROLE, type: categorical, synonyms: [role]}
+  - {name: course_role_description, column: COURSE_ROLE_DESCRIPTION, type: categorical, synonyms: [role name]}
+  - {name: design_mode, column: DESIGN_MODE, type: categorical, synonyms: [ultra or classic, course view]}
+  - {name: course_duration, column: COURSE_DURATION, type: categorical}
+  - {name: course_reportable, column: COURSE_REPORTABLE, type: categorical, description: "'Y' once the course or term start has passed"}
+  - {name: course_ended, column: COURSE_ENDED, type: numeric}
+  - {name: course_started, column: COURSE_STARTED_IND, type: numeric}
+  - {name: has_student_activity, column: STUDENT_ACTIVITY_IND, type: numeric}
+  - {name: has_instructor, column: ACT_AS_INSTRUCTOR_IND, type: numeric}
+  - {name: is_child_course, column: CHILD_COURSE_IND, type: numeric}
+  - {name: is_parent_course, column: PARENT_COURSE_IND, type: numeric}
+  - {name: course_weeks, column: COURSE_WEEKS, type: numeric}
+  - {name: course_creation_date, column: COURSE_CREATION_DATE, type: time, grains: [day, week, month, quarter, year]}
+  - {name: start_date, column: START_DATE, type: time, grains: [day, week, month, quarter, year]}
+  - {name: end_date, column: END_DATE, type: time, grains: [day, week, month, quarter, year], description: "Capped at today while the course is running"}
+  - {name: end_real_date, column: END_REAL_DATE, type: time, grains: [day, week, month, quarter, year], description: "The scheduled end date, not capped"}
+  - {name: first_student_activity, column: START_STUDENT_ACTIVITY_DATE, type: time, grains: [day, week, month, quarter, year]}
+  - {name: last_student_activity, column: END_STUDENT_ACTIVITY_DATE, type: time, grains: [day, week, month, quarter, year]}
+measures:
+  - {name: courses, agg: count_distinct, expr: COURSE_ID, unit: courses}
+  - {name: courses_with_student_activity, agg: count_distinct, expr: "IFF(STUDENT_ACTIVITY_IND = 1, COURSE_ID, NULL)", unit: courses}
+  - {name: courses_with_instructor, agg: count_distinct, expr: "IFF(ACT_AS_INSTRUCTOR_IND = 1, COURSE_ID, NULL)", unit: courses}
+  - {name: share_with_student_activity, agg: ratio, numerator: courses_with_student_activity, denominator: courses, unit: ratio}
+filters:
+  - {name: reportable, sql: "COURSE_REPORTABLE = 'Y'"}
+  - {name: ongoing, sql: "COURSE_ENDED = 0"}
+  - {name: top_level, sql: "CHILD_COURSE_IND = 0"}
+  - {name: ultra, sql: "DESIGN_MODE = 'Ultra'"}
+  - {name: classic, sql: "DESIGN_MODE = 'Classic'"}
```

- [ ] **Step 3: Verify**

Run: `$PY -m pytest -q tests`
Expected: all pass.

Live run recorded during planning: every dimension (28) and measure (4): 3 rows, 32 columns, 7.2 s.

- [ ] **Step 4: Commit, open the PR, merge** (PR body: the summary above plus the live-run line).

### Task 5 (PR 4e): dataset active_students (bbd-analytics ACTIVE_STUDENTS)

**Files:** `canonical/datasets/course/course_filters.yaml`, `canonical/datasets/course/course_filters_ih.yaml`, `canonical/datasets/enrollment/active_students.yaml`

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b feat/dataset-active-students origin/main
```

- [ ] **Step 2: Implement**

```diff
diff --git a/canonical/datasets/course/course_filters.yaml b/canonical/datasets/course/course_filters.yaml
index 31e6e7c..0cc4a55 100644
--- a/canonical/datasets/course/course_filters.yaml
+++ b/canonical/datasets/course/course_filters.yaml
@@ -62,8 +62,8 @@ base_sql: |
       IFF(YEAR(co.COURSE_CREATION_DATE) < 1970, TO_DATE('1970-01-01'), co.COURSE_CREATION_DATE) AS COURSE_CREATION_DATE,
       NVL(co.TERM_NAME, '-') AS TERM_NAME,
       co.COURSE_ENDED,
-      COALESCE(IFF(YEAR(co.START_WEEK_DATE) < 1970, TO_DATE('1970-01-01'), co.START_WEEK_DATE), sa.FIRST_COURSE_WEEK) AS START_WEEK,
-      COALESCE(IFF(YEAR(co.END_WEEK_DATE) < 1970, TO_DATE('1970-01-01'), co.END_WEEK_DATE), sa.LAST_COURSE_WEEK) AS END_WEEK,
+      COALESCE(IFF(YEAR(co.START_WEEK_DATE) < 1970, TO_DATE('1970-01-01'), co.START_WEEK_DATE), sa.FIRST_COURSE_WEEK)::DATE AS START_WEEK,
+      COALESCE(IFF(YEAR(co.END_WEEK_DATE) < 1970, TO_DATE('1970-01-01'), co.END_WEEK_DATE), sa.LAST_COURSE_WEEK)::DATE AS END_WEEK,
       COALESCE(IFF(YEAR(co.START_DATE) < 1970, TO_DATE('1970-01-01'), co.START_DATE), sa.FIRST_COURSE_TIME)::DATE AS START_DATE,
       COALESCE(IFF(YEAR(co.END_DATE) < 1970, TO_DATE('1970-01-01'), co.END_DATE), sa.LAST_COURSE_TIME)::DATE AS END_DATE,
       DATEDIFF(week, START_WEEK, END_WEEK) + 1 AS COURSE_WEEKS,
diff --git a/canonical/datasets/course/course_filters_ih.yaml b/canonical/datasets/course/course_filters_ih.yaml
index bdcfcd6..e2f07ff 100644
--- a/canonical/datasets/course/course_filters_ih.yaml
+++ b/canonical/datasets/course/course_filters_ih.yaml
@@ -86,8 +86,8 @@ base_sql: |
       IFF(YEAR(co.COURSE_CREATION_DATE) < 1970, TO_DATE('1970-01-01'), co.COURSE_CREATION_DATE) AS COURSE_CREATION_DATE,
       NVL(co.TERM_NAME, '-') AS TERM_NAME,
       co.COURSE_ENDED,
-      COALESCE(IFF(YEAR(co.START_WEEK_DATE) < 1970, TO_DATE('1970-01-01'), co.START_WEEK_DATE), sa.FIRST_COURSE_WEEK) AS START_WEEK,
-      COALESCE(IFF(YEAR(co.END_WEEK_DATE) < 1970, TO_DATE('1970-01-01'), co.END_WEEK_DATE), sa.LAST_COURSE_WEEK) AS END_WEEK,
+      COALESCE(IFF(YEAR(co.START_WEEK_DATE) < 1970, TO_DATE('1970-01-01'), co.START_WEEK_DATE), sa.FIRST_COURSE_WEEK)::DATE AS START_WEEK,
+      COALESCE(IFF(YEAR(co.END_WEEK_DATE) < 1970, TO_DATE('1970-01-01'), co.END_WEEK_DATE), sa.LAST_COURSE_WEEK)::DATE AS END_WEEK,
       COALESCE(IFF(YEAR(co.START_DATE) < 1970, TO_DATE('1970-01-01'), co.START_DATE), sa.FIRST_COURSE_TIME)::DATE AS START_DATE,
       COALESCE(IFF(YEAR(co.END_DATE) < 1970, TO_DATE('1970-01-01'), co.END_DATE), sa.LAST_COURSE_TIME)::DATE AS END_DATE,
       DATEDIFF(week, START_WEEK, END_WEEK) + 1 AS COURSE_WEEKS,
diff --git a/canonical/datasets/enrollment/active_students.yaml b/canonical/datasets/enrollment/active_students.yaml
new file mode 100644
index 0000000..83c6597
--- /dev/null
+++ b/canonical/datasets/enrollment/active_students.yaml
@@ -0,0 +1,67 @@
+id: dataset.active_students.v1
+display_name: Active student enrollments
+description: >
+  Student enrollments with course activity inside the course window (course start week through
+  one week after the end week), in courses that have at least one instructor-role enrollment.
+  `active` is 1 when the enrollment is available, enabled and not deleted.
+grain: one row per student enrollment (person_course)
+domain: enrollment
+source: bbdata-bbd-analytics snowflake/migrations/bbd_analytics/repeatable/R__HLP_PROCESS_ACTIVE_STUDENTS.sql
+depends_on: [dataset.course_filters.v1]
+base_sql: |
+  WITH students_in_courses AS (
+      SELECT pc.COURSE_ID, pc.PERSON_ID, pc.ID AS PERSON_COURSE_ID, c.TERM_ID,
+             COALESCE(pe.STAGE:student_id, pe.STAGE:user_id, pe.STAGE:batch_uid, pe.SOURCE_ID)::STRING AS ALTERNATIVE_SOURCE_ID,
+             pc.COURSE_ROLE, pe.FIRST_NAME, pe.LAST_NAME, pe.EMAIL AS STUDENT_EMAIL,
+             c.COURSE_NUMBER, c.NAME AS COURSE_NAME,
+             IFF(pc.AVAILABLE_IND AND pc.ENABLED_IND AND pc.ROW_DELETED_TIME IS NULL, 1, 0) AS ACTIVE,
+             c.ROW_DELETED_TIME IS NOT NULL AS COURSE_DELETED_IND
+      FROM {{ database }}.CDM_LMS.PERSON_COURSE pc
+      JOIN {{ database }}.CDM_LMS.PERSON pe ON pe.ID = pc.PERSON_ID
+      JOIN {{ database }}.CDM_LMS.COURSE c ON c.ID = pc.COURSE_ID
+      WHERE pc.STUDENT_IND
+        AND EXISTS (
+            SELECT 1 FROM {{ database }}.CDM_LMS.PERSON_COURSE i
+            WHERE i.ACT_AS_INSTRUCTOR_IND AND i.COURSE_ID = pc.COURSE_ID
+        )
+  ),
+  activity_intervals AS (
+      SELECT DISTINCT START_WEEK AS INTERVAL_START, END_WEEK + 7 AS INTERVAL_END,
+             COURSE_ID, END_WEEK, COURSE_WEEKS, COURSE_CREATION_DATE, COURSE_DURATION
+      FROM {{ ref('dataset.course_filters.v1') }}
+      WHERE COURSE_ROLE = 'S'
+  )
+  SELECT DISTINCT
+      sc.COURSE_ID, sc.PERSON_ID, sc.PERSON_COURSE_ID, sc.TERM_ID,
+      sc.ALTERNATIVE_SOURCE_ID, sc.COURSE_ROLE, sc.FIRST_NAME, sc.LAST_NAME, sc.STUDENT_EMAIL,
+      sc.COURSE_NUMBER, sc.COURSE_NAME, sc.ACTIVE, sc.COURSE_DELETED_IND,
+      ai.INTERVAL_START AS START_WEEK, ai.END_WEEK, ai.COURSE_WEEKS, ai.COURSE_CREATION_DATE, ai.COURSE_DURATION
+  FROM {{ database }}.CDM_LMS.COURSE_ACTIVITY ca
+  JOIN students_in_courses sc ON ca.PERSON_COURSE_ID = sc.PERSON_COURSE_ID
+  JOIN activity_intervals ai
+    ON ai.COURSE_ID = sc.COURSE_ID
+   AND ca.FIRST_ACCESSED_TIME BETWEEN ai.INTERVAL_START AND ai.INTERVAL_END
+entities:
+  - {name: person_course, column: PERSON_COURSE_ID, type: primary}
+  - {name: course, column: COURSE_ID, type: foreign}
+  - {name: person, column: PERSON_ID, type: foreign}
+dimensions:
+  - {name: course_name, column: COURSE_NAME, type: categorical, synonyms: [course, class]}
+  - {name: course_number, column: COURSE_NUMBER, type: categorical, synonyms: [course code]}
+  - {name: course_duration, column: COURSE_DURATION, type: categorical}
+  - {name: active, column: ACTIVE, type: numeric, description: "1 when the enrollment is available, enabled and not deleted"}
+  - {name: course_deleted, column: COURSE_DELETED_IND, type: boolean}
+  - {name: course_weeks, column: COURSE_WEEKS, type: numeric}
+  - {name: course_start_week, column: START_WEEK, type: time, grains: [week, month, quarter, year], synonyms: [course start]}
+  - {name: course_creation_date, column: COURSE_CREATION_DATE, type: time, grains: [day, week, month, quarter, year]}
+  - {name: student_email, column: STUDENT_EMAIL, type: categorical, description: "Filter only; never selectable"}
+measures:
+  - {name: active_students, agg: count_distinct, expr: "IFF(ACTIVE = 1, PERSON_ID, NULL)", unit: students, synonyms: [active learners, engaged students]}
+  - {name: students, agg: count_distinct, expr: PERSON_ID, unit: students, synonyms: [students with activity]}
+  - {name: enrollments, agg: count_distinct, expr: PERSON_COURSE_ID, unit: enrollments}
+  - {name: active_enrollments, agg: count_distinct, expr: "IFF(ACTIVE = 1, PERSON_COURSE_ID, NULL)", unit: enrollments}
+  - {name: share_active, agg: ratio, numerator: active_enrollments, denominator: enrollments, unit: ratio}
+filters:
+  - {name: active_only, sql: "ACTIVE = 1"}
+  - {name: live_courses, sql: "NOT COURSE_DELETED_IND"}
+pii_columns: [PERSON_ID, PERSON_COURSE_ID, ALTERNATIVE_SOURCE_ID, FIRST_NAME, LAST_NAME, STUDENT_EMAIL]
```

- [ ] **Step 3: Verify**

Run: `$PY -m pytest -q tests`
Expected: all pass.

Live run recorded during planning: chained on course_filters; every non-PII dimension (8) and measure (5): 5 rows, 4.4 s. Live run caught END_WEEK + 7 on TIMESTAMP_TZ; course_filters and course_filters_ih now cast week columns to DATE as the bbd-analytics tables do.

- [ ] **Step 4: Commit, open the PR, merge** (PR body: the summary above plus the live-run line).

### Task 6 (PR 4f): dataset course_role_activity; counts of PII columns are not PII

**Files:** `canonical/datasets/engagement/course_role_activity.yaml`, `semantic_layer/pii.py`, `semantic_layer/validate.py`, `snowflake_client.py`, `tests/test_semantic_validate.py`

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b feat/dataset-course-role-activity origin/main
```

- [ ] **Step 2: Write the tests**

```diff
diff --git a/tests/test_semantic_validate.py b/tests/test_semantic_validate.py
index 88b619b..427a971 100644
--- a/tests/test_semantic_validate.py
+++ b/tests/test_semantic_validate.py
@@ -89,3 +89,22 @@ def test_matching_dimension_types_pass():
         {"name": "course_role", "column": "COURSE_ROLE", "type": "categorical"},
     ])
     assert validate_dataset(ok, catalog(ok), SNAPSHOT, PII) == []
+
+
+def _aggregating(select: str):
+    return dataset(
+        base_sql=f"SELECT pc.COURSE_ID, {select} FROM {{{{ database }}}}.CDM_LMS.PERSON_COURSE pc "
+                 "JOIN {{ database }}.CDM_LMS.PERSON p ON p.ID = pc.PERSON_ID GROUP BY pc.COURSE_ID",
+        entities=[], dimensions=[], measures=[{"name": "n", "agg": "count", "expr": "COURSE_ID"}],
+        filters=[], pii_columns=[],
+    )
+
+
+def test_counts_of_pii_columns_are_not_pii():
+    ok = _aggregating("COUNT(DISTINCT pc.PERSON_ID) AS PEOPLE, COUNT(DISTINCT p.EMAIL) AS EMAILS")
+    assert validate_dataset(ok, catalog(ok), SNAPSHOT, PII) == []
+
+
+def test_value_returning_aggregates_of_pii_stay_pii():
+    bad = _aggregating("MIN(p.BIRTH_DATE) AS EARLIEST_BIRTH")
+    assert any("EARLIEST_BIRTH" in e for e in validate_dataset(bad, catalog(bad), SNAPSHOT, PII))
```

- [ ] **Step 3: Run them to verify they fail**

Run: `$PY -m pytest -q tests/test_semantic_validate.py`
Expected: `test_counts_of_pii_columns_are_not_pii` fails.

- [ ] **Step 4: Implement**

```diff
diff --git a/canonical/datasets/engagement/course_role_activity.yaml b/canonical/datasets/engagement/course_role_activity.yaml
new file mode 100644
index 0000000..61e4892
--- /dev/null
+++ b/canonical/datasets/engagement/course_role_activity.yaml
@@ -0,0 +1,124 @@
+id: dataset.course_role_activity.v1
+display_name: Course activity by role
+description: >
+  Per course and role (students S, instructors I): enrollments, people with course activity in the
+  course window, minutes and distinct days of activity, last login and last submission, and
+  Collaborate usage in sessions attended by both students and instructors. Preview and bbsupport
+  test users are excluded. Counts are per course; summing across courses counts role enrollments,
+  not distinct people.
+grain: one row per course x course role (S, I)
+domain: engagement
+source: bbdata-bbd-analytics procedures/orchestration/courseRoleActivity/processCourseRoleActivity.ts
+depends_on: [dataset.course_filters.v1]
+base_sql: |
+  WITH course_use AS (
+      SELECT DISTINCT COURSE_ID, START_WEEK, END_WEEK, COURSE_CREATION_DATE, COURSE_WEEKS, COURSE_DURATION, TERM_NAME
+      FROM {{ ref('dataset.course_filters.v1') }}
+  ),
+  course_per_role AS (
+      SELECT pc.COURSE_ID, pc.COURSE_ROLE,
+             ANY_VALUE(cu.COURSE_WEEKS) AS COURSE_WEEKS,
+             ANY_VALUE(cu.TERM_NAME) AS TERM_NAME,
+             ANY_VALUE(cu.COURSE_CREATION_DATE) AS COURSE_CREATION_DATE,
+             ANY_VALUE(cu.START_WEEK) AS START_WEEK,
+             ANY_VALUE(cu.END_WEEK) AS END_WEEK,
+             ANY_VALUE(cu.COURSE_DURATION) AS COURSE_DURATION,
+             MAX(ca.FIRST_ACCESSED_TIME) AS MAX_LOGIN_ACCESSED_TIME,
+             MAX(sub.MAX_SUBMITTED_TIME) AS MAX_SUBMITTED_TIME,
+             COUNT(DISTINCT ca.PERSON_ID) AS ACTIVE_ROLE_CNT,
+             COUNT(DISTINCT pc.PERSON_ID) AS ENROLLMENT_ROLE_COUNT,
+             SUM(NVL(ca.DURATION_SUM, 0)) / 60 AS TOTAL_MIN_IN_COURSE,
+             COUNT(DISTINCT DATE_TRUNC('DAY', ca.FIRST_ACCESSED_TIME)) AS CNT_DAYS,
+             COUNT(DISTINCT CASE WHEN pc.AVAILABLE_IND = FALSE OR pc.ENABLED_IND = FALSE OR pc.ROW_DELETED_TIME IS NOT NULL
+                                 THEN NULL ELSE ca.PERSON_ID END) AS AVAILABLE_ACTIVE_ROLE_CNT,
+             COUNT(DISTINCT CASE WHEN pc.AVAILABLE_IND = FALSE OR pc.ENABLED_IND = FALSE OR pc.ROW_DELETED_TIME IS NOT NULL
+                                 THEN NULL ELSE pc.PERSON_ID END) AS AVAILABLE_ENROLLMENT_ROLE_COUNT
+      FROM {{ database }}.CDM_LMS.PERSON_COURSE pc
+      JOIN {{ database }}.CDM_LMS.PERSON pe ON pe.ID = pc.PERSON_ID
+      JOIN course_use cu ON cu.COURSE_ID = pc.COURSE_ID
+      LEFT JOIN {{ database }}.CDM_LMS.COURSE_ACTIVITY ca
+        ON pc.ID = ca.PERSON_COURSE_ID
+       AND ca.FIRST_ACCESSED_TIME BETWEEN cu.START_WEEK AND (cu.END_WEEK + 7)
+      LEFT JOIN (
+          SELECT PERSON_COURSE_ID, MAX(SUBMITTED_TIME) AS MAX_SUBMITTED_TIME
+          FROM {{ database }}.CDM_LMS.SUBMISSION
+          GROUP BY PERSON_COURSE_ID
+      ) sub ON sub.PERSON_COURSE_ID = pc.ID
+      WHERE pc.COURSE_ROLE IN ('S', 'I')
+        AND pe.LAST_NAME NOT LIKE '%PreviewUser'
+        AND pe.LAST_NAME NOT LIKE 'bbsupport%'
+      GROUP BY pc.COURSE_ID, pc.COURSE_ROLE
+  ),
+  clb_sessions AS (
+      SELECT att.SESSION_ID, mp.LMS_PERSON_ID, se.ATTENDED_DURATION, cr.LMS_COURSE_ID
+      FROM {{ database }}.CDM_CLB.ATTENDANCE att
+      JOIN {{ database }}.CDM_CLB.SESSION se ON se.ID = att.SESSION_ID
+      JOIN {{ database }}.CDM_MAP.COURSE_ROOM cr ON cr.CLB_ROOM_ID = se.ROOM_ID
+      JOIN {{ database }}.CDM_MAP.PERSON mp ON mp.CLB_PERSON_ID = att.PERSON_ID
+  ),
+  sessions_used AS (
+      SELECT se.SESSION_ID
+      FROM clb_sessions se
+      JOIN {{ database }}.CDM_LMS.PERSON_COURSE pc
+        ON pc.PERSON_ID = se.LMS_PERSON_ID AND pc.COURSE_ID = se.LMS_COURSE_ID AND pc.COURSE_ROLE IN ('S', 'I')
+      GROUP BY se.SESSION_ID
+      HAVING COUNT(DISTINCT pc.COURSE_ROLE) > 1
+  ),
+  clb_use AS (
+      SELECT pc.COURSE_ID, pc.COURSE_ROLE, COUNT(DISTINCT pc.ID) AS CNT_CLB
+      FROM clb_sessions se
+      JOIN sessions_used seu ON seu.SESSION_ID = se.SESSION_ID
+      JOIN {{ database }}.CDM_LMS.PERSON_COURSE pc
+        ON pc.PERSON_ID = se.LMS_PERSON_ID AND pc.COURSE_ID = se.LMS_COURSE_ID
+      GROUP BY pc.COURSE_ID, pc.COURSE_ROLE
+  ),
+  clb_minutes AS (
+      SELECT cr.LMS_COURSE_ID AS COURSE_ID,
+             CEIL(SUM(se.ATTENDED_DURATION) / 60, 0) AS TOTAL_SESSION_MIN,
+             COUNT(se.ID) AS SESSION_CNT
+      FROM {{ database }}.CDM_CLB.SESSION se
+      JOIN sessions_used seu ON seu.SESSION_ID = se.ID
+      JOIN {{ database }}.CDM_MAP.COURSE_ROOM cr ON cr.CLB_ROOM_ID = se.ROOM_ID
+      WHERE cr.LMS_COURSE_ID IS NOT NULL AND cr.CLB_ROOM_ID IS NOT NULL
+      GROUP BY cr.LMS_COURSE_ID
+  )
+  SELECT
+      cr.COURSE_ID, cr.COURSE_ROLE, cr.COURSE_WEEKS, cr.TERM_NAME, cr.COURSE_CREATION_DATE,
+      cr.START_WEEK, cr.END_WEEK, cr.COURSE_DURATION,
+      cr.ACTIVE_ROLE_CNT, cr.ENROLLMENT_ROLE_COUNT, cr.TOTAL_MIN_IN_COURSE, cr.CNT_DAYS,
+      NVL(clm.TOTAL_SESSION_MIN, 0) AS CLB_SESSION_MIN,
+      NVL(clm.SESSION_CNT, 0) AS CLB_SESSION_CNT,
+      NVL(clb.CNT_CLB, 0) AS CLB_COUNT_PERSON,
+      cr.MAX_LOGIN_ACCESSED_TIME, cr.MAX_SUBMITTED_TIME,
+      cr.AVAILABLE_ACTIVE_ROLE_CNT, cr.AVAILABLE_ENROLLMENT_ROLE_COUNT
+  FROM course_per_role cr
+  LEFT JOIN clb_use clb ON cr.COURSE_ID = clb.COURSE_ID AND cr.COURSE_ROLE = clb.COURSE_ROLE
+  LEFT JOIN clb_minutes clm ON cr.COURSE_ID = clm.COURSE_ID
+entities:
+  - {name: course, column: COURSE_ID, type: foreign}
+dimensions:
+  - {name: course_role, column: COURSE_ROLE, type: categorical, description: "S = students, I = instructors", synonyms: [role]}
+  - {name: term_name, column: TERM_NAME, type: categorical, synonyms: [term, semester]}
+  - {name: course_duration, column: COURSE_DURATION, type: categorical}
+  - {name: course_weeks, column: COURSE_WEEKS, type: numeric}
+  - {name: course_start_week, column: START_WEEK, type: time, grains: [week, month, quarter, year]}
+  - {name: course_creation_date, column: COURSE_CREATION_DATE, type: time, grains: [day, week, month, quarter, year]}
+  - {name: last_login, column: MAX_LOGIN_ACCESSED_TIME, type: time, grains: [day, week, month]}
+  - {name: last_submission, column: MAX_SUBMITTED_TIME, type: time, grains: [day, week, month]}
+measures:
+  - {name: courses, agg: count_distinct, expr: COURSE_ID, unit: courses}
+  - {name: active_people, agg: sum, expr: ACTIVE_ROLE_CNT, unit: role enrollments, description: "People with in-window course activity, summed over courses"}
+  - {name: enrolled_people, agg: sum, expr: ENROLLMENT_ROLE_COUNT, unit: role enrollments}
+  - {name: available_active_people, agg: sum, expr: AVAILABLE_ACTIVE_ROLE_CNT, unit: role enrollments}
+  - {name: available_enrolled_people, agg: sum, expr: AVAILABLE_ENROLLMENT_ROLE_COUNT, unit: role enrollments}
+  - {name: minutes_in_course, agg: sum, expr: TOTAL_MIN_IN_COURSE, unit: minutes}
+  - {name: activity_days, agg: sum, expr: CNT_DAYS, unit: days, description: "Distinct days with activity per course, summed"}
+  - {name: collab_minutes, agg: sum, expr: CLB_SESSION_MIN, unit: minutes}
+  - {name: collab_sessions, agg: sum, expr: CLB_SESSION_CNT, unit: sessions}
+  - {name: collab_people, agg: sum, expr: CLB_COUNT_PERSON, unit: role enrollments}
+  - {name: share_active, agg: ratio, numerator: active_people, denominator: enrolled_people, unit: ratio, synonyms: [engagement rate]}
+  - {name: minutes_per_active_person, agg: ratio, numerator: minutes_in_course, denominator: active_people, unit: minutes}
+  - {name: courses_with_activity, agg: count_distinct, expr: "IFF(ACTIVE_ROLE_CNT > 0, COURSE_ID, NULL)", unit: courses}
+filters:
+  - {name: students, sql: "COURSE_ROLE = 'S'"}
+  - {name: instructors, sql: "COURSE_ROLE = 'I'"}
diff --git a/semantic_layer/pii.py b/semantic_layer/pii.py
index 759407c..396ead3 100644
--- a/semantic_layer/pii.py
+++ b/semantic_layer/pii.py
@@ -1,7 +1,25 @@
-"""Column names treated as personally identifiable wherever they appear."""
+"""PII column names, and the aggregates that reduce PII columns to counts."""
+
+import sqlglot.expressions as exp
 
 PII_COLUMN_NAMES = frozenset({
     "FIRST_NAME", "LAST_NAME", "EMAIL", "SSN", "PHONE", "ADDRESS",
     "DOB", "DATE_OF_BIRTH", "PASSWORD", "PASSWD", "PHONE_NUMBER",
     "STREET_ADDRESS", "ZIP_CODE", "ZIPCODE",
 })
+
+# Aggregates whose result is a count, never a value from the column.
+COUNTING_AGGREGATES = (exp.Count, exp.CountIf, exp.ApproxDistinct, exp.Hll)
+
+
+def inside_counting_aggregate(node: exp.Expression, select_expression: exp.Expression) -> bool:
+    """True when the nearest aggregate above node, within select_expression, is an unwindowed count."""
+    while node is not None:
+        if isinstance(node, exp.Window):
+            return False
+        if isinstance(node, COUNTING_AGGREGATES):
+            return not isinstance(node.parent, exp.Window)
+        if isinstance(node, exp.AggFunc) or node is select_expression:
+            return False
+        node = node.parent
+    return False
diff --git a/semantic_layer/validate.py b/semantic_layer/validate.py
index a775d56..2d52518 100644
--- a/semantic_layer/validate.py
+++ b/semantic_layer/validate.py
@@ -19,7 +19,7 @@ from sqlglot.optimizer.qualify import qualify
 from sqlglot.schema import MappingSchema
 
 from .compiler import CompileError, build_ctes
-from .pii import PII_COLUMN_NAMES
+from .pii import PII_COLUMN_NAMES, inside_counting_aggregate
 from .schema import Catalog, Dataset, SemanticMetric
 
 _FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
@@ -85,18 +85,21 @@ def _dimension_type_errors(ds: Dataset, catalog: Catalog, snapshot: dict) -> lis
 
 
 def _traced_pii(ds: Dataset, catalog: Catalog, snapshot: dict, outputs: set[str], pii: frozenset[str]) -> set[str]:
-    """Outputs whose lineage reaches a PII-flagged source column."""
+    """Outputs whose lineage reaches a PII-flagged source column other than through a count."""
     sql = _dataset_sql(ds, catalog)
-    found = set()
-    for col in outputs:
-        for node in lineage(col, sql, schema=snapshot, dialect="snowflake").walk():
-            if node.downstream or not isinstance(node.source, exp.Table):
-                continue
-            source = f"{node.source.db}.{node.source.name}.{node.name.split('.')[-1]}".upper()
-            if source in pii:
-                found.add(col)
-                break
-    return found
+
+    def reaches_pii(node) -> bool:
+        expr = node.expression
+        columns = list(expr.find_all(exp.Column))
+        if columns and all(inside_counting_aggregate(c, expr) for c in columns):
+            return False
+        if not node.downstream:
+            if not isinstance(node.source, exp.Table):
+                return False
+            return f"{node.source.db}.{node.source.name}.{node.name.split('.')[-1]}".upper() in pii
+        return any(reaches_pii(d) for d in node.downstream)
+
+    return {col for col in outputs if reaches_pii(lineage(col, sql, schema=snapshot, dialect="snowflake"))}
 
 
 def validate_dataset(ds: Dataset, catalog: Catalog, snapshot: dict, pii: frozenset[str]) -> list[str]:
diff --git a/snowflake_client.py b/snowflake_client.py
index bed9871..ae0604d 100644
--- a/snowflake_client.py
+++ b/snowflake_client.py
@@ -14,9 +14,8 @@ import logging
 import os
 
 import boto3
-import sqlglot.expressions as exp
 
-from semantic_layer.pii import PII_COLUMN_NAMES
+from semantic_layer.pii import PII_COLUMN_NAMES, inside_counting_aggregate
 
 logger = logging.getLogger("API-PROXY")
 
@@ -100,22 +99,6 @@ def query_preview(schema: str, table: str, limit: int = 20) -> dict:
         cursor.close()
 
 
-_COUNTING_AGGREGATES = (exp.Count, exp.CountIf, exp.ApproxDistinct, exp.Hll)
-
-
-def _inside_counting_aggregate(node, select_expression) -> bool:
-    """True when the nearest aggregate above node, within select_expression, is an unwindowed count."""
-    while node is not None:
-        if isinstance(node, exp.Window):
-            return False
-        if isinstance(node, _COUNTING_AGGREGATES):
-            return not isinstance(node.parent, exp.Window)
-        if isinstance(node, exp.AggFunc) or node is select_expression:
-            return False
-        node = node.parent
-    return False
-
-
 def validate_and_execute(sql: str, params: dict | None = None) -> dict:
     """Validate a SQL statement with sqlglot AST checks, then execute it.
 
@@ -210,7 +193,7 @@ def validate_and_execute(sql: str, params: dict | None = None) -> dict:
         for sel in outer_select.expressions:
             for col_node in sel.find_all(exp.Column):
                 col_name = col_node.name.upper().strip('"').strip("'")
-                if col_name in PII_COLUMN_NAMES and not _inside_counting_aggregate(col_node, sel):
+                if col_name in PII_COLUMN_NAMES and not inside_counting_aggregate(col_node, sel):
                     return {
                         "error": (
                             f"Column '{col_name}' contains personally identifiable information (PII). "
```

- [ ] **Step 5: Verify**

Run: `$PY -m pytest -q tests`
Expected: all pass.

Live run recorded during planning: chained on course_filters; every dimension (8) and measure (13): 5 rows, 21 columns, 10.1 s.

- [ ] **Step 6: Commit, open the PR, merge** (PR body: the summary above plus the live-run line).

### Task 7 (PR 4g): dataset course_student_activity (bbd-analytics COURSE_STUDENT_ACTIVITY)

**Files:** `canonical/datasets/engagement/course_student_activity.yaml`

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b feat/dataset-course-student-activity origin/main
```

- [ ] **Step 2: Implement**

```diff
diff --git a/canonical/datasets/engagement/course_student_activity.yaml b/canonical/datasets/engagement/course_student_activity.yaml
new file mode 100644
index 0000000..f70ff13
--- /dev/null
+++ b/canonical/datasets/engagement/course_student_activity.yaml
@@ -0,0 +1,129 @@
+id: dataset.course_student_activity.v1
+display_name: Student course activity
+description: >
+  Per student enrollment with activity in the course window: active days, interactions,
+  submissions, course accesses, minutes in the course, first and last access, share of course
+  days with activity, Collaborate minutes and sessions, and content items reviewed and completed
+  against the course's available content items.
+grain: one row per student enrollment (person_course) with in-window activity
+domain: engagement
+source: bbdata-bbd-analytics snowflake/migrations/bbd_analytics/repeatable/R__TFV_COURSE_STUDENT_ACTIVITY.sql
+depends_on: [dataset.course_filters.v1]
+base_sql: |
+  WITH course_per_student AS (
+      SELECT pc.ID AS PERSON_COURSE_ID,
+             ANY_VALUE(pc.COURSE_ID) AS COURSE_ID,
+             ANY_VALUE(pc.PERSON_ID) AS PERSON_ID,
+             ANY_VALUE(pe.FIRST_NAME) AS PERSON_FIRST_NAME,
+             ANY_VALUE(pe.LAST_NAME) AS PERSON_LAST_NAME,
+             ANY_VALUE(pe.EMAIL) AS PERSON_EMAIL,
+             ANY_VALUE(COALESCE(pe.STAGE:student_id, pe.STAGE:user_id, pe.STAGE:batch_uid, pe.SOURCE_ID)::STRING) AS ALTERNATIVE_SOURCE_ID,
+             ANY_VALUE(cu.COURSE_WEEKS) AS COURSE_WEEKS,
+             ANY_VALUE(cu.COURSE_DURATION) AS COURSE_DURATION,
+             ANY_VALUE(cu.TERM_NAME) AS TERM_NAME,
+             COUNT(DISTINCT DATE_TRUNC('DAY', ca.FIRST_ACCESSED_TIME)) AS CNT_DAYS,
+             SUM(ca.INTERACTION_CNT) AS INTERACTION_COUNT,
+             ANY_VALUE(sub.SUBMISSION_COUNT) AS SUBMISSION_COUNT,
+             COUNT(*) AS ACCESSES_COUNT,
+             MIN(ca.FIRST_ACCESSED_TIME) AS FIRST_ACCESSED_TIME,
+             MAX(ca.LAST_ACCESSED_TIME) AS LAST_ACCESSED_TIME,
+             MAX(ca.FIRST_ACCESSED_TIME) AS LAST_INITIAL_ACCESSED_TIME,
+             SUM(COALESCE(ca.DURATION_SUM, 0)) / 60 AS TOTAL_MIN_IN_COURSE,
+             ANY_VALUE(CASE WHEN pc.AVAILABLE_IND = FALSE OR pc.ENABLED_IND = FALSE OR pc.ROW_DELETED_TIME IS NOT NULL
+                            THEN 0 ELSE 1 END) AS ACTIVE,
+             COUNT(DISTINCT DATE_TRUNC('DAY', ca.FIRST_ACCESSED_TIME)) / NULLIF(ANY_VALUE(cu.COURSE_WEEKS) * 7, 0) AS PERCENT_ACTIVITY
+      FROM {{ database }}.CDM_LMS.PERSON_COURSE pc
+      JOIN {{ database }}.CDM_LMS.PERSON pe ON pe.ID = pc.PERSON_ID
+      JOIN (
+          SELECT DISTINCT COURSE_ID, START_WEEK, END_WEEK, COURSE_WEEKS, COURSE_DURATION, TERM_NAME
+          FROM {{ ref('dataset.course_filters.v1') }}
+      ) cu ON cu.COURSE_ID = pc.COURSE_ID
+      JOIN {{ database }}.CDM_LMS.COURSE_ACTIVITY ca
+        ON pc.ID = ca.PERSON_COURSE_ID
+       AND ca.FIRST_ACCESSED_TIME BETWEEN cu.START_WEEK AND (cu.END_WEEK + 7)
+      LEFT JOIN (
+          SELECT PERSON_COURSE_ID, COUNT(1) AS SUBMISSION_COUNT
+          FROM {{ database }}.CDM_LMS.SUBMISSION
+          GROUP BY PERSON_COURSE_ID
+      ) sub ON sub.PERSON_COURSE_ID = pc.ID
+      WHERE pc.STUDENT_IND = TRUE
+      GROUP BY pc.ID
+  ),
+  clb_use AS (
+      SELECT CEIL(SUM(att.DURATION) / 60, 0) AS TOTAL_SESSION_MIN,
+             cr.LMS_COURSE_ID AS COURSE_ID,
+             pc.ID AS PERSON_COURSE_ID,
+             COUNT(DISTINCT att.SESSION_ID) AS INTERACTION_CNT
+      FROM {{ database }}.CDM_CLB.ATTENDANCE att
+      JOIN {{ database }}.CDM_CLB.SESSION se ON se.ID = att.SESSION_ID
+      JOIN {{ database }}.CDM_MAP.COURSE_ROOM cr ON cr.CLB_ROOM_ID = se.ROOM_ID
+      JOIN {{ database }}.CDM_MAP.PERSON mp ON mp.CLB_PERSON_ID = att.PERSON_ID
+      JOIN {{ database }}.CDM_LMS.PERSON_COURSE pc
+        ON pc.PERSON_ID = mp.LMS_PERSON_ID AND pc.COURSE_ID = cr.LMS_COURSE_ID AND pc.COURSE_ROLE = 'S'
+      WHERE cr.LMS_COURSE_ID IS NOT NULL AND cr.CLB_ROOM_ID IS NOT NULL
+      GROUP BY cr.LMS_COURSE_ID, pc.ID
+  ),
+  course_progress AS (
+      SELECT ccr.PERSON_COURSE_ID,
+             COUNT(ccr.ID) AS PERSON_TOTAL_CONTENT_ITEMS_CNT,
+             COUNT(CASE WHEN ccr.REVIEWED_STATE = 'S' THEN ccr.ID END) AS PERSON_REVIEWED_CONTENT_ITEMS_CNT,
+             COUNT(CASE WHEN ccr.REVIEWED_STATE = 'C' THEN ccr.ID END) AS PERSON_COMPLETED_CONTENT_ITEMS_CNT
+      FROM {{ database }}.CDM_LMS.COURSE_CONTENT_ITEMS_REVIEWED ccr
+      JOIN {{ database }}.CDM_LMS.COURSE_ITEM ci
+        ON ccr.COURSE_ITEM_ID = ci.ID AND ci.ROW_DELETED_TIME IS NULL AND NVL(ci.AVAILABLE_IND, FALSE)
+      WHERE ccr.PERSON_COURSE_ID IS NOT NULL AND ci.ITEM_TYPE <> 'ITEM'
+      GROUP BY ccr.PERSON_COURSE_ID
+  ),
+  course_content_items AS (
+      SELECT ci.COURSE_ID, COUNT(*) AS TOTAL_COURSE_CONTENT_ITEM_CNT
+      FROM {{ database }}.CDM_LMS.COURSE_ITEM ci
+      WHERE ci.ITEM_TYPE <> 'ITEM' AND ci.ROW_DELETED_TIME IS NULL AND NVL(ci.AVAILABLE_IND, FALSE)
+      GROUP BY ci.COURSE_ID
+  )
+  SELECT
+      cr.COURSE_ID, cr.PERSON_FIRST_NAME, cr.PERSON_LAST_NAME, cr.ALTERNATIVE_SOURCE_ID,
+      cr.INTERACTION_COUNT, cr.SUBMISSION_COUNT, cr.PERSON_EMAIL, cr.PERSON_COURSE_ID, cr.PERSON_ID,
+      cr.COURSE_WEEKS, cr.COURSE_DURATION, cr.TERM_NAME, cr.TOTAL_MIN_IN_COURSE, cr.CNT_DAYS, cr.ACCESSES_COUNT,
+      cr.FIRST_ACCESSED_TIME, cr.LAST_ACCESSED_TIME, cr.LAST_INITIAL_ACCESSED_TIME, cr.ACTIVE,
+      COALESCE(cr.PERCENT_ACTIVITY, 0) AS PERCENT_ACTIVITY,
+      COALESCE(clb.TOTAL_SESSION_MIN, 0) AS CLB_SESSION_MIN,
+      COALESCE(clb.INTERACTION_CNT, 0) AS CLB_SESSION_INTERACTION_CNT,
+      COALESCE(cp.PERSON_TOTAL_CONTENT_ITEMS_CNT, 0) AS PERSON_TOTAL_CONTENT_ITEMS_CNT,
+      COALESCE(cp.PERSON_REVIEWED_CONTENT_ITEMS_CNT, 0) AS PERSON_REVIEWED_CONTENT_ITEMS_CNT,
+      COALESCE(cp.PERSON_COMPLETED_CONTENT_ITEMS_CNT, 0) AS PERSON_COMPLETED_CONTENT_ITEMS_CNT,
+      COALESCE(cci.TOTAL_COURSE_CONTENT_ITEM_CNT, 0) AS TOTAL_COURSE_CONTENT_ITEM_CNT
+  FROM course_per_student cr
+  LEFT JOIN clb_use clb ON cr.COURSE_ID = clb.COURSE_ID AND cr.PERSON_COURSE_ID = clb.PERSON_COURSE_ID
+  LEFT JOIN course_progress cp ON cr.PERSON_COURSE_ID = cp.PERSON_COURSE_ID
+  LEFT JOIN course_content_items cci ON cr.COURSE_ID = cci.COURSE_ID
+entities:
+  - {name: person_course, column: PERSON_COURSE_ID, type: primary}
+  - {name: course, column: COURSE_ID, type: foreign}
+  - {name: person, column: PERSON_ID, type: foreign}
+dimensions:
+  - {name: term_name, column: TERM_NAME, type: categorical, synonyms: [term, semester]}
+  - {name: course_duration, column: COURSE_DURATION, type: categorical}
+  - {name: course_weeks, column: COURSE_WEEKS, type: numeric}
+  - {name: active, column: ACTIVE, type: numeric, description: "1 when the enrollment is available, enabled and not deleted"}
+  - {name: first_access, column: FIRST_ACCESSED_TIME, type: time, grains: [day, week, month]}
+  - {name: last_access, column: LAST_ACCESSED_TIME, type: time, grains: [day, week, month], synonyms: [last seen]}
+  - {name: person_email, column: PERSON_EMAIL, type: categorical, description: "Filter only; never selectable"}
+measures:
+  - {name: students, agg: count_distinct, expr: PERSON_ID, unit: students}
+  - {name: enrollments, agg: count_distinct, expr: PERSON_COURSE_ID, unit: enrollments}
+  - {name: minutes_in_course, agg: sum, expr: TOTAL_MIN_IN_COURSE, unit: minutes, synonyms: [time in course]}
+  - {name: avg_minutes_per_enrollment, agg: avg, expr: TOTAL_MIN_IN_COURSE, unit: minutes}
+  - {name: interactions, agg: sum, expr: INTERACTION_COUNT, unit: interactions}
+  - {name: submissions, agg: sum, expr: SUBMISSION_COUNT, unit: submissions}
+  - {name: course_accesses, agg: sum, expr: ACCESSES_COUNT, unit: accesses}
+  - {name: avg_active_days, agg: avg, expr: CNT_DAYS, unit: days}
+  - {name: avg_percent_activity, agg: avg, expr: PERCENT_ACTIVITY, unit: ratio, description: "Average share of course days with activity"}
+  - {name: collab_minutes, agg: sum, expr: CLB_SESSION_MIN, unit: minutes}
+  - {name: collab_sessions_attended, agg: sum, expr: CLB_SESSION_INTERACTION_CNT, unit: sessions}
+  - {name: content_items_reviewed, agg: sum, expr: PERSON_REVIEWED_CONTENT_ITEMS_CNT, unit: items}
+  - {name: content_items_completed, agg: sum, expr: PERSON_COMPLETED_CONTENT_ITEMS_CNT, unit: items}
+  - {name: course_content_items, agg: sum, expr: TOTAL_COURSE_CONTENT_ITEM_CNT, unit: items, description: "Available content items in each student's course, summed per student"}
+  - {name: content_completion_rate, agg: ratio, numerator: content_items_completed, denominator: course_content_items, unit: ratio}
+filters:
+  - {name: active_only, sql: "ACTIVE = 1"}
+pii_columns: [PERSON_ID, PERSON_COURSE_ID, ALTERNATIVE_SOURCE_ID, PERSON_FIRST_NAME, PERSON_LAST_NAME, PERSON_EMAIL]
```

- [ ] **Step 3: Verify**

Run: `$PY -m pytest -q tests`
Expected: all pass.

Live run recorded during planning: chained on course_filters; every non-PII dimension (6) and measure (15): 5 rows, 21 columns, 8.4 s.

- [ ] **Step 4: Commit, open the PR, merge** (PR body: the summary above plus the live-run line).

### Task 8 (PR 4h): dataset student_grade; live-schema supplement for the dictionary snapshot

**Files:** `canonical/datasets/grades/student_grade.yaml`, `semantic_layer/validate.py`, `tests/fixtures/cdm_dictionary_supplement.json`, `tests/test_semantic_validate.py`

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b feat/dataset-student-grade origin/main
```

- [ ] **Step 2: Write the tests**

```diff
diff --git a/tests/test_semantic_validate.py b/tests/test_semantic_validate.py
index 427a971..e29009d 100644
--- a/tests/test_semantic_validate.py
+++ b/tests/test_semantic_validate.py
@@ -108,3 +108,9 @@ def test_counts_of_pii_columns_are_not_pii():
 def test_value_returning_aggregates_of_pii_stay_pii():
     bad = _aggregating("MIN(p.BIRTH_DATE) AS EARLIEST_BIRTH")
     assert any("EARLIEST_BIRTH" in e for e in validate_dataset(bad, catalog(bad), SNAPSHOT, PII))
+
+
+def test_snapshot_includes_tables_from_the_live_schema_supplement():
+    meta = SNAPSHOT["VALIDATION_DB"]["CDM_META"]
+    assert meta["BBD_CALCULATION_DETAIL"]["CALCULATION_LIST"] == "ARRAY"
+    assert "CANON_DEFINITION" in meta
```

- [ ] **Step 3: Run them to verify they fail**

Run: `$PY -m pytest -q tests/test_semantic_validate.py`
Expected: `test_snapshot_includes_tables_from_the_live_schema_supplement` fails.

- [ ] **Step 4: Implement**

```diff
diff --git a/canonical/datasets/grades/student_grade.yaml b/canonical/datasets/grades/student_grade.yaml
new file mode 100644
index 0000000..46a17ae
--- /dev/null
+++ b/canonical/datasets/grades/student_grade.yaml
@@ -0,0 +1,101 @@
+id: dataset.student_grade.v1
+display_name: Student course grades
+description: >
+  One row per student enrollment in a reportable course: the final-grade column's score, the
+  summed score over the other gradebook items whose type is listed in CDM_META.BBD_CALCULATION_DETAIL,
+  normalized-score statistics, and grade_percentage = the greater of the two as a percentage,
+  bucketed into 5-point bands. As in bbd-analytics, an enrollment with no possible points gets
+  grade_percentage 0 (band '0-04%'); the grade measures below count graded enrollments only.
+grain: one row per student enrollment (person_course) in a reportable course
+domain: grades
+source: bbdata-bbd-analytics snowflake/migrations/bbd_analytics/repeatable/R__TFV_STUDENT_GRADE.sql
+depends_on: [dataset.course_filters.v1]
+base_sql: |
+  WITH active_persons AS (
+      SELECT DISTINCT pc.COURSE_ID, pc.PERSON_ID, pc.ID AS PERSON_COURSE_ID,
+             pc.AVAILABLE_IND AS PERSON_COURSE_AVAILABLE, pc.ENABLED_IND AS PERSON_COURSE_ENABLED,
+             pc.ROW_DELETED_TIME AS PERSON_COURSE_DELETED
+      FROM {{ database }}.CDM_LMS.PERSON_COURSE pc
+      JOIN {{ ref('dataset.course_filters.v1') }} fv ON fv.COURSE_ID = pc.COURSE_ID AND fv.COURSE_ROLE = 'S'
+      JOIN {{ database }}.CDM_LMS.PERSON pe ON pe.ID = pc.PERSON_ID
+      WHERE pc.STUDENT_IND = TRUE
+  ),
+  bucket_name AS (
+      SELECT BUCKET, BUCKET_DESC
+      FROM (VALUES (0, '<0%'), (1, '0-04%'), (2, '05-09%'), (3, '10-14%'), (4, '15-19%'), (5, '20-24%'),
+                   (6, '25-29%'), (7, '30-34%'), (8, '35-39%'), (9, '40-44%'), (10, '45-49%'), (11, '50-54%'),
+                   (12, '55-59%'), (13, '60-64%'), (14, '65-69%'), (15, '70-74%'), (16, '75-79%'), (17, '80-84%'),
+                   (18, '85-89%'), (19, '90-94%'), (20, '95-100%'), (21, '>100%')) AS v (BUCKET, BUCKET_DESC)
+  ),
+  final_grades AS (
+      SELECT gr.PERSON_COURSE_ID, gr.SCORE AS FINAL_SCORE, gr.POSSIBLE_SCORE AS FINAL_POSSIBLE_SCORE,
+             gr.NORMALIZED_SCORE AS FINAL_NORMALIZED_SCORE
+      FROM {{ database }}.CDM_LMS.GRADE gr
+      JOIN {{ database }}.CDM_LMS.GRADEBOOK gb ON gb.ID = gr.GRADEBOOK_ID
+      JOIN {{ database }}.CDM_LMS.PERSON_COURSE pc ON gr.PERSON_COURSE_ID = pc.ID AND pc.COURSE_ID = gb.COURSE_ID
+      WHERE gr.ROW_DELETED_TIME IS NULL AND gb.FINAL_GRADE_IND = TRUE
+  ),
+  counted_grades_detail AS (
+      SELECT gr.PERSON_COURSE_ID, gr.SCORE, gr.POSSIBLE_SCORE, gr.NORMALIZED_SCORE,
+             IFF(gr.NORMALIZED_SCORE = 0, 1, NULL) AS ZERO_IND_NORMALIZED_SCORE,
+             IFF(gr.NORMALIZED_SCORE IS NULL, 1, NULL) AS NULL_IND_NORMALIZED_SCORE
+      FROM {{ database }}.CDM_LMS.GRADE gr
+      JOIN {{ database }}.CDM_LMS.GRADEBOOK gb ON gb.ID = gr.GRADEBOOK_ID
+      JOIN {{ database }}.CDM_LMS.COURSE_ITEM ci ON ci.ID = gb.COURSE_ITEM_ID
+      JOIN {{ database }}.CDM_LMS.PERSON_COURSE pc ON gr.PERSON_COURSE_ID = pc.ID AND pc.COURSE_ID = gb.COURSE_ID
+      JOIN {{ database }}.CDM_META.BBD_CALCULATION_DETAIL cd
+        ON cd.NAME = IFF(ci.ITEM_TYPE IN ('SCORM_ENGINE', 'CONTENT_PACKAGE_SCORM', 'SCORM'), 'SCORM PACKAGE', ci.ITEM_TYPE)
+      WHERE gr.ROW_DELETED_TIME IS NULL AND (gb.FINAL_GRADE_IND = FALSE OR gb.FINAL_GRADE_IND IS NULL)
+  ),
+  counted_grades AS (
+      SELECT PERSON_COURSE_ID,
+             SUM(SCORE) AS SUM_SCORE, SUM(POSSIBLE_SCORE) AS SUM_POSSIBLE_SCORE, COUNT(*) AS MAX_COUNT,
+             MAX(NORMALIZED_SCORE) AS MAX_NORMALIZED_SCORE, AVG(NORMALIZED_SCORE) AS AVG_NORMALIZED_SCORE,
+             SUM(ZERO_IND_NORMALIZED_SCORE) AS ZERO_NORMALIZED_SCORE, SUM(NULL_IND_NORMALIZED_SCORE) AS NULL_NORMALIZED_SCORE
+      FROM counted_grades_detail
+      GROUP BY PERSON_COURSE_ID
+  ),
+  student_bucket AS (
+      SELECT ap.COURSE_ID, ap.PERSON_ID, ap.PERSON_COURSE_ID,
+             ap.PERSON_COURSE_AVAILABLE, ap.PERSON_COURSE_ENABLED, ap.PERSON_COURSE_DELETED,
+             NVL(fi.FINAL_SCORE, 0) / IFF(NVL(fi.FINAL_POSSIBLE_SCORE, 0) = 0, 1, fi.FINAL_POSSIBLE_SCORE) AS FINAL_GRADE,
+             NVL(co.SUM_SCORE, 0) / IFF(NVL(co.SUM_POSSIBLE_SCORE, 0) = 0, 1, co.SUM_POSSIBLE_SCORE) AS SUM_GRADE,
+             GREATEST(NVL(fi.FINAL_SCORE, 0), NVL(co.SUM_SCORE, 0)) AS SCORE,
+             GREATEST(NVL(fi.FINAL_POSSIBLE_SCORE, 0), NVL(co.SUM_POSSIBLE_SCORE, 0)) AS POSSIBLE_SCORE,
+             NVL(fi.FINAL_SCORE, 0) AS FINAL_SCORE,
+             NVL(co.SUM_SCORE, 0) AS SUM_SCORE,
+             fi.FINAL_POSSIBLE_SCORE, co.SUM_POSSIBLE_SCORE, fi.FINAL_NORMALIZED_SCORE,
+             co.MAX_NORMALIZED_SCORE, co.AVG_NORMALIZED_SCORE, co.ZERO_NORMALIZED_SCORE, co.NULL_NORMALIZED_SCORE,
+             co.MAX_COUNT AS ENROLLMENT_ITEMS,
+             GREATEST(FINAL_GRADE, SUM_GRADE) * 100 AS GRADE_PERCENTAGE,
+             WIDTH_BUCKET(GRADE_PERCENTAGE, 0, 100, 20) AS BUCKET
+      FROM active_persons ap
+      LEFT JOIN final_grades fi ON fi.PERSON_COURSE_ID = ap.PERSON_COURSE_ID
+      LEFT JOIN counted_grades co ON co.PERSON_COURSE_ID = ap.PERSON_COURSE_ID
+  )
+  SELECT sb.*, bn.BUCKET_DESC
+  FROM student_bucket sb
+  LEFT JOIN bucket_name bn ON bn.BUCKET = sb.BUCKET
+entities:
+  - {name: person_course, column: PERSON_COURSE_ID, type: primary}
+  - {name: course, column: COURSE_ID, type: foreign}
+  - {name: person, column: PERSON_ID, type: foreign}
+dimensions:
+  - {name: grade_band, column: BUCKET_DESC, type: categorical, description: "5-point grade_percentage band, '<0%' to '>100%'", synonyms: [grade range, grade bucket]}
+  - {name: enrollment_available, column: PERSON_COURSE_AVAILABLE, type: boolean}
+  - {name: enrollment_enabled, column: PERSON_COURSE_ENABLED, type: boolean}
+  - {name: graded_items, column: ENROLLMENT_ITEMS, type: numeric, description: "Graded non-final items counted for the enrollment"}
+measures:
+  - {name: students, agg: count_distinct, expr: PERSON_ID, unit: students}
+  - {name: enrollments, agg: count_distinct, expr: PERSON_COURSE_ID, unit: enrollments}
+  - {name: graded_enrollments, agg: count_distinct, expr: "IFF(POSSIBLE_SCORE > 0, PERSON_COURSE_ID, NULL)", unit: enrollments}
+  - {name: average_grade_percentage, agg: avg, expr: "IFF(POSSIBLE_SCORE > 0, GRADE_PERCENTAGE, NULL)", unit: percent, synonyms: [average grade, mean grade, gpa]}
+  - {name: median_grade_percentage, agg: median, expr: "IFF(POSSIBLE_SCORE > 0, GRADE_PERCENTAGE, NULL)", unit: percent}
+  - {name: average_final_grade, agg: avg, expr: "IFF(FINAL_POSSIBLE_SCORE > 0, FINAL_GRADE * 100, NULL)", unit: percent}
+  - {name: average_normalized_score, agg: avg, expr: AVG_NORMALIZED_SCORE, unit: score}
+  - {name: failing_enrollments, agg: count_distinct, expr: "IFF(POSSIBLE_SCORE > 0 AND GRADE_PERCENTAGE < 60, PERSON_COURSE_ID, NULL)", unit: enrollments, description: "Graded enrollments below 60%"}
+  - {name: share_failing, agg: ratio, numerator: failing_enrollments, denominator: graded_enrollments, unit: ratio}
+filters:
+  - {name: graded_only, sql: "POSSIBLE_SCORE > 0", description: Enrollments with at least one possible point}
+  - {name: active_enrollments, sql: "PERSON_COURSE_AVAILABLE AND PERSON_COURSE_ENABLED AND PERSON_COURSE_DELETED IS NULL"}
+pii_columns: [PERSON_ID, PERSON_COURSE_ID]
diff --git a/semantic_layer/validate.py b/semantic_layer/validate.py
index 2d52518..2ab34fd 100644
--- a/semantic_layer/validate.py
+++ b/semantic_layer/validate.py
@@ -25,12 +25,20 @@ from .schema import Catalog, Dataset, SemanticMetric
 _FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
 DICTIONARY_SNAPSHOT = _FIXTURES / "cdm_dictionary.json"
 PII_SNAPSHOT = _FIXTURES / "cdm_pii_columns.json"
+SUPPLEMENT_SNAPSHOT = _FIXTURES / "cdm_dictionary_supplement.json"
 _DB = "VALIDATION_DB"
 
 
-def load_snapshot(path: Path = DICTIONARY_SNAPSHOT) -> dict:
-    """The snapshot as a sqlglot schema: {database: {schema: {table: {column: type}}}}."""
-    return {_DB: json.loads(path.read_text())}
+def load_snapshot(path: Path = DICTIONARY_SNAPSHOT, supplement: Path = SUPPLEMENT_SNAPSHOT) -> dict:
+    """The snapshot as a sqlglot schema: {database: {schema: {table: {column: type}}}}.
+
+    The supplement adds tables the dictionary export omits, taken from the live schema.
+    """
+    schemas = json.loads(path.read_text())
+    for schema, tables in json.loads(supplement.read_text()).items():
+        if not schema.startswith("_"):
+            schemas.setdefault(schema, {}).update(tables)
+    return {_DB: schemas}
 
 
 def load_pii_columns(path: Path = PII_SNAPSHOT) -> frozenset[str]:
diff --git a/tests/fixtures/cdm_dictionary_supplement.json b/tests/fixtures/cdm_dictionary_supplement.json
new file mode 100644
index 0000000..49c9540
--- /dev/null
+++ b/tests/fixtures/cdm_dictionary_supplement.json
@@ -0,0 +1,7 @@
+{
+ "_source": "illuminate-mcp describe_entity, 2026-10-08: tables the data-dictionary export omits",
+ "CDM_META": {
+  "BBD_CALCULATION_DEFINITION": {"DESCRIPTION": "TEXT", "NAME": "TEXT", "SOURCE_SCHEMA": "TEXT"},
+  "BBD_CALCULATION_DETAIL": {"CALCULATION_LIST": "ARRAY", "DISPLAY_NAME": "TEXT", "NAME": "TEXT"}
+ }
+}
```

- [ ] **Step 5: Verify**

Run: `$PY -m pytest -q tests`
Expected: all pass.

Live run recorded during planning: chained on course_filters; every non-PII dimension (4) and measure (9): 10 rows, 7.1 s. Data showed ungraded enrollments at grade_percentage 0 (as in bbd-analytics); grade measures are scoped to graded enrollments.

- [ ] **Step 6: Commit, open the PR, merge** (PR body: the summary above plus the live-run line).

### Task 9 (PR 4i): governed metrics over the core datasets

**Files:** `canonical/datasets/engagement/course_student_activity.yaml`, `canonical/metrics/course.yaml`, `canonical/metrics/engagement.yaml`, `canonical/metrics/enrollment.yaml`, `canonical/metrics/grades.yaml`

- [ ] **Step 1: Branch**

```bash
git fetch origin && git checkout -b feat/governed-metrics-core origin/main
```

- [ ] **Step 2: Implement**

```diff
diff --git a/canonical/datasets/engagement/course_student_activity.yaml b/canonical/datasets/engagement/course_student_activity.yaml
index f70ff13..341ec07 100644
--- a/canonical/datasets/engagement/course_student_activity.yaml
+++ b/canonical/datasets/engagement/course_student_activity.yaml
@@ -124,6 +124,8 @@ measures:
   - {name: content_items_completed, agg: sum, expr: PERSON_COMPLETED_CONTENT_ITEMS_CNT, unit: items}
   - {name: course_content_items, agg: sum, expr: TOTAL_COURSE_CONTENT_ITEM_CNT, unit: items, description: "Available content items in each student's course, summed per student"}
   - {name: content_completion_rate, agg: ratio, numerator: content_items_completed, denominator: course_content_items, unit: ratio}
+  - {name: students_active_last_7_days, agg: count_distinct, expr: "IFF(LAST_ACCESSED_TIME >= DATEADD(day, -7, CURRENT_DATE()), PERSON_ID, NULL)", unit: students}
+  - {name: share_active_last_7_days, agg: ratio, numerator: students_active_last_7_days, denominator: students, unit: ratio}
 filters:
   - {name: active_only, sql: "ACTIVE = 1"}
 pii_columns: [PERSON_ID, PERSON_COURSE_ID, ALTERNATIVE_SOURCE_ID, PERSON_FIRST_NAME, PERSON_LAST_NAME, PERSON_EMAIL]
diff --git a/canonical/metrics/course.yaml b/canonical/metrics/course.yaml
index 03a13dd..1948920 100644
--- a/canonical/metrics/course.yaml
+++ b/canonical/metrics/course.yaml
@@ -18,3 +18,33 @@ metrics:
     default_filters: [ongoing]
     synonyms: [active courses, current courses, running courses]
     example_questions: ["How many courses are running right now?"]
+  - id: metric.courses.v1
+    display_name: Courses
+    description: Top-level courses, including courses not yet started.
+    owner: Blackboard
+    authority: vendor-canonical
+    last_reviewed: 2026-10-08
+    measure: dataset.course_catalog.v1:courses
+    default_filters: [top_level]
+    synonyms: [course count, number of courses]
+    example_questions: ["How many courses are there?"]
+  - id: metric.classic_courses.v1
+    display_name: Classic courses
+    description: Top-level courses still in the Original (Classic) course view.
+    owner: Blackboard
+    authority: vendor-canonical
+    last_reviewed: 2026-10-08
+    measure: dataset.course_catalog.v1:courses
+    default_filters: [top_level, classic]
+    synonyms: [classic holdouts, original experience courses]
+    example_questions: ["How many Classic courses are left?"]
+  - id: metric.ultra_courses.v1
+    display_name: Ultra courses
+    description: Top-level courses in the Ultra course view.
+    owner: Blackboard
+    authority: vendor-canonical
+    last_reviewed: 2026-10-08
+    measure: dataset.course_catalog.v1:courses
+    default_filters: [top_level, ultra]
+    synonyms: [ultra adoption, ultra courses]
+    example_questions: ["How many courses are on Ultra?"]
diff --git a/canonical/metrics/engagement.yaml b/canonical/metrics/engagement.yaml
new file mode 100644
index 0000000..9574283
--- /dev/null
+++ b/canonical/metrics/engagement.yaml
@@ -0,0 +1,49 @@
+metrics:
+  - id: metric.student_engagement_rate.v1
+    display_name: Student engagement rate
+    description: Share of student enrollments with course activity in the course window.
+    owner: Blackboard
+    authority: vendor-canonical
+    last_reviewed: 2026-10-08
+    measure: dataset.course_role_activity.v1:share_active
+    default_filters: [students]
+    synonyms: [engagement rate, participation rate]
+    example_questions: ["What share of students are engaging with their courses?"]
+  - id: metric.active_courses.v1
+    display_name: Courses with student activity
+    description: Reportable courses where at least one student has course activity in the course window.
+    owner: Blackboard
+    authority: vendor-canonical
+    last_reviewed: 2026-10-08
+    measure: dataset.course_role_activity.v1:courses_with_activity
+    default_filters: [students]
+    synonyms: [active courses, courses in use]
+    example_questions: ["How many courses have student activity this term?"]
+  - id: metric.weekly_engagement_rate.v1
+    display_name: Students active in the last 7 days
+    description: Share of students with course activity whose last course access was in the last 7 days.
+    owner: Blackboard
+    authority: vendor-canonical
+    last_reviewed: 2026-10-08
+    measure: dataset.course_student_activity.v1:share_active_last_7_days
+    synonyms: [platform engagement, weekly engagement, active this week]
+    example_questions: ["How engaged are students this week?"]
+  - id: metric.minutes_per_active_student.v1
+    display_name: Minutes per active student
+    description: Course minutes divided by students with in-window activity.
+    owner: Blackboard
+    authority: vendor-canonical
+    last_reviewed: 2026-10-08
+    measure: dataset.course_role_activity.v1:minutes_per_active_person
+    default_filters: [students]
+    synonyms: [time on task, time in course]
+    example_questions: ["How much time do students spend in courses?"]
+  - id: metric.content_completion_rate.v1
+    display_name: Content completion rate
+    description: Content items students marked complete, as a share of the available content items in their courses.
+    owner: Blackboard
+    authority: vendor-canonical
+    last_reviewed: 2026-10-08
+    measure: dataset.course_student_activity.v1:content_completion_rate
+    synonyms: [content completion, progress tracking]
+    example_questions: ["How much course content are students completing?"]
diff --git a/canonical/metrics/enrollment.yaml b/canonical/metrics/enrollment.yaml
new file mode 100644
index 0000000..4e0aa08
--- /dev/null
+++ b/canonical/metrics/enrollment.yaml
@@ -0,0 +1,22 @@
+metrics:
+  - id: metric.active_students.v1
+    display_name: Active students
+    description: >
+      Students with an available, enabled enrollment and course activity inside the course window,
+      in courses that have an instructor and have not been deleted.
+    owner: Blackboard
+    authority: vendor-canonical
+    last_reviewed: 2026-10-08
+    measure: dataset.active_students.v1:active_students
+    default_filters: [live_courses]
+    synonyms: [active students, active learners, engaged students]
+    example_questions: ["How many active students do we have?", "Active students by course start month"]
+  - id: metric.students_with_activity.v1
+    display_name: Students with course activity
+    description: Distinct students with any in-window course activity, active or not.
+    owner: Blackboard
+    authority: vendor-canonical
+    last_reviewed: 2026-10-08
+    measure: dataset.active_students.v1:students
+    synonyms: [student count, how many students]
+    example_questions: ["How many students used their courses?"]
diff --git a/canonical/metrics/grades.yaml b/canonical/metrics/grades.yaml
new file mode 100644
index 0000000..ff5ff97
--- /dev/null
+++ b/canonical/metrics/grades.yaml
@@ -0,0 +1,19 @@
+metrics:
+  - id: metric.average_grade.v1
+    display_name: Average grade
+    description: Average course grade percentage across graded student enrollments in reportable courses.
+    owner: Blackboard
+    authority: vendor-canonical
+    last_reviewed: 2026-10-08
+    measure: dataset.student_grade.v1:average_grade_percentage
+    synonyms: [average grade, gpa, mean grade]
+    example_questions: ["What is the average grade?", "What does the grade distribution look like?"]
+  - id: metric.failing_rate.v1
+    display_name: Share of students failing
+    description: Graded student enrollments below 60%, as a share of graded enrollments.
+    owner: Blackboard
+    authority: vendor-canonical
+    last_reviewed: 2026-10-08
+    measure: dataset.student_grade.v1:share_failing
+    synonyms: [failure rate, at risk by grade]
+    example_questions: ["What share of students are failing?"]
```

- [ ] **Step 3: Verify**

Run: `$PY -m pytest -q tests`
Expected: all pass.

Live run recorded during planning: three metrics from three datasets combined by CROSS JOIN: 361 active students, 94 reportable courses, average grade 40.95, 7.4 s; many-to-one dimension join (average grade by active-student course name, CTE bodies trimmed to the joined columns): 5 rows, 5.8 s.

- [ ] **Step 4: Commit, open the PR, merge** (PR body: the summary above plus the live-run line).
