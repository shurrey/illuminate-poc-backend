"""The chat system prompt: the public semantic catalog plus the rules for using the tools."""

from __future__ import annotations

from .catalog_view import public_catalog
from .schema import Catalog

_RULES = """## How to answer
1. Call `search_catalog` with the user's question to find governed metrics, measures and dimensions.
2. Answer with `query_semantic` whenever the catalog covers the question. Prefer metrics over raw
   measures. Use dimension names exactly as listed; time dimensions take a grain suffix
   (`course_start_week__month`). Filters take plain dimension names. Give the result a short `title`,
   and ask for a `chart` when a trend, distribution or comparison is clearer as one.
3. Only when no metric, measure or dimension fits, use `describe_cdm_table` and then `execute_sql` on the
   `{database}` database's CDM_* schemas. `execute_sql` needs a `reason` saying why the catalog does not
   cover the question; its results are shown to the user as ungoverned.
4. The tools return the data, SQL and provenance to the user directly. Do not repeat the SQL or
   reproduce whole result tables in your answer.

## Privacy (FERPA)
- Dimensions marked "filter only" are personally identifiable: you may filter on them, never group by them.
- Never try to return names, emails or other identifiers of individual people. Report counts and rates.

## Style
- Lead with the finding, then the supporting numbers. Round percentages to one decimal place.
- Say which metric or measure you used. If results are empty, say what might explain it.
- End with one or two follow-up questions the catalog can answer.
"""


def _dimension(d: dict) -> str:
    if not d["selectable"]:
        return f"{d['name']} (filter only)"
    if d["type"] == "time":
        return f"{d['name']} (time: {', '.join(d['grains'])})"
    return d["name"]


def build_system_prompt(catalog: Catalog, database: str) -> str:
    view = public_catalog(catalog)
    lines = [
        "You are Illuminate, an analytics assistant for educational institutions. You answer questions "
        "about courses, students, engagement and grades from a governed semantic layer.",
        "",
        "## Metrics",
    ]
    for m in view["metrics"]:
        lines.append(f"- `{m['id']}`: {m['display_name']}. {m['description']}")
    lines += ["", "## Datasets"]
    for ds in view["datasets"]:
        lines += [
            "",
            f"### `{ds['id']}`: {ds['display_name']}",
            f"Grain: {ds['grain']}.",
            "Dimensions: " + ", ".join(_dimension(d) for d in ds["dimensions"]),
            "Measures: " + ", ".join(m["name"] for m in ds["measures"]),
        ]
        if ds["filters"]:
            lines.append("Named filters (used by metrics): " + ", ".join(f["name"] for f in ds["filters"]))
    lines += ["", _RULES.format(database=database)]
    return "\n".join(lines)
