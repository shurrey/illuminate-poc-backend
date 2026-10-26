"""PII column names, and the aggregates that reduce PII columns to counts."""

import sqlglot.expressions as exp

PII_COLUMN_NAMES = frozenset({
    "FIRST_NAME", "LAST_NAME", "EMAIL", "SSN", "PHONE", "ADDRESS",
    "DOB", "DATE_OF_BIRTH", "PASSWORD", "PASSWD", "PHONE_NUMBER",
    "STREET_ADDRESS", "ZIP_CODE", "ZIPCODE",
})

# Aggregates whose result is a count, never a value from the column.
COUNTING_AGGREGATES = (exp.Count, exp.CountIf, exp.ApproxDistinct, exp.Hll)


def inside_counting_aggregate(node: exp.Expression, select_expression: exp.Expression) -> bool:
    """True when the nearest aggregate above node, within select_expression, is an unwindowed count."""
    while node is not None:
        if isinstance(node, exp.Window):
            return False
        if isinstance(node, COUNTING_AGGREGATES):
            return not isinstance(node.parent, exp.Window)
        if isinstance(node, exp.AggFunc) or node is select_expression:
            return False
        node = node.parent
    return False


# Steps that hand a value on unchanged, as far as an exact comparison or a count can tell. Casts are left out:
# a failed cast echoes the value in the warehouse's error.
_VALUE_PRESERVING = (exp.Paren, exp.Lower, exp.Upper, exp.Trim, exp.Coalesce, exp.Distinct, exp.JSONExtract)
_EXACT_COMPARISONS = (exp.EQ, exp.NEQ, exp.NullSafeEQ, exp.NullSafeNEQ, exp.Is, exp.In)


def used_exactly(node: exp.Expression) -> bool:
    """True when node's value reaches only an exact comparison (=, <>, IN, IS [NOT] NULL), a counting aggregate or a
    window's PARTITION BY, through value-preserving steps or a CASE/IFF result branch. A pattern, range or other
    function would let a caller read the value back one guess at a time."""
    while True:
        parent, key = node.parent, node.arg_key
        if isinstance(parent, _VALUE_PRESERVING):
            node = parent
        elif isinstance(parent, exp.If) and key in ("true", "false"):
            node = parent.parent if isinstance(parent.parent, exp.Case) else parent
        elif isinstance(parent, exp.Case) and key == "default":
            node = parent
        elif isinstance(parent, exp.Alias):
            node = parent
        elif isinstance(parent, exp.Select) and key == "expressions":
            # A subquery's projection compared as a set (IN, EXISTS) or as one value (=, <>).
            query = parent.parent if isinstance(parent.parent, exp.Subquery) else parent
            outer = query.parent
            return (isinstance(outer, exp.Exists) or (isinstance(outer, exp.In) and query.arg_key == "query")
                    or (isinstance(outer, (exp.EQ, exp.NEQ)) and query is not parent))
        else:
            return (isinstance(parent, _EXACT_COMPARISONS + COUNTING_AGGREGATES)
                    or (isinstance(parent, exp.Window) and key == "partition_by"))
