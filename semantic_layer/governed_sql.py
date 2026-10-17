"""Splice server-compiled governed parts into raw SQL.

Each part `governed_N` is compiled from its contract and hoisted, with its own CTEs renamed `governed_N__<name>`,
into one top-level WITH ahead of the outer SQL's CTEs. The outer SQL reads the parts by name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
import sqlglot.expressions as exp

from .compiler import CompileError, _output_name, compile_query
from .contract import CompiledQuery, QueryContract
from .schema import Catalog

MAX_PARTS = 5
_PART_NAME = re.compile(r"governed_[1-9][0-9]?")


class SpliceError(ValueError):
    """The outer SQL or its governed parts cannot be spliced."""


@dataclass(frozen=True)
class Part:
    contract: QueryContract
    compiled: CompiledQuery
    outputs: dict[str, str]  # Snowflake-folded output column name -> definition ref


@dataclass(frozen=True)
class Spliced:
    sql: str
    parts: dict[str, Part]
    part_names: frozenset[str]
    helper_names: frozenset[str]  # the parts' hoisted CTEs; only the parts may read them


def _folded(identifier: exp.Identifier) -> str:
    return identifier.name if identifier.args.get("quoted") else identifier.name.upper()


def _with(node: exp.Expression) -> exp.With | None:
    return node.args.get("with") or node.args.get("with_")


def unqualified_table(table: exp.Table) -> bool:
    return isinstance(table.this, exp.Identifier) and not table.args.get("db") and not table.args.get("catalog")


def _parse_outer(outer_sql: str) -> exp.Query:
    try:
        statements = sqlglot.parse(outer_sql.strip(), dialect="snowflake")
    except Exception as e:
        raise SpliceError(f"SQL parse error: {e}") from e
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise SpliceError("Submit one SELECT or WITH statement.")
    return statements[0]


def _check_outer(outer: exp.Query, names: list[str]) -> None:
    for cte in outer.find_all(exp.CTE):
        if cte.alias.lower().startswith("governed_"):
            raise SpliceError(f"The SQL defines {cte.alias}; governed_* names are reserved for governed parts.")
    referenced = {_folded(t.this) for t in outer.find_all(exp.Table)
                  if unqualified_table(t) and _folded(t.this).upper().startswith("GOVERNED_")}
    for name in names:
        if name.upper() not in referenced:
            raise SpliceError(f"{name} is declared but the SQL never selects from it.")
    undeclared = sorted(referenced - {n.upper() for n in names})
    if undeclared:
        raise SpliceError(f"{undeclared[0].lower()} is referenced but not declared as a governed part.")


def _shadowed(table: exp.Table, name: str, root: exp.Expression) -> bool:
    """Whether a WITH nested between table and root (inclusive) defines name."""
    node = table
    while node is not None:
        with_ = _with(node)
        if with_ is not None and any(_folded(c.args["alias"].this) == name for c in with_.expressions):
            return True
        if node is root:
            return False
        node = node.parent
    return False


def _rename_references(root: exp.Expression, renames: dict[str, str]) -> None:
    """Point root's references to the part's top-level CTEs at their new names.

    A renamed table keeps its old name as its alias, so column qualifiers such as g1.x still bind.
    """
    for table in list(root.find_all(exp.Table)):
        if not unqualified_table(table):
            continue
        old = _folded(table.this)
        if old not in renames or _shadowed(table, old, root):
            continue
        if not table.alias:
            table.set("alias", exp.TableAlias(this=table.this.copy()))
        table.set("this", exp.to_identifier(renames[old]))


def _hoist(name: str, compiled: CompiledQuery) -> list[exp.CTE]:
    """The part's CTEs, renamed with its prefix, followed by the part itself as `name AS (...)`."""
    tree = sqlglot.parse_one(compiled.sql, dialect="snowflake")
    with_ = _with(tree)
    tree.set("with", None)
    helpers = list(with_.expressions) if with_ is not None else []
    renames = {_folded(c.args["alias"].this): f"{name}__{c.alias}" for c in helpers}
    for cte in helpers:
        _rename_references(cte.this, renames)
        cte.set("alias", exp.TableAlias(this=exp.to_identifier(renames[_folded(cte.args["alias"].this)])))
    _rename_references(tree, renames)
    return helpers + [exp.CTE(this=tree, alias=exp.TableAlias(this=exp.to_identifier(name)))]


def _outputs(contract: QueryContract, catalog: Catalog, compiled: CompiledQuery) -> dict[str, str]:
    refs = {_output_name(d).lower(): f"dimension:{d}" for d in contract.dimensions}
    refs |= {catalog.metrics[m].short_name.lower(): m for m in contract.metrics}
    refs |= {m.partition(":")[2].lower(): m for m in contract.measures}
    select = sqlglot.parse_one(compiled.sql, dialect="snowflake")
    out = {}
    for projection in select.expressions:
        identifier = projection.args["alias"] if isinstance(projection, exp.Alias) else projection.this
        if identifier.name.lower() not in refs:
            raise SpliceError(f"Output {identifier.name} does not map to a definition in the contract.")
        out[_folded(identifier)] = refs[identifier.name.lower()]
    return out


def splice(outer_sql: str, governed: dict[str, QueryContract], catalog: Catalog, database: str, *,
           allow_identity: bool) -> Spliced:
    """Compile each governed part and splice it into outer_sql. Raises SpliceError, before compiling for
    malformed input, and with the part's name for a compile refusal."""
    for name in governed:
        if not _PART_NAME.fullmatch(name):
            raise SpliceError(f"Governed part name {name!r} must be governed_1 to governed_99.")
    if len(governed) > MAX_PARTS:
        raise SpliceError(f"At most {MAX_PARTS} governed parts are allowed; got {len(governed)}.")
    outer = _parse_outer(outer_sql)
    _check_outer(outer, list(governed))

    parts, ctes = {}, []
    for name, contract in governed.items():
        try:
            compiled = compile_query(contract, catalog, database, allow_identity=allow_identity)
        except CompileError as e:
            raise SpliceError(f"{name}: {e}") from e
        hoisted = _hoist(name, compiled)
        ctes += hoisted
        parts[name] = Part(contract=contract, compiled=compiled, outputs=_outputs(contract, catalog, compiled))

    helper_names = frozenset(cte.alias for cte in ctes) - parts.keys()
    outer_with = _with(outer)
    outer.set("with", exp.With(expressions=ctes + (list(outer_with.expressions) if outer_with else []),
                               recursive=bool(outer_with and outer_with.args.get("recursive"))))
    sql = outer.sql(dialect="snowflake", pretty=True)
    try:
        sqlglot.parse_one(sql, dialect="snowflake")
    except Exception as e:
        raise SpliceError(f"The spliced SQL does not parse: {e}") from e
    return Spliced(sql=sql, parts=parts, part_names=frozenset(parts), helper_names=helper_names)
