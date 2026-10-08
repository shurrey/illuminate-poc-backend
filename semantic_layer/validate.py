"""Offline validation of catalog definitions against a CDM dictionary snapshot.

Every column a dataset reads must exist in the snapshot, and every column a
dimension, measure, filter or entity names must be one the dataset outputs.
"""

from __future__ import annotations

import json
from pathlib import Path

import sqlglot
import sqlglot.expressions as exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify

from .compiler import CompileError, build_ctes
from .pii import PII_COLUMN_NAMES
from .schema import Catalog, Dataset, SemanticMetric

DICTIONARY_SNAPSHOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "cdm_dictionary.json"
_DB = "VALIDATION_DB"


def load_snapshot(path: Path = DICTIONARY_SNAPSHOT) -> dict:
    """The snapshot as a sqlglot schema: {database: {schema: {table: {column: type}}}}."""
    return {_DB: json.loads(path.read_text())}


def _columns_in(sql: str) -> set[str]:
    return {c.name.upper() for c in sqlglot.parse_one(sql, read="snowflake").find_all(exp.Column)}


def output_columns(ds: Dataset, catalog: Catalog, snapshot: dict) -> list[str]:
    """Qualify the dataset's SQL against the snapshot and return its output column names."""
    ctes = build_ctes(catalog, [ds.id], _DB)
    *deps, own = ctes.items()
    sql = own[1]
    if deps:
        sql = "WITH " + ",\n".join(f"{n} AS (\n{s}\n)" for n, s in deps) + f"\nSELECT * FROM ({sql})"
    tree = qualify(
        sqlglot.parse_one(sql, read="snowflake"),
        schema=snapshot, dialect="snowflake", validate_qualify_columns=True,
    )
    return [c.upper() for c in tree.named_selects]


def validate_dataset(ds: Dataset, catalog: Catalog, snapshot: dict) -> list[str]:
    try:
        outputs = set(output_columns(ds, catalog, snapshot))
    except (CompileError, OptimizeError, ParseError) as e:
        return [f"{ds.id}: {e}"]

    errors: list[str] = []

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
            need(_columns_in(m.expr), f"measure {m.name}")
    for f in ds.filters:
        need(_columns_in(f.sql), f"filter {f.name}")
    need({c.upper() for c in ds.pii_columns}, "pii_columns")
    undeclared = sorted((outputs & PII_COLUMN_NAMES) - {c.upper() for c in ds.pii_columns})
    if undeclared:
        errors.append(f"{ds.id}: outputs PII columns not listed in pii_columns: {undeclared}")
    return errors


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
