"""Build the dictionary snapshots in semantic_layer/data from Illuminate data-dictionary exports.

Usage: python scripts/build_dictionary_snapshot.py [<catalog.json> <definitions.json>] [--learn <definitions.json>]
<catalog.json> is the `catalog` export (schema -> tables -> columns); <definitions.json> is
the response of https://us.data.api.blackboard.com/api/v1/data/dictionary/definitions.
"""

import argparse
import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "semantic_layer" / "data"


def _is_pii(definition: dict) -> bool:
    return any(s.get("isPii") for s in definition.get("technicalSpecifications") or [])


def build_cdm(catalog_src: str, definitions_src: str) -> None:
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
        if d.get("name", "").upper().startswith("CDM_") and _is_pii(d)
    })
    (FIXTURES / "cdm_pii_columns.json").write_text(json.dumps(pii, indent=1) + "\n")
    print(f"wrote {sum(len(t) for t in snapshot.values())} tables and {len(pii)} PII columns to {FIXTURES}")


def build_learn(definitions_src: str) -> None:
    """LEARN.TABLE.COLUMN definitions to {TABLE: {COLUMN: TYPE}} and a sorted list of PII "TABLE.COLUMN"."""
    tables: dict[str, dict[str, str]] = {}
    pii = set()
    for d in json.loads(Path(definitions_src).read_text()):
        parts = d.get("name", "").upper().split(".")
        if len(parts) != 3 or parts[0] != "LEARN":
            continue
        tables.setdefault(parts[1], {})[parts[2]] = d.get("columnDataType") or ""
        if _is_pii(d):
            pii.add(f"{parts[1]}.{parts[2]}")
    (FIXTURES / "learn_dictionary.json").write_text(json.dumps(tables, indent=1, sort_keys=True) + "\n")
    (FIXTURES / "learn_pii_columns.json").write_text(json.dumps(sorted(pii), indent=1) + "\n")
    print(f"wrote {len(tables)} LEARN tables, {sum(len(t) for t in tables.values())} columns "
          f"and {len(pii)} PII columns to {FIXTURES}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("cdm", nargs="*", metavar="<catalog.json> <definitions.json>")
    parser.add_argument("--learn", metavar="<definitions.json>")
    args = parser.parse_args()
    if len(args.cdm) not in (0, 2) or not (args.cdm or args.learn):
        parser.error("give <catalog.json> <definitions.json>, --learn <definitions.json>, or both")
    if args.cdm:
        build_cdm(*args.cdm)
    if args.learn:
        build_learn(args.learn)
