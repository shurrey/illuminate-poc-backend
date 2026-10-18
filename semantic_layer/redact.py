"""Redaction of shared query text for recipients without identity rights: literals and filter values are masked."""

from __future__ import annotations

from typing import Optional

import sqlglot
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

MASK = "…"
_STRING_TOKENS = {TokenType.STRING, TokenType.RAW_STRING, TokenType.NATIONAL_STRING, TokenType.BYTE_STRING,
                  TokenType.HEX_STRING, TokenType.BIT_STRING, TokenType.HEREDOC_STRING, TokenType.UNICODE_STRING}


def _row_limit(tokens: list, i: int) -> bool:
    """True when the number at tokens[i] is a LIMIT, TOP, OFFSET, FETCH or SAMPLE argument rather than data."""
    prev = [t.token_type for t in tokens[max(0, i - 3):i]][::-1] + [None] * 3
    if prev[0] in (TokenType.LIMIT, TokenType.TOP, TokenType.OFFSET):
        return True
    if prev[0] == TokenType.FETCH or (prev[0] in (TokenType.FIRST, TokenType.NEXT) and prev[1] == TokenType.FETCH):
        return True
    if prev[0] != TokenType.L_PAREN:
        return False
    # SAMPLE (n), SAMPLE BERNOULLI (n ROWS), ... SEED (n)
    before = tokens[i - 2] if i >= 2 else None
    return before is not None and (before.token_type == TokenType.TABLE_SAMPLE or (
        before.token_type == TokenType.VAR and (before.text.upper() in ("SEED", "REPEATABLE") or (
            i >= 3 and tokens[i - 3].token_type == TokenType.TABLE_SAMPLE))))


def _gap(text: str, edge: bool) -> str:
    """Text between tokens is whitespace and comments; a gap holding a comment collapses to a newline or space,
    or to nothing before the first token or after the last."""
    if not text.strip():
        return text
    if edge:
        return ""
    return "\n" if "\n" in text else " "


def redact_sql(sql: str) -> Optional[str]:
    """The SQL with every string and data number literal replaced by '…' and comments removed, formatting otherwise
    kept; None when it does not parse."""
    if not isinstance(sql, str):
        return None
    try:
        sqlglot.parse(sql, dialect="snowflake")
        tokens = sqlglot.Dialect.get_or_raise("snowflake").tokenize(sql)
    except (SqlglotError, ValueError):
        return None
    out, pos = [], 0
    for i, token in enumerate(tokens):
        out.append(_gap(sql[pos:token.start], edge=not out))
        masked = token.token_type in _STRING_TOKENS or (
            token.token_type == TokenType.NUMBER and not _row_limit(tokens, i))
        out.append(f"'{MASK}'" if masked else sql[token.start:token.end + 1])
        pos = token.end + 1
    out.append(_gap(sql[pos:], edge=True))
    return "".join(out)


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
