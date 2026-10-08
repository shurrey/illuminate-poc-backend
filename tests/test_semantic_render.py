import pytest

from semantic_layer.render import TemplateError, cte_name, render


def _no_refs(target):
    raise AssertionError(f"unexpected ref {target}")


def test_cte_name():
    assert cte_name("dataset.course_filters.v1") == "DS_COURSE_FILTERS_V1"


def test_renders_database_and_refs():
    seen = []
    out = render("SELECT * FROM {{ database }}.CDM_LMS.COURSE JOIN {{ ref('dataset.a.v1') }} ;",
                 "DB1", lambda t: seen.append(t) or "DS_A_V1")
    assert out == "SELECT * FROM DB1.CDM_LMS.COURSE JOIN DS_A_V1"
    assert seen == ["dataset.a.v1"]


@pytest.mark.parametrize("template", [
    "{{ ''.__class__.__mro__ }}",
    "{{ database.upper() }}",
    "{{ database | lower }}",
    "{% for x in [1] %}x{% endfor %}",
    "{{ where }}",
    "{{ ref(database) }}",
    "{{ ref('a', 'b') }}",
    "{{ lipsum() }}",
])
def test_rejects_constructs_outside_the_allowed_subset(template):
    with pytest.raises(TemplateError):
        render(template, "DB", _no_refs)


def test_syntax_errors_become_template_errors():
    with pytest.raises(TemplateError):
        render("{{ database ", "DB", _no_refs)
