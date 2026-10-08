"""Compile a QueryContract into Snowflake SQL over the semantic catalog.

Dataset SQL is inlined verbatim as CTEs (dependencies first); the outer SELECT is
built as a sqlglot AST, so filter values are always typed literals and never
spliced into SQL text.
"""

from __future__ import annotations

from typing import Optional

import sqlglot
import sqlglot.expressions as exp

from .contract import CompiledQuery, ContractFilter, Provenance, QueryContract
from .render import TemplateError, cte_name, render
from .schema import Catalog, Dataset, DatasetDimension, Measure, SemanticMetric


class CompileError(ValueError):
    """The contract cannot be compiled against the catalog."""


def build_ctes(catalog: Catalog, dataset_ids: list[str], database: str) -> dict[str, str]:
    """Rendered SQL for the datasets and everything they depend on, keyed by CTE name, dependencies first."""
    done: dict[str, str] = {}

    def visit(ds_id: str, path: tuple[str, ...]) -> None:
        if cte_name(ds_id) in done:
            return
        if ds_id in path:
            raise CompileError(f"dataset dependency cycle: {' -> '.join(path + (ds_id,))}")
        ds = catalog.datasets.get(ds_id)
        if ds is None:
            raise CompileError(f"unknown dataset {ds_id}" + (f" (referenced by {path[-1]})" if path else ""))

        def on_ref(target: str) -> str:
            if target not in ds.depends_on:
                raise CompileError(f"{ds.id} refs {target} without listing it in depends_on")
            return cte_name(target)

        try:
            sql = render(ds.base_sql, database, on_ref)
        except TemplateError as e:
            raise CompileError(f"{ds.id}: {e}") from e
        for dep in ds.depends_on:
            visit(dep, path + (ds_id,))
        done[cte_name(ds_id)] = sql

    for ds_id in dataset_ids:
        visit(ds_id, ())
    return done


def _literal(value) -> exp.Expression:
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, (int, float)):
        return exp.Literal.number(value)
    return exp.Literal.string(str(value))


def _condition(col: exp.Expression, f: ContractFilter) -> exp.Expression:
    v = [_literal(x) for x in f.values]
    if f.op == "eq":
        return exp.EQ(this=col, expression=v[0])
    if f.op == "neq":
        return exp.NEQ(this=col, expression=v[0])
    if f.op == "gt":
        return exp.GT(this=col, expression=v[0])
    if f.op == "gte":
        return exp.GTE(this=col, expression=v[0])
    if f.op == "lt":
        return exp.LT(this=col, expression=v[0])
    if f.op == "lte":
        return exp.LTE(this=col, expression=v[0])
    if f.op == "in":
        return exp.In(this=col, expressions=v)
    if f.op == "not_in":
        return exp.Not(this=exp.In(this=col, expressions=v))
    if f.op == "between":
        return exp.Between(this=col, low=v[0], high=v[1])
    if f.op == "is_null":
        return exp.Is(this=col, expression=exp.Null())
    if f.op == "not_null":
        return exp.Not(this=exp.Is(this=col, expression=exp.Null()))
    # contains: case-insensitive substring; CONTAINS avoids LIKE wildcard escaping.
    return exp.Anonymous(this="CONTAINS", expressions=[exp.Lower(this=col), exp.Lower(this=v[0])])


def _parse(sql: str, what: str) -> exp.Expression:
    try:
        return sqlglot.parse_one(sql, read="snowflake")
    except sqlglot.errors.ParseError as e:
        raise CompileError(f"{what}: {e}") from e


def _aggregate(ds: Dataset, m: Measure, scope: Optional[exp.Expression]) -> exp.Expression:
    if m.agg == "ratio":
        num = _aggregate(ds, ds.measure(m.numerator), scope)
        den = _aggregate(ds, ds.measure(m.denominator), scope)
        return exp.Div(this=num, expression=exp.Anonymous(this="NULLIF", expressions=[den, exp.Literal.number(0)]))
    arg = _parse(m.expr, f"{ds.id}:{m.name} expr")
    if scope is not None:
        # COUNT(*) under a scope counts matching rows: CASE ... THEN * is not valid SQL.
        then = exp.Literal.number(1) if isinstance(arg, exp.Star) else arg
        arg = exp.Case(ifs=[exp.If(this=scope.copy(), true=then)])
    if m.agg == "count_distinct":
        return exp.Count(this=exp.Distinct(expressions=[arg]))
    return exp.Anonymous(this=m.agg.upper(), expressions=[arg])


def _split_ref(ref: str) -> tuple[Optional[str], str, Optional[str]]:
    """'[dataset.x.v1:]name[__grain]' -> (dataset id or None, name, grain or None)."""
    qualifier, sep, rest = ref.rpartition(":")
    name, suffix, grain = rest.partition("__")
    if suffix and not grain:
        raise CompileError(f"dimension {ref!r} has an empty grain after '__'")
    return (qualifier if sep else None), name, (grain or None)


def _output_name(ref: str) -> str:
    """The result column for a dimension reference: its name and grain, without a dataset qualifier."""
    return ref.rpartition(":")[2]


def _joins_from(base: Dataset, catalog: Catalog, complete_only: bool = True) -> dict[str, tuple[Dataset, str, str]]:
    """Datasets reachable many-to-one from base: id -> (dataset, base column, its primary column)."""
    out: dict[str, tuple[Dataset, str, str]] = {}
    for other in catalog.datasets.values():
        if other.id == base.id or (complete_only and not other.complete):
            continue
        for mine in base.entities:
            theirs = next((e for e in other.entities if e.name == mine.name and e.type == "primary"), None)
            if theirs is not None and other.id not in out:
                out[other.id] = (other, mine.column, theirs.column)
    return out


def _resolve(base: Dataset, ref: str, catalog: Catalog, joins: dict) -> tuple[Dataset, DatasetDimension, Optional[str]]:
    ds_id, name, grain = _split_ref(ref)
    if ds_id is not None and ds_id != base.id:
        target = catalog.datasets.get(ds_id)
        if target is None:
            raise CompileError(f"unknown dataset {ds_id} in dimension {ref!r}")
        if ds_id not in joins:
            if ds_id in _joins_from(base, catalog, complete_only=False):
                raise CompileError(
                    f"{ds_id} is not complete (it omits some {base.id} rows), so it cannot supply dimensions"
                )
            reverse = _joins_from(target, catalog, complete_only=False)
            if base.id in reverse:
                raise CompileError(f"joining {ds_id} to {base.id} would multiply {base.id} rows")
            raise CompileError(f"{ds_id} cannot be reached from {base.id} through a shared entity")
    elif ds_id is not None or base.dimension(name) is not None:
        target = base
    else:
        hits = [d for d, _, _ in joins.values() if d.dimension(name) is not None]
        if not hits:
            raise CompileError(f"unknown dimension {name!r} on {base.id} or the datasets it joins to")
        if len(hits) > 1:
            ids = ", ".join(sorted(h.id for h in hits))
            raise CompileError(f"dimension {name!r} is ambiguous ({ids}); qualify it as '<dataset id>:{name}'")
        target = hits[0]
    dim = target.dimension(name)
    if dim is None:
        raise CompileError(f"{target.id} has no dimension {name!r}")
    if grain and grain not in dim.grains:
        raise CompileError(f"dimension {name!r} does not support grain {grain!r}; allowed: {dim.grains}")
    return target, dim, grain


def _qualified(node: exp.Expression, table: Optional[str]) -> exp.Expression:
    if table:
        for col in node.find_all(exp.Column):
            if not col.table:
                col.set("table", exp.to_identifier(table))
    return node


def _unquoted_upper(ident: Optional[exp.Identifier]) -> str:
    """The identifier as Snowflake resolves it; '' when absent."""
    if ident is None:
        return ""
    return ident.name if ident.quoted else ident.name.upper()


def _check_tables(sql: str, database: str) -> None:
    """Every real table must be <database>.CDM_*.<table>; unqualified names must be CTEs."""
    tree = _parse(sql, "compiled query")
    local = {cte.alias_or_name.upper() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        schema = _unquoted_upper(table.args.get("db"))
        catalog = _unquoted_upper(table.args.get("catalog"))
        if not schema and not catalog and table.name.upper() in local:
            continue
        if catalog != database.upper() or not schema.startswith("CDM_"):
            raise CompileError(
                f"table {table.sql(dialect='snowflake')} is outside this database's CDM_* schemas"
            )


def _selections(contract: QueryContract, catalog: Catalog) -> list[tuple[str, Dataset, Measure, Optional[SemanticMetric]]]:
    out = []
    for metric_id in contract.metrics:
        metric = catalog.metrics.get(metric_id)
        if metric is None:
            raise CompileError(f"unknown metric {metric_id}")
        ds = catalog.datasets.get(metric.dataset_id)
        measure = ds.measure(metric.measure_name) if ds else None
        if measure is None:
            raise CompileError(f"metric {metric_id} points at missing measure {metric.measure}")
        out.append((metric.short_name, ds, measure, metric))
    for ref in contract.measures:
        ds_id, _, name = ref.partition(":")
        ds = catalog.datasets.get(ds_id)
        measure = ds.measure(name) if ds else None
        if measure is None:
            raise CompileError(f"unknown measure {ref}")
        out.append((name, ds, measure, None))
    for _, ds, _, _ in out:
        if ds.visibility != "public":
            raise CompileError(f"{ds.id} is internal and cannot be queried directly")
    return out


def _group_query(base: Dataset, selections: list, contract: QueryContract, catalog: Catalog) -> tuple[exp.Select, list[str]]:
    """One aggregate SELECT over base (plus many-to-one joins) and the dataset ids it reads."""
    joins = _joins_from(base, catalog)
    dims = [(ref, *_resolve(base, ref, catalog, joins)) for ref in contract.dimensions]
    filters = []
    for f in contract.filters:
        if "__" in f.dimension.rpartition(":")[2]:
            raise CompileError(
                f"filter dimension {f.dimension!r} has a grain suffix; filters take the plain dimension name"
            )
        filters.append((f, *_resolve(base, f.dimension, catalog, joins)[:2]))
    time_range = None
    if contract.time_range:
        target, dim, _ = _resolve(base, contract.time_range.dimension, catalog, joins)
        if dim.type != "time":
            raise CompileError(f"time_range needs a time dimension; {contract.time_range.dimension!r} is not one")
        time_range = (target, dim)

    joined = []
    for target in [t for _, t, _, _ in dims] + [t for _, t, _ in filters] + ([time_range[0]] if time_range else []):
        if target.id != base.id and target.id not in joined:
            joined.append(target.id)
    alias = {base.id: "b" if joined else None} | {ds_id: f"j{i}" for i, ds_id in enumerate(joined, start=1)}

    def column(target: Dataset, name: str) -> exp.Column:
        return exp.column(name, table=alias[target.id])

    select: list[exp.Expression] = []
    for ref, target, dim, grain in dims:
        if target.is_pii(dim.column):
            raise CompileError(f"dimension {dim.name!r} is personally identifiable and cannot be selected")
        col = column(target, dim.column)
        node = exp.Anonymous(this="DATE_TRUNC", expressions=[exp.Literal.string(grain), col]) if grain else col
        select.append(exp.alias_(node, _output_name(ref)))

    for out_name, _, measure, metric in selections:
        scope = None
        if metric and metric.default_filters:
            parts = []
            for fname in metric.default_filters:
                f = base.filter(fname)
                if f is None:
                    raise CompileError(f"metric {metric.id} uses unknown filter {fname!r}")
                parts.append(_parse(f.sql, f"{base.id} filter {fname}"))
            scope = exp.and_(*parts)
        select.append(exp.alias_(_qualified(_aggregate(base, measure, scope), alias[base.id]), out_name))

    where: list[exp.Expression] = []
    for f, target, dim in filters:
        col = column(target, dim.column)
        if dim.type == "time":
            col = exp.Cast(this=col, to=exp.DataType.build("DATE"))
        where.append(_condition(col, f))
    if time_range:
        day = exp.Cast(this=column(*time_range[:1], time_range[1].column), to=exp.DataType.build("DATE"))
        if contract.time_range.start:
            where.append(exp.GTE(this=day, expression=exp.Literal.string(contract.time_range.start.isoformat())))
        if contract.time_range.end:
            where.append(exp.LTE(this=day.copy(), expression=exp.Literal.string(contract.time_range.end.isoformat())))

    source = exp.to_table(cte_name(base.id))
    query = exp.select(*select).from_(exp.alias_(source, "b", table=True) if joined else source)
    for ds_id in joined:
        other, mine, theirs = joins[ds_id]
        on = exp.EQ(this=exp.column(mine, table="b"), expression=exp.column(theirs, table=alias[ds_id]))
        query = query.join(exp.alias_(exp.to_table(cte_name(ds_id)), alias[ds_id], table=True), on=on, join_type="left")
    if where:
        query = query.where(exp.and_(*where))
    if contract.dimensions:
        query = query.group_by(*[exp.Literal.number(i + 1) for i in range(len(contract.dimensions))])
    return query, [base.id] + joined


def _combine(groups: list[str], contract: QueryContract, measure_names: list[list[str]]) -> exp.Select:
    """Join per-dataset aggregate CTEs g1..gN on the shared dimensions (null-safe)."""
    dims = [_output_name(d) for d in contract.dimensions]
    select = [
        exp.alias_(exp.Coalesce(this=exp.column(d, table=groups[0]), expressions=[exp.column(d, table=g) for g in groups[1:]]), d)
        for d in dims
    ]
    for g, names in zip(groups, measure_names):
        select += [exp.column(n, table=g) for n in names]
    query = exp.select(*select).from_(groups[0])
    for i, g in enumerate(groups[1:], start=1):
        if not dims:
            query = query.join(g, join_type="cross")
            continue
        conditions = []
        for d in dims:
            prior = [exp.column(d, table=p) for p in groups[:i]]
            left = prior[0] if len(prior) == 1 else exp.Coalesce(this=prior[0], expressions=prior[1:])
            conditions.append(exp.NullSafeEQ(this=left, expression=exp.column(d, table=g)))
        query = query.join(g, on=exp.and_(*conditions), join_type="full outer")
    return query


def compile_query(contract: QueryContract, catalog: Catalog, database: str) -> CompiledQuery:
    selections = _selections(contract, catalog)
    aliases = [_output_name(d) for d in contract.dimensions] + [name for name, _, _, _ in selections]
    if len({a.lower() for a in aliases}) != len(aliases):
        raise CompileError(f"output names collide: {aliases}")

    bases: dict[str, list] = {}
    for sel in selections:
        bases.setdefault(sel[1].id, []).append(sel)
    groups = [(_group_query(catalog.datasets[ds_id], sels, contract, catalog), sels) for ds_id, sels in bases.items()]

    read = list(dict.fromkeys(ds_id for (_, used), _ in groups for ds_id in used))
    ctes = build_ctes(catalog, read, database)
    with_parts = [f"{name} AS (\n{sql}\n)" for name, sql in ctes.items()]

    if len(groups) == 1:
        query = groups[0][0][0]
    else:
        names = [f"g{i}" for i in range(1, len(groups) + 1)]
        with_parts += [f"{n} AS (\n{q.sql(dialect='snowflake', pretty=True)}\n)" for n, ((q, _), _) in zip(names, groups)]
        query = _combine(names, contract, [[name for name, _, _, _ in sels] for _, sels in groups])

    lowered = {a.lower() for a in aliases}
    for ob in contract.order_by:
        if ob.field.lower() not in lowered:
            raise CompileError(f"order_by {ob.field!r} is not one of the selected fields {aliases}")
        query = query.order_by(exp.Ordered(this=exp.column(ob.field), desc=ob.direction == "desc"))
    query = query.limit(contract.limit)

    sql = "WITH " + ",\n".join(with_parts) + "\n" + query.sql(dialect="snowflake", pretty=True)
    _check_tables(sql, database)

    return CompiledQuery(
        sql=sql,
        provenance=Provenance(
            datasets=[ds_id for ds_id in catalog.datasets if cte_name(ds_id) in ctes],
            metrics=list(contract.metrics),
            measures=[f"{d.id}:{m.name}" for _, d, m, _ in selections],
        ),
    )
