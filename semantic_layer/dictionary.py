"""Column metadata for CDM tables from the Blackboard data dictionary, fetched once per process."""

from __future__ import annotations

import json
import logging
import os
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


def describe_table(schema: str, table: str) -> Optional[list[dict]]:
    """Columns of SCHEMA.TABLE, or None when the dictionary has no such table or cannot be reached."""
    try:
        return _tables().get((schema.upper(), table.upper()))
    except Exception as e:
        logger.warning("Data dictionary unavailable: %s", e)
        return None
