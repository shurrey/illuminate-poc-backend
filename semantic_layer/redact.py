"""Redaction of shared query text for recipients without identity rights: literals and filter values are masked."""

from __future__ import annotations

from typing import Optional

import sqlglot
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

MASK = "…"
_STRING_TOKENS = {TokenType.STRING, TokenType.RAW_STRING, TokenType.NATIONAL_STRING, TokenType.BYTE_STRING,
                  TokenType.HEX_STRING, TokenType.BIT_STRING, TokenType.HEREDOC_STRING, TokenType.UNICODE_STRING}


def redact_sql(sql: str) -> Optional[str]:
    """The SQL with every string literal replaced by '…', formatting otherwise kept; None when it does not parse."""
    if not isinstance(sql, str):
        return None
    try:
        sqlglot.parse(sql, dialect="snowflake")
        tokens = sqlglot.Dialect.get_or_raise("snowflake").tokenize(sql)
    except (SqlglotError, ValueError):
        return None
    out, pos = [], 0
    for token in tokens:
        if token.token_type in _STRING_TOKENS:
            out += [sql[pos:token.start], f"'{MASK}'"]
            pos = token.end + 1
    return "".join(out) + sql[pos:]


def redact_contract(contract) -> dict:
    """A copy of the contract with every filter value replaced by "…"; malformed parts are dropped, never echoed."""
    if not isinstance(contract, dict):
        return {}
    out = dict(contract)
    if "filters" in contract:
        filters = contract["filters"] if isinstance(contract["filters"], list) else []
        out["filters"] = [
            {**f, "values": [MASK] * len(f["values"]) if isinstance(f.get("values"), list) else []}
            for f in filters if isinstance(f, dict)
        ]
    return out
