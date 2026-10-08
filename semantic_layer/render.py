"""Render a dataset's base_sql template.

Templates may contain only `{{ database }}` and `{{ ref('<dataset id>') }}`. Anything
else (statements, filters, attribute access, other names) is rejected before
rendering, and rendering itself runs in Jinja's sandbox.
"""

from __future__ import annotations

from typing import Callable

from jinja2 import StrictUndefined, nodes
from jinja2.exceptions import TemplateError as _JinjaError
from jinja2.sandbox import SandboxedEnvironment

_env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)


class TemplateError(ValueError):
    """The template uses a construct outside the allowed subset, or fails to render."""


def cte_name(dataset_id: str) -> str:
    """dataset.course_filters.v1 -> DS_COURSE_FILTERS_V1"""
    _, name, version = dataset_id.split(".")
    return f"DS_{name}_{version}".upper()


def _check(node: nodes.Node) -> None:
    if isinstance(node, (nodes.Template, nodes.Output, nodes.TemplateData)):
        pass
    elif isinstance(node, nodes.Name):
        if node.name != "database":
            raise TemplateError(f"template variable {node.name!r} is not allowed")
        return
    elif isinstance(node, nodes.Call):
        is_ref = isinstance(node.node, nodes.Name) and node.node.name == "ref"
        single_const = (
            len(node.args) == 1 and isinstance(node.args[0], nodes.Const)
            and isinstance(node.args[0].value, str)
        )
        if not (is_ref and single_const and not node.kwargs and not node.dyn_args and not node.dyn_kwargs):
            raise TemplateError("only ref('<dataset id>') calls are allowed")
        return
    else:
        raise TemplateError(f"template construct {type(node).__name__} is not allowed")
    for child in node.iter_child_nodes():
        _check(child)


def render(template: str, database: str, on_ref: Callable[[str], str]) -> str:
    """on_ref receives each referenced dataset id and returns the SQL that replaces it."""
    try:
        _check(_env.parse(template))
        return _env.from_string(template).render(database=database, ref=on_ref).strip().rstrip(";").rstrip()
    except _JinjaError as e:
        raise TemplateError(str(e)) from e
