"""The catalog as callers see it: public datasets and metrics, without SQL or column names."""

from __future__ import annotations

from .schema import Catalog, Dataset


def _dataset_view(ds: Dataset) -> dict:
    pii = set(ds.pii_columns)
    return {
        "id": ds.id,
        "display_name": ds.display_name,
        "description": ds.description,
        "grain": ds.grain,
        "domain": ds.domain,
        "dimensions": [
            d.model_dump(exclude={"column"}) | {"selectable": d.column not in pii}
            for d in ds.dimensions
        ],
        "measures": [m.model_dump(exclude={"expr"}) for m in ds.measures],
        "filters": [{"name": f.name, "description": f.description} for f in ds.filters],
    }


def public_catalog(catalog: Catalog) -> dict:
    public = {i: d for i, d in catalog.datasets.items() if d.visibility == "public"}
    return {
        "datasets": [_dataset_view(d) for d in public.values()],
        "metrics": [
            m.model_dump(mode="json") for m in catalog.metrics.values() if m.dataset_id in public
        ],
    }
