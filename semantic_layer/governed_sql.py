"""Splice server-compiled governed parts into raw SQL.

Each part `governed_N` is compiled from its contract and hoisted, with its own CTEs renamed `governed_N__<name>`,
into one top-level WITH ahead of the outer SQL's CTEs. The outer SQL reads the parts by name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
import sqlglot.expressions as exp
from sqlglot.lineage import Node, lineage
from sqlglot.optimizer.qualify import qualify

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
    limited: bool = False  # the contract set a limit, so the part returns only its top rows


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

    for name, contract in governed.items():
        if "limit" in contract.model_fields_set and not contract.order_by:
            raise SpliceError(f"{name}: A governed part with a limit needs an order (order_by).")

    parts, ctes = {}, []
    for name, contract in governed.items():
        # The contract's default limit would silently cut the part; only an explicit one applies.
        limited = "limit" in contract.model_fields_set
        try:
            compiled = compile_query(contract, catalog, database, allow_identity=allow_identity, with_limit=limited)
        except CompileError as e:
            raise SpliceError(f"{name}: {e}") from e
        hoisted = _hoist(name, compiled)
        ctes += hoisted
        parts[name] = Part(contract=contract, compiled=compiled, outputs=_outputs(contract, catalog, compiled),
                           limited=limited)

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


UNTRACED = "Column origin could not be traced"


def _trace_tree(spliced: Spliced) -> exp.Query:
    """The spliced statement without the parts' definitions, so lineage stops at each part as if it were a table."""
    tree = sqlglot.parse_one(spliced.sql, dialect="snowflake")
    with_ = _with(tree)
    dropped = spliced.part_names | spliced.helper_names
    kept = [c for c in with_.expressions if c.alias not in dropped]
    tree.set("with", exp.With(expressions=kept, recursive=with_.args.get("recursive")) if kept else None)
    return tree


def _origin(root: Node, spliced: Spliced) -> tuple[str, str] | None:
    """(part, column) when every path from root ends at the same part column through plain references."""
    leaves = set()
    for node in root.walk():
        expression = node.expression
        if not node.downstream:
            if not (isinstance(expression, exp.Table) and isinstance(expression.this, exp.Identifier)
                    and not expression.args.get("db") and not expression.args.get("catalog")):
                return None
            leaves.add((expression.name.lower(), node.name.rpartition(".")[2].upper()))
        elif isinstance(expression, exp.Alias):
            if not isinstance(expression.this, exp.Column):
                return None
        elif not isinstance(expression, exp.Column):
            return None
    if len(leaves) != 1:
        return None
    part, column = next(iter(leaves))
    return (part, column) if part in spliced.part_names and column in spliced.parts[part].outputs else None


def _rows_modified(tree: exp.Query, spliced: Spliced) -> bool:
    """Whether the outer query reads anything but governed parts (a raw table, an outer CTE, a table function)."""
    tables = (t for t in tree.find_all(exp.Table)
              if not (unqualified_table(t) and t.name.lower() in spliced.part_names))
    return any(True for _ in tables) or any(True for _ in tree.find_all(exp.Lateral, exp.Unnest))


def label(spliced: Spliced, result_columns: list[str], *, reason: str) -> dict:
    """Mark each result column governed when it is one governed part column passed through unchanged
    (aliasing allowed), else ungoverned with reason. Never raises; a tracing failure leaves all ungoverned.
    `rows` is "modified" when the outer query reads a non-governed source or a part is limited, which also caps
    governance at partial."""
    ungoverned = {"governed": False, "reason": reason}
    try:
        schema = {name: {column: "VARCHAR" for column in part.outputs} for name, part in spliced.parts.items()}
        trace = _trace_tree(spliced)
        modified = _rows_modified(trace, spliced) or any(part.limited for part in spliced.parts.values())
        tree = qualify(trace, schema=schema, dialect="snowflake", validate_qualify_columns=False)
        folded: dict[str, list[str]] = {}
        for name in tree.named_selects:
            folded.setdefault(name.upper(), []).append(name)
        columns = {}
        for column in result_columns:
            names = folded.get(column.upper(), [])
            # Selects whose names differ only by case cannot be told apart in the result, so none is claimed.
            origin = None
            if len(names) == 1:
                root = lineage(exp.to_identifier(names[0], quoted=True).sql(), tree, schema=schema,
                               dialect="snowflake")
                origin = _origin(root, spliced)
            columns[column] = ({"governed": True, "definition": spliced.parts[origin[0]].outputs[origin[1]]}
                               if origin else dict(ungoverned))
    except Exception:
        modified = True
        columns = {c: {"governed": False, "reason": UNTRACED} for c in result_columns}
    governed = sum(1 for c in columns.values() if c["governed"])
    level = "none" if governed == 0 else "full" if governed == len(columns) else "partial"
    if modified and level == "full":
        level = "partial"
    return {"governance": level, "rows": "modified" if modified else "governed", "columns": columns}
