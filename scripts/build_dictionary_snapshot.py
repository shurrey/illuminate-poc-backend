"""Build the CDM dictionary test fixtures from Illuminate data-dictionary exports.

Usage: python scripts/build_dictionary_snapshot.py <catalog.json> <definitions.json>
<catalog.json> is the `catalog` export (schema -> tables -> columns); <definitions.json> is
the response of https://us.data.api.blackboard.com/api/v1/data/dictionary/definitions.
"""

import json
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "semantic_layer" / "data"


def main(catalog_src: str, definitions_src: str) -> None:
    raw = json.loads(Path(catalog_src).read_text())
    catalog = raw.get("catalog", raw)
    snapshot = {
        schema: {
            table: {col: meta["dataType"] for col, meta in sorted(t["columns"].items())}
            for table, t in sorted(body["tables"].items())
        }
        for schema, body in sorted(catalog.items())
        if schema.startswith("CDM_")
    }
    (FIXTURES / "cdm_dictionary.json").write_text(json.dumps(snapshot, indent=1, sort_keys=True) + "\n")

    definitions = json.loads(Path(definitions_src).read_text())
    pii = sorted({
        d["name"].upper() for d in definitions
        if d.get("name", "").upper().startswith("CDM_")
        and any(s.get("isPii") for s in d.get("technicalSpecifications") or [])
    })
    (FIXTURES / "cdm_pii_columns.json").write_text(json.dumps(pii, indent=1) + "\n")
    print(f"wrote {sum(len(t) for t in snapshot.values())} tables and {len(pii)} PII columns to {FIXTURES}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
