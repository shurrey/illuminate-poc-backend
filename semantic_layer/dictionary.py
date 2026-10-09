"""Column metadata for CDM tables from the Blackboard data dictionary, fetched once per process."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from functools import lru_cache
from typing import Optional

logger = logging.getLogger("API-PROXY")

_BASE_URL = os.environ.get("DATA_DICTIONARY_URL", "https://us.data.api.blackboard.com/api/v1/data/dictionary")
_TIMEOUT = int(os.environ.get("DATA_DICTIONARY_TIMEOUT", "15"))


@lru_cache(maxsize=1)
def _tables() -> dict[tuple[str, str], list[dict]]:
    with urllib.request.urlopen(f"{_BASE_URL}/definitions", timeout=_TIMEOUT) as resp:
        definitions = json.loads(resp.read().decode())
    tables: dict[tuple[str, str], list[dict]] = {}
    for d in definitions:
        parts = d.get("name", "").upper().split(".")
        if len(parts) != 3 or not parts[0].startswith("CDM_"):
            continue
        tables.setdefault((parts[0], parts[1]), []).append({
            "name": parts[2],
            "type": d.get("columnDataType", ""),
            "pii": any(s.get("isPii") for s in d.get("technicalSpecifications") or []),
            "description": (d.get("text") or "")[:200],
        })
    return tables


_RETRY_SECONDS = 60
_failed_at: Optional[float] = None


def describe_table(schema: str, table: str) -> Optional[list[dict]]:
    """Columns of SCHEMA.TABLE, or None when the dictionary has no such table or cannot be reached.

    After a failed fetch, calls return None for a minute rather than waiting on the dictionary again.
    """
    global _failed_at
    if _failed_at is not None and time.monotonic() - _failed_at < _RETRY_SECONDS:
        return None
    try:
        tables = _tables()
    except Exception as e:
        logger.warning("Data dictionary unavailable: %s", e)
        _failed_at = time.monotonic()
        return None
    _failed_at = None
    return tables.get((schema.upper(), table.upper()))
