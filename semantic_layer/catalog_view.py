"""The catalog as callers see it: public datasets and metrics, without SQL or column names.

Every published field is listed explicitly so fields added to the definitions later stay private.
"""

from __future__ import annotations

from .compiler import joinable_datasets
from .schema import Catalog, Dataset

_DIMENSION_FIELDS = {"name", "type", "description", "grains", "synonyms"}
_MEASURE_FIELDS = {"name", "agg", "numerator", "denominator", "unit", "description", "synonyms"}
_METRIC_FIELDS = {
    "id", "display_name", "description", "owner", "authority", "last_reviewed",
    "measure", "default_filters", "synonyms", "example_questions",
}


def _dataset_view(ds: Dataset, joins: list[str]) -> dict:
    return {
        "id": ds.id,
        "display_name": ds.display_name,
        "description": ds.description,
        "grain": ds.grain,
        "domain": ds.domain,
        "dimensions": [
            d.model_dump(include=_DIMENSION_FIELDS) | {"selectable": not ds.is_pii(d.column)}
            for d in ds.dimensions
        ],
        "measures": [m.model_dump(include=_MEASURE_FIELDS) for m in ds.measures],
        "filters": [{"name": f.name, "description": f.description} for f in ds.filters],
        "joins": joins,
        "required_time_range": ds.required_time_range,
    }


def public_catalog(catalog: Catalog) -> dict:
    public = {i: d for i, d in catalog.datasets.items() if d.visibility == "public"}
    return {
        "datasets": [
            _dataset_view(d, [j for j in joinable_datasets(d, catalog) if j in public]) for d in public.values()
        ],
        "metrics": [
            m.model_dump(mode="json", include=_METRIC_FIELDS)
            for m in catalog.metrics.values() if m.dataset_id in public
        ],
    }
