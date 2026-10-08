"""Load the semantic catalog from canonical/datasets/**.yaml and canonical/metrics/*.yaml."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import ValidationError

from .schema import Catalog, Dataset, SemanticMetric

CANONICAL_DIR = Path(__file__).resolve().parent.parent / "canonical"


class CatalogError(ValueError):
    """A definition file is malformed or duplicates an id."""


def load_catalog(root: Path = CANONICAL_DIR) -> Catalog:
    datasets: dict[str, Dataset] = {}
    for path in sorted((root / "datasets").rglob("*.yaml")):
        try:
            ds = Dataset(**yaml.safe_load(path.read_text()))
        except ValidationError as e:
            raise CatalogError(f"{path}: {e}") from e
        if ds.id in datasets:
            raise CatalogError(f"{path}: duplicate dataset id {ds.id}")
        datasets[ds.id] = ds

    metrics: dict[str, SemanticMetric] = {}
    for path in sorted((root / "metrics").glob("*.yaml")):
        for raw in (yaml.safe_load(path.read_text()) or {}).get("metrics", []):
            try:
                m = SemanticMetric(**raw)
            except ValidationError as e:
                raise CatalogError(f"{path}: {e}") from e
            if m.id in metrics:
                raise CatalogError(f"{path}: duplicate metric id {m.id}")
            metrics[m.id] = m

    return Catalog(datasets=datasets, metrics=metrics)


@lru_cache(maxsize=1)
def default_catalog() -> Catalog:
    """The canonical catalog, loaded once per process."""
    return load_catalog()
