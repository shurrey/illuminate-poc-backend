"""Tenant overlays: per-tenant replacements for a measure's expr, a filter's sql, or a metric's default filters.

Targets are `measure:<dataset id>:<name>`, `filter:<dataset id>:<name>` (a new name adds a tenant-only
filter) and `metric:<metric id>`. Overlays never change base SQL, entities or grain, and are checked by
the same validator and compiler as canonical definitions.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable, Literal, Optional

import sqlglot
import sqlglot.expressions as exp
from pydantic import BaseModel, model_validator
from sqlglot.errors import ParseError

from .compiler import CompileError, compile_query
from .contract import QueryContract
from .schema import Catalog, Dataset, DatasetFilter, SemanticMetric
from .validate import load_pii_columns, load_snapshot, validate_dataset, validate_metric

Kind = Literal["measure", "filter", "metric"]
_FIELD = {"measure": "expr", "filter": "sql", "metric": "default_filters"}
_FORBIDDEN = (exp.Query, exp.Subquery, exp.Table, exp.DML, exp.DDL, exp.Command)
# Forms that reach columns or data without naming a column: *, $n, IDENTIFIER(), qualified UDFs.
_HIDDEN_ACCESS = (exp.Star, exp.StarMap, exp.Parameter, exp.Placeholder, exp.Dot, exp.CurrentUser)
# Functions sqlglot doesn't model; anything else untyped (IDENTIFIER, GET, HASH, UDFs...) is refused.
_UNTYPED_ALLOWED = frozenset({
    "TRY_TO_NUMBER", "TRY_TO_DECIMAL", "TRY_TO_DOUBLE", "TRY_TO_DATE", "TRY_TO_TIMESTAMP", "TRY_TO_BOOLEAN",
    "DIV0", "DIV0NULL", "NULLIFZERO", "NVL2", "EQUAL_NULL", "LEAST_IGNORE_NULLS", "GREATEST_IGNORE_NULLS",
    "CONTAINS", "STARTSWITH", "ENDSWITH", "REGEXP_LIKE", "DATE_FROM_PARTS", "TRUNCATE", "SIGN", "DATE_TRUNC",
})
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class OverlayError(ValueError):
    pass


def parse_target(target: str) -> tuple[Kind, str, Optional[str]]:
    """(kind, dataset or metric id, measure or filter name); OverlayError when malformed."""
    kind, _, rest = target.partition(":")
    if kind == "metric" and rest:
        return "metric", rest, None
    owner, _, name = rest.rpartition(":")
    if kind in ("measure", "filter") and owner and _NAME.match(name):
        return kind, owner, name
    raise OverlayError(
        f"invalid overlay target {target!r}; use measure:<dataset>:<name>, filter:<dataset>:<name> or metric:<id>, "
        "where name is lowercase letters, digits and underscores, starting with a letter"
    )


class Overlay(BaseModel):
    target: str
    expr: Optional[str] = None
    sql: Optional[str] = None
    default_filters: Optional[list[str]] = None
    description: str = ""
    version: int = 0
    updated_by: str = ""
    updated_at: str = ""

    @model_validator(mode="after")
    def _sets_exactly_its_field(self) -> "Overlay":
        try:
            kind = parse_target(self.target)[0]
        except OverlayError:
            return self
        set_fields = {f for f in _FIELD.values() if getattr(self, f) is not None}
        if set_fields != {_FIELD[kind]}:
            raise ValueError(f"a {kind} overlay sets only {_FIELD[kind]}")
        return self

    @property
    def kind(self) -> Kind:
        return parse_target(self.target)[0]


def check_expression(sql: str, what: str) -> None:
    """One plain SQL expression over the dataset's columns: no templates, statements, queries or tables."""
    if any(token in sql for token in ("{{", "{%", "{#")):
        raise OverlayError(f"{what}: template syntax is not allowed")
    try:
        parsed = [s for s in sqlglot.parse(sql, read="snowflake") if s is not None]
    except ParseError as e:
        raise OverlayError(f"{what}: {e}") from e
    if len(parsed) != 1:
        raise OverlayError(f"{what}: must be exactly one expression")
    for node in parsed[0].walk():
        if isinstance(node, _FORBIDDEN):
            raise OverlayError(f"{what}: may not contain queries or tables")
        if isinstance(node, (exp.AggFunc, exp.Window)):
            raise OverlayError(f"{what}: may not contain aggregate or window functions; the measure's agg is applied for you")
        if isinstance(node, _HIDDEN_ACCESS) or (isinstance(node, exp.Anonymous) and str(node.this).upper() not in _UNTYPED_ALLOWED):
            raise OverlayError(f"{what}: {node.sql(dialect='snowflake')[:60]} is not allowed; reference the dataset's columns by name")


def _with_dataset(catalog: Catalog, ds: Dataset, **changes) -> Catalog:
    # model_validate re-runs the dataset's own checks (unique names, ratios resolve).
    updated = Dataset.model_validate({**ds.model_dump(), **changes})
    return catalog.model_copy(update={"datasets": {**catalog.datasets, ds.id: updated}})


def _apply(catalog: Catalog, ov: Overlay) -> Catalog:
    kind, owner, name = parse_target(ov.target)
    if kind == "metric":
        metric = catalog.metrics.get(owner)
        if metric is None:
            raise OverlayError(f"unknown metric {owner}")
        updated = SemanticMetric.model_validate({**metric.model_dump(), "default_filters": ov.default_filters})
        return catalog.model_copy(update={"metrics": {**catalog.metrics, owner: updated}})
    ds = catalog.datasets.get(owner)
    if ds is None:
        raise OverlayError(f"unknown dataset {owner}")
    if kind == "measure":
        measure = ds.measure(name)
        if measure is None:
            raise OverlayError(f"{owner} has no measure {name!r}")
        if measure.agg == "ratio":
            raise OverlayError(f"{owner}:{name} is a ratio; overlay its numerator or denominator instead")
        measures = [m.model_dump() | ({"expr": ov.expr} if m.name == name else {}) for m in ds.measures]
        return _with_dataset(catalog, ds, measures=measures)
    new = DatasetFilter(name=name, sql=ov.sql, description=ov.description or (ds.filter(name).description if ds.filter(name) else ""))
    filters = [f.model_dump() for f in ds.filters if f.name != name] + [new.model_dump()]
    return _with_dataset(catalog, ds, filters=filters)


def apply_overlays(catalog: Catalog, overlays: Iterable[Overlay]) -> Catalog:
    """A new catalog with the overlays applied in order; the given catalog is unchanged."""
    for ov in overlays:
        catalog = _apply(catalog, ov)
    return catalog


@lru_cache(maxsize=1)
def _dictionary() -> tuple[dict, frozenset[str]]:
    return load_snapshot(), load_pii_columns()


def validate_overlay(ov: Overlay, catalog: Catalog) -> list[str]:
    """Errors that would make this overlay unsafe or unusable; empty when it can be saved."""
    kind, owner, _ = parse_target(ov.target)
    if kind != "metric":
        check_expression(ov.expr if kind == "measure" else ov.sql, ov.target)
    candidate = apply_overlays(catalog, [ov])
    if kind == "metric":
        errors = validate_metric(candidate.metrics[owner], candidate)
        affected = [owner]
    else:
        snapshot, pii = _dictionary()
        errors = validate_dataset(candidate.datasets[owner], candidate, snapshot, pii)
        affected = [m.id for m in candidate.metrics.values() if m.dataset_id == owner]
    if errors:
        return errors
    for metric_id in affected:
        try:
            compile_query(QueryContract(metrics=[metric_id]), candidate, "VALIDATION_DB")
        except CompileError as e:
            errors.append(f"{metric_id}: {e}")
    return errors
