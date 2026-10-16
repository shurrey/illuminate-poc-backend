"""LEARN schema columns and PII flags from the committed data-dictionary snapshot."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_DATA = Path(__file__).resolve().parent / "data"


@lru_cache(maxsize=1)
def _dictionary() -> dict[str, frozenset[str]]:
    tables = json.loads((_DATA / "learn_dictionary.json").read_text())
    return {table: frozenset(columns) for table, columns in tables.items()}


@lru_cache(maxsize=1)
def _pii() -> frozenset[str]:
    return frozenset(json.loads((_DATA / "learn_pii_columns.json").read_text()))


def learn_columns(table: str) -> frozenset[str] | None:
    """Column names of LEARN.<table> (exact, upper-case snapshot names), or None when the snapshot lacks it."""
    return _dictionary().get(table)


def learn_pii(table: str, column: str) -> bool:
    """True when the dictionary flags LEARN.<table>.<column> as PII, or lists no such table or column."""
    columns = learn_columns(table)
    return columns is None or column not in columns or f"{table}.{column}" in _pii()
