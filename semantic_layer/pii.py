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
