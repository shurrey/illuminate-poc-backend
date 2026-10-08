"""Build tests/fixtures/cdm_dictionary.json from an Illuminate data-dictionary catalog export.

Usage: python scripts/build_dictionary_snapshot.py <catalog.json>
The input is the `catalog` shape served by /api/v1/dictionary (schema -> tables -> columns).
"""

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "cdm_dictionary.json"


def main(src: str) -> None:
    raw = json.loads(Path(src).read_text())
    catalog = raw.get("catalog", raw)
    snapshot = {
        schema: {
            table: {col: meta["dataType"] for col, meta in sorted(t["columns"].items())}
            for table, t in sorted(body["tables"].items())
        }
        for schema, body in sorted(catalog.items())
        if schema.startswith("CDM_")
    }
    OUT.write_text(json.dumps(snapshot, indent=1, sort_keys=True) + "\n")
    print(f"wrote {OUT} ({sum(len(t) for t in snapshot.values())} tables)")


if __name__ == "__main__":
    main(sys.argv[1])
