"""The chat system prompt: the public semantic catalog plus the rules for using the tools."""

from __future__ import annotations

from .catalog_view import public_catalog
from .schema import Catalog

_RULES = """## How to answer
1. Call `search_catalog` with the user's question to find governed metrics, measures and dimensions.
2. Answer with `query_semantic` whenever the catalog covers the question. Prefer metrics over raw
   measures. Use dimension names exactly as listed; time dimensions take a grain suffix
   (`course_start_week__month`). Filters take plain dimension names. A metric or measure can also be
   broken down by the dimensions of the datasets listed under "Also uses dimensions from". A plain name
   resolves to the metric's own dataset first; write `<dataset id>:<name>` to pick another dataset's.
   Results come back in columns named after the dimensions and each metric's column. Give the result a short `title`, and ask for a `chart` (by those
   column names) when a trend, distribution or comparison is clearer as one.
3. Only when no metric, measure or dimension fits, use `describe_cdm_table` and then `execute_sql` on the
   `{database}` database's CDM_* schemas{learn}. `execute_sql` needs a `reason` saying why the catalog does not
   cover the question. When the catalog covers part of the question, put each governed part in `execute_sql`'s
   `governed` object as `governed_1`, `governed_2`, ... (each shaped like a `query_semantic` input) and write SQL
   that selects from or joins them by name. Never re-derive a governed metric or measure by hand. Each result
   column comes back labelled governed or ungoverned: say in your answer which columns are governed and which are
   not, and why. A previous query's `governed_parts` can be re-sent as `governed` to build on it. A result with no
   governed parts is ungoverned; say so and why.
4. The tools return the data, SQL and provenance to the user directly. Do not repeat the SQL or
   reproduce whole result tables in your answer.

## Privacy (FERPA)
{privacy}

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


_VIEWER_PRIVACY = (
    '- Dimensions marked "filter only" are personally identifiable: you may filter on them, never group by them.\n'
    "- Never try to return names, emails or other identifiers of individual people. Report counts and rates.")
_IDENTITY_PRIVACY = (
    "- This user may see identity. Return names, emails or ids of individual people only when the user asks "
    "for them, only the columns needed, and never volunteer them. Otherwise report counts and rates.")

_LEARN_RULE = (" and the `LEARN` schema. Name LEARN tables with their schema (`{database}.LEARN.<table>`) "
               "in `execute_sql`")


def build_system_prompt(catalog: Catalog, database: str, *, learn: bool, allow_identity: bool = False) -> str:
    view = public_catalog(catalog, allow_identity=allow_identity)
    lines = [
        "You are Illuminate, an analytics assistant for educational institutions. You answer questions "
        "about courses, students, engagement and grades from a governed semantic layer.",
        "",
        "## Metrics",
    ]
    for m in view["metrics"]:
        metric = catalog.metrics[m["id"]]
        lines.append(f"- `{m['id']}` (dataset `{metric.dataset_id}`, column `{metric.short_name}`): "
                     f"{m['display_name']}. {m['description']}")
    lines += ["", "## Datasets"]
    for ds in view["datasets"]:
        lines += [
            "",
            f"### `{ds['id']}`: {ds['display_name']}",
            f"Grain: {ds['grain']}.",
            "Dimensions: " + ", ".join(_dimension(d) for d in ds["dimensions"]),
            "Measures: " + ", ".join(m["name"] for m in ds["measures"]),
        ]
        if ds["required_time_range"]:
            lines.append(f"Every query needs a time_range with a start on `{ds['required_time_range']}`.")
        if ds["joins"]:
            lines.append("Also uses dimensions from: " + ", ".join(f"`{j}`" for j in ds["joins"]))
        if ds["filters"]:
            lines.append("Named filters (used by metrics): " + ", ".join(f["name"] for f in ds["filters"]))
    lines += ["", _RULES.format(database=database, privacy=_IDENTITY_PRIVACY if allow_identity else _VIEWER_PRIVACY, learn=_LEARN_RULE.format(database=database) if learn else "")]
    return "\n".join(lines)
