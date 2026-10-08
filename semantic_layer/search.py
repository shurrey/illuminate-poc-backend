"""Rank catalog entries (metrics, measures, dimensions) against a natural-language question.

Deterministic token overlap: synonym and name matches outweigh description matches, and
governed metrics get a small boost so the model reaches for them before raw measures.
"""

from __future__ import annotations

import re

from .schema import Catalog

_STOPWORDS = frozenset(
    "a an and are as at by do does each for from give have how i in is it list many me much of on or our "
    "per show tell than that the their there this to us was we were what which who with".split()
)
_WEIGHTS = {"synonym": 3, "name": 2, "question": 1, "description": 1}
_METRIC_BOOST = 1


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower().replace("_", " "))
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words if w not in _STOPWORDS}


def _score(question: set[str], fields: dict[str, list[str]]) -> int:
    best: dict[str, int] = {}
    for kind, texts in fields.items():
        field_tokens = set()
        for text in texts:
            field_tokens |= _tokens(text)
        for token in question & field_tokens:
            best[token] = max(best.get(token, 0), _WEIGHTS[kind])
    return sum(best.values())


def search_catalog(question: str, catalog: Catalog, limit: int = 8) -> list[dict]:
    """Best matches first: {kind, id, display_name, description, score}; empty when nothing matches."""
    q = _tokens(question)
    hits = []
    public = {i: d for i, d in catalog.datasets.items() if d.visibility == "public"}
    for m in catalog.metrics.values():
        if m.dataset_id not in public:
            continue
        s = _score(q, {"synonym": m.synonyms, "name": [m.display_name, m.short_name],
                       "question": m.example_questions, "description": [m.description]})
        if s:
            hits.append({"kind": "metric", "id": m.id, "display_name": m.display_name,
                         "description": m.description, "score": s + _METRIC_BOOST})
    for ds in public.values():
        for meas in ds.measures:
            s = _score(q, {"synonym": meas.synonyms, "name": [meas.name], "description": [meas.description]})
            if s:
                hits.append({"kind": "measure", "id": f"{ds.id}:{meas.name}", "display_name": meas.name,
                             "description": meas.description or ds.display_name, "score": s})
        for dim in ds.dimensions:
            if ds.is_pii(dim.column):
                continue
            s = _score(q, {"synonym": dim.synonyms, "name": [dim.name], "description": [dim.description]})
            if s:
                hits.append({"kind": "dimension", "id": f"{ds.id}:{dim.name}", "display_name": dim.name,
                             "description": dim.description or ds.display_name, "score": s})
    hits.sort(key=lambda h: (-h["score"], h["kind"] != "metric", h["id"]))
    return hits[:limit]
