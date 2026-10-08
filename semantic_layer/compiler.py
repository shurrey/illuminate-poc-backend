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
        arg = exp.Case(ifs=[exp.If(this=scope.copy(), true=arg)])
    if m.agg == "count_distinct":
        return exp.Count(this=exp.Distinct(expressions=[arg]))
    return exp.Anonymous(this=m.agg.upper(), expressions=[arg])


def _resolve_dimension(ds: Dataset, ref: str) -> tuple[DatasetDimension, Optional[str]]:
    name, _, grain = ref.partition("__")
    dim = ds.dimension(name)
    if dim is None:
        raise CompileError(f"unknown dimension {name!r} on {ds.id}")
    if grain and grain not in dim.grains:
        raise CompileError(f"dimension {name!r} does not support grain {grain!r}; allowed: {dim.grains}")
    return dim, grain or None


def _check_tables(sql: str) -> None:
    tree = _parse(sql, "compiled query")
    local = {cte.alias_or_name.upper() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        schema = table.db.upper()
        if not schema and table.name.upper() in local:
            continue
        if not schema.startswith("CDM_"):
            raise CompileError(f"table {table.sql(dialect='snowflake')} is outside the CDM_* schemas")


def compile_query(contract: QueryContract, catalog: Catalog, database: str) -> CompiledQuery:
    selections: list[tuple[str, Dataset, Measure, Optional[SemanticMetric]]] = []
    for metric_id in contract.metrics:
        metric = catalog.metrics.get(metric_id)
        if metric is None:
            raise CompileError(f"unknown metric {metric_id}")
        ds = catalog.datasets.get(metric.dataset_id)
        measure = ds.measure(metric.measure_name) if ds else None
        if measure is None:
            raise CompileError(f"metric {metric_id} points at missing measure {metric.measure}")
        selections.append((metric.short_name, ds, measure, metric))
    for ref in contract.measures:
        ds_id, _, name = ref.partition(":")
        ds = catalog.datasets.get(ds_id)
        measure = ds.measure(name) if ds else None
        if measure is None:
            raise CompileError(f"unknown measure {ref}")
        selections.append((name, ds, measure, None))

    dataset_ids = sorted({ds.id for _, ds, _, _ in selections})
    if len(dataset_ids) > 1:
        raise CompileError(f"measures from more than one dataset are not supported yet: {dataset_ids}")
    ds = selections[0][1]
    if ds.visibility != "public":
        raise CompileError(f"{ds.id} is internal and cannot be queried directly")

    select: list[exp.Expression] = []
    aliases: list[str] = []
    for ref in contract.dimensions:
        dim, grain = _resolve_dimension(ds, ref)
        if dim.column in ds.pii_columns:
            raise CompileError(f"dimension {dim.name!r} is personally identifiable and cannot be selected")
        col = exp.column(dim.column)
        node = exp.Anonymous(this="DATE_TRUNC", expressions=[exp.Literal.string(grain), col]) if grain else col
        select.append(exp.alias_(node, ref))
        aliases.append(ref)

    for alias, _, measure, metric in selections:
        scope = None
        if metric and metric.default_filters:
            parts = []
            for fname in metric.default_filters:
                f = ds.filter(fname)
                if f is None:
                    raise CompileError(f"metric {metric.id} uses unknown filter {fname!r}")
                parts.append(_parse(f.sql, f"{ds.id} filter {fname}"))
            scope = exp.and_(*parts)
        select.append(exp.alias_(_aggregate(ds, measure, scope), alias))
        aliases.append(alias)
    if len(set(aliases)) != len(aliases):
        raise CompileError(f"output names collide: {aliases}")

    where: list[exp.Expression] = []
    for f in contract.filters:
        dim = ds.dimension(f.dimension)
        if dim is None:
            raise CompileError(f"unknown filter dimension {f.dimension!r} on {ds.id}")
        col = exp.column(dim.column)
        if dim.type == "time":
            col = exp.Cast(this=col, to=exp.DataType.build("DATE"))
        where.append(_condition(col, f))
    if contract.time_range:
        dim = ds.dimension(contract.time_range.dimension)
        if dim is None or dim.type != "time":
            raise CompileError(f"time_range needs a time dimension; {contract.time_range.dimension!r} is not one")
        day = exp.Cast(this=exp.column(dim.column), to=exp.DataType.build("DATE"))
        if contract.time_range.start:
            where.append(exp.GTE(this=day, expression=exp.Literal.string(contract.time_range.start.isoformat())))
        if contract.time_range.end:
            where.append(exp.LTE(this=day.copy(), expression=exp.Literal.string(contract.time_range.end.isoformat())))

    query = exp.select(*select).from_(cte_name(ds.id))
    if where:
        query = query.where(exp.and_(*where))
    if contract.dimensions:
        query = query.group_by(*[exp.Literal.number(i + 1) for i in range(len(contract.dimensions))])
    lowered = {a.lower() for a in aliases}
    for ob in contract.order_by:
        if ob.field.lower() not in lowered:
            raise CompileError(f"order_by {ob.field!r} is not one of the selected fields {aliases}")
        query = query.order_by(exp.Ordered(this=exp.column(ob.field), desc=ob.direction == "desc"))
    query = query.limit(contract.limit)

    ctes = build_ctes(catalog, [ds.id], database)
    with_clause = ",\n".join(f"{name} AS (\n{sql}\n)" for name, sql in ctes.items())
    sql = f"WITH {with_clause}\n{query.sql(dialect='snowflake', pretty=True)}"
    _check_tables(sql)

    return CompiledQuery(
        sql=sql,
        provenance=Provenance(
            datasets=[ds_id for ds_id in catalog.datasets if cte_name(ds_id) in ctes],
            metrics=list(contract.metrics),
            measures=[f"{d.id}:{m.name}" for _, d, m, _ in selections],
        ),
    )
