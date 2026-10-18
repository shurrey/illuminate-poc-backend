"""The catalog as callers see it: public datasets and metrics.

Measures carry their expression and filters their condition, so a caller can show how a number is
calculated; base SQL and dimension columns stay private. Every published field is listed explicitly
so fields added to the definitions later stay private.
"""

from __future__ import annotations

from .compiler import joinable_datasets
from .schema import Catalog, Dataset

_DIMENSION_FIELDS = {"name", "type", "description", "grains", "synonyms"}
_MEASURE_FIELDS = {"name", "agg", "expr", "numerator", "denominator", "unit", "description", "synonyms"}
_METRIC_FIELDS = {
    "id", "display_name", "description", "owner", "authority", "last_reviewed",
    "measure", "default_filters", "synonyms", "example_questions",
}


def _dataset_view(ds: Dataset, joins: list[str], allow_identity: bool) -> dict:
    return {
        "id": ds.id,
        "display_name": ds.display_name,
        "description": ds.description,
        "grain": ds.grain,
        "domain": ds.domain,
        "dimensions": [
            d.model_dump(include=_DIMENSION_FIELDS) | {"selectable": allow_identity or not ds.is_pii(d.column)}
            for d in ds.dimensions
        ],
        "measures": [m.model_dump(include=_MEASURE_FIELDS) for m in ds.measures],
        "filters": [{"name": f.name, "description": f.description, "sql": f.sql} for f in ds.filters],
        "joins": joins,
        "required_time_range": ds.required_time_range,
    }


def public_catalog(catalog: Catalog, allow_identity: bool = False) -> dict:
    public = {i: d for i, d in catalog.datasets.items() if d.visibility == "public"}
    return {
        "datasets": [
            _dataset_view(d, [j for j in joinable_datasets(d, catalog) if j in public], allow_identity)
            for d in public.values()
        ],
        "metrics": [
            m.model_dump(mode="json", include=_METRIC_FIELDS)
            for m in catalog.metrics.values() if m.dataset_id in public
        ],
    }


def _dataset_summary(ds: dict) -> dict:
    return {"id": ds["id"], "display_name": ds["display_name"], "grain": ds["grain"]}


def entity_view(catalog: Catalog, entity_id: str, allow_identity: bool = False) -> dict | None:
    """One public dataset, metric, measure or dimension ('<dataset id>:<name>') as public_catalog shows it, with its
    kind; a metric or measure also lists the dimensions it can be broken down by. None when there is no such entity."""
    view = public_catalog(catalog, allow_identity)
    datasets = {d["id"]: d for d in view["datasets"]}
    if entity_id in datasets:
        return {"kind": "dataset", **datasets[entity_id]}
    metric = next((m for m in view["metrics"] if m["id"] == entity_id), None)
    if metric is not None:
        ds = datasets[metric["measure"].split(":", 1)[0]]
        return {"kind": "metric", **metric, "dataset": _dataset_summary(ds),
                "dimensions": ds["dimensions"], "joins": ds["joins"]}
    ds_id, _, name = entity_id.partition(":")
    ds = datasets.get(ds_id)
    if ds is None or not name:
        return None
    measure = next((m for m in ds["measures"] if m["name"] == name), None)
    if measure is not None:
        return {"kind": "measure", "id": entity_id, **measure, "dataset": _dataset_summary(ds),
                "dimensions": ds["dimensions"], "joins": ds["joins"]}
    dimension = next((d for d in ds["dimensions"] if d["name"] == name), None)
    if dimension is not None:
        return {"kind": "dimension", "id": entity_id, **dimension, "dataset": _dataset_summary(ds)}
    return None
