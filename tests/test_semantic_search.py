from semantic_layer.catalog import load_catalog
from semantic_layer.search import search_catalog
from tests.semantic_fixtures import catalog

CATALOG = load_catalog()


def _ids(question, **kw):
    return [hit["id"] for hit in search_catalog(question, CATALOG, **kw)]


def test_metric_synonyms_rank_first():
    assert _ids("how many active learners do we have")[0] == "metric.active_students.v1"


def test_grade_questions_find_grade_metrics():
    hits = _ids("what is the average grade by term")
    assert "metric.average_grade.v1" in hits[:3]


def test_dimension_hits_carry_their_dataset():
    hits = search_catalog("department", CATALOG)
    dims = [h for h in hits if h["kind"] == "dimension"]
    assert dims and all(h["id"].startswith("dataset.") and ":" in h["id"] for h in dims)
    assert any(h["id"].endswith(":ih_level_3") for h in dims)


def test_results_are_limited_and_scored_descending():
    hits = search_catalog("courses students grades activity", CATALOG, limit=5)
    assert len(hits) == 5
    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)


def test_unrelated_question_returns_nothing():
    assert search_catalog("weather in paris tomorrow", CATALOG) == []


def test_internal_datasets_are_not_searchable():
    from tests.semantic_fixtures import dataset
    hidden = dataset(visibility="internal")
    assert search_catalog("enrollments", catalog(hidden, metrics=())) == []


def test_hits_name_their_dataset():
    hit = search_catalog("average grade", CATALOG)[0]
    assert hit["dataset"] == CATALOG.metrics[hit["id"]].dataset_id
