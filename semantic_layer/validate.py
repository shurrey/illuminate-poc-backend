"""Offline validation of catalog definitions against a CDM dictionary snapshot.

Every column a dataset reads must exist in the snapshot, every column a
dimension, measure, filter or entity names must be one the dataset outputs, and
every output traced to a dictionary-flagged PII column must be declared.
"""

from __future__ import annotations

import json
from pathlib import Path

import sqlglot
import sqlglot.expressions as exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.lineage import lineage
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.qualify import qualify
from sqlglot.schema import MappingSchema

from .compiler import CompileError, build_ctes
from .pii import PII_COLUMN_NAMES, inside_counting_aggregate
from .schema import Catalog, Dataset, SemanticMetric

# Shipped with the package: overlays are validated at runtime against the same snapshot as canonical definitions.
_DATA = Path(__file__).resolve().parent / "data"
DICTIONARY_SNAPSHOT = _DATA / "cdm_dictionary.json"
PII_SNAPSHOT = _DATA / "cdm_pii_columns.json"
SUPPLEMENT_SNAPSHOT = _DATA / "cdm_dictionary_supplement.json"
_DB = "VALIDATION_DB"


def load_snapshot(path: Path = DICTIONARY_SNAPSHOT, supplement: Path = SUPPLEMENT_SNAPSHOT) -> dict:
    """The snapshot as a sqlglot schema: {database: {schema: {table: {column: type}}}}.

    The supplement adds tables the dictionary export omits, taken from the live schema.
    """
    schemas = json.loads(path.read_text())
    for schema, tables in json.loads(supplement.read_text()).items():
        if not schema.startswith("_"):
            schemas.setdefault(schema, {}).update(tables)
    return {_DB: schemas}


def load_pii_columns(path: Path = PII_SNAPSHOT) -> frozenset[str]:
    """Dictionary-flagged PII columns as SCHEMA.TABLE.COLUMN."""
    return frozenset(json.loads(path.read_text()))


def _columns_in(sql: str) -> set[str]:
    return {c.name.upper() for c in sqlglot.parse_one(sql, read="snowflake").find_all(exp.Column)}


def _dataset_sql(ds: Dataset, catalog: Catalog) -> str:
    ctes = build_ctes(catalog, [ds.id], _DB)
    *deps, own = ctes.items()
    if not deps:
        return own[1]
    return "WITH " + ",\n".join(f"{n} AS (\n{s}\n)" for n, s in deps) + f"\nSELECT * FROM ({own[1]})"


def _qualified(ds: Dataset, catalog: Catalog, snapshot: dict) -> exp.Expression:
    return qualify(
        sqlglot.parse_one(_dataset_sql(ds, catalog), read="snowflake"),
        schema=snapshot, dialect="snowflake", validate_qualify_columns=True,
    )


def output_columns(ds: Dataset, catalog: Catalog, snapshot: dict) -> list[str]:
    """Qualify the dataset's SQL against the snapshot and return its output column names."""
    return [c.upper() for c in _qualified(ds, catalog, snapshot).named_selects]


_TYPE_CHECKS = {
    "boolean": lambda t: t.this == exp.DataType.Type.BOOLEAN,
    "time": lambda t: t.this in exp.DataType.TEMPORAL_TYPES,
    "numeric": lambda t: t.this in exp.DataType.NUMERIC_TYPES,
}


def _dimension_type_errors(ds: Dataset, catalog: Catalog, snapshot: dict) -> list[str]:
    """Columns whose type sqlglot cannot infer (UNKNOWN) are not checked."""
    # A MappingSchema normalizes the quoted identifiers qualify emits; the raw dict does not.
    schema = MappingSchema(snapshot, dialect="snowflake")
    tree = annotate_types(_qualified(ds, catalog, snapshot), schema=schema, dialect="snowflake")
    types = {s.alias_or_name.upper(): s.type for s in tree.selects}
    errors = []
    for d in ds.dimensions:
        t = types.get(d.column.upper())
        check = _TYPE_CHECKS.get(d.type)
        if check and t is not None and t.this != exp.DataType.Type.UNKNOWN and not check(t):
            errors.append(f"{ds.id}: dimension {d.name} is typed {d.type} but column {d.column} is {t.sql()}")
    return errors


def _traced_pii(ds: Dataset, catalog: Catalog, snapshot: dict, outputs: set[str], pii: frozenset[str]) -> set[str]:
    """Outputs whose lineage reaches a PII-flagged source column other than through a count."""
    sql = _dataset_sql(ds, catalog)

    def reaches_pii(node) -> bool:
        expr = node.expression
        columns = list(expr.find_all(exp.Column))
        if columns and all(inside_counting_aggregate(c, expr) for c in columns):
            return False
        if not node.downstream:
            if not isinstance(node.source, exp.Table):
                return False
            return f"{node.source.db}.{node.source.name}.{node.name.split('.')[-1]}".upper() in pii
        return any(reaches_pii(d) for d in node.downstream)

    return {col for col in outputs if reaches_pii(lineage(col, sql, schema=snapshot, dialect="snowflake"))}


def validate_dataset(ds: Dataset, catalog: Catalog, snapshot: dict, pii: frozenset[str]) -> list[str]:
    try:
        outputs = set(output_columns(ds, catalog, snapshot))
    except (CompileError, OptimizeError, ParseError) as e:
        return [f"{ds.id}: {e}"]

    errors: list[str] = []
    exempt = {c.upper() for c in ds.pii_exempt}
    errors += [f"{ds.id}: {c} is a known PII column name and cannot be exempt" for c in sorted(exempt & PII_COLUMN_NAMES)]
    pii_outputs = (outputs & PII_COLUMN_NAMES) | (_traced_pii(ds, catalog, snapshot, outputs, pii) - exempt)

    def need(cols: set[str], what: str) -> None:
        missing = sorted(cols - outputs)
        if missing:
            errors.append(f"{ds.id}: {what} uses columns the dataset does not output: {missing}")

    for e in ds.entities:
        need({e.column.upper()}, f"entity {e.name}")
    for d in ds.dimensions:
        need({d.column.upper()}, f"dimension {d.name}")
    for m in ds.measures:
        if m.expr:
            cols = _columns_in(m.expr)
            need(cols, f"measure {m.name}")
            exposed = sorted(c for c in cols if c in pii_outputs or ds.is_pii(c))
            if exposed and m.agg not in ("count", "count_distinct"):
                errors.append(f"{ds.id}: measure {m.name} returns values of PII columns {exposed}; only counts are allowed")
    for f in ds.filters:
        need(_columns_in(f.sql), f"filter {f.name}")
    need({c.upper() for c in ds.pii_columns}, "pii_columns")
    undeclared = sorted(pii_outputs - {c.upper() for c in ds.pii_columns})
    if undeclared:
        errors.append(f"{ds.id}: outputs PII columns not listed in pii_columns: {undeclared}")
    return errors + _dimension_type_errors(ds, catalog, snapshot)


def validate_metric(m: SemanticMetric, catalog: Catalog) -> list[str]:
    ds = catalog.datasets.get(m.dataset_id)
    if ds is None:
        return [f"{m.id}: unknown dataset {m.dataset_id}"]
    errors = []
    if ds.measure(m.measure_name) is None:
        errors.append(f"{m.id}: {ds.id} has no measure {m.measure_name!r}")
    errors += [f"{m.id}: {ds.id} has no filter {f!r}" for f in m.default_filters if ds.filter(f) is None]
    if ds.visibility != "public":
        errors.append(f"{m.id}: {ds.id} is internal")
    return errors
