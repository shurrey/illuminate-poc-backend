# Semantic Layer Phase 1 (Walking Skeleton) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the metric-only semantic layer's foundation with a dataset model, a contract compiler, offline validation, the first ported bbd-analytics dataset, and a `POST /api/v1/semantic/compile` endpoint.

**Architecture:** Datasets are YAML files holding Jinja-templated SQL over `CDM_*` plus typed dimensions, measures and filters. The compiler inlines datasets as CTEs (dependencies first) and builds the outer aggregate query as a sqlglot AST, so filter values are typed literals. A validator qualifies every dataset's SQL against a committed CDM dictionary snapshot to catch unknown columns offline. The legacy `semantic_layer/models.py`, `engine.py` and `tool.py` are untouched; they keep serving chat until Phase 8.

**Tech Stack:** Python 3.11, Pydantic v2, Jinja2 `SandboxedEnvironment`, sqlglot (Snowflake dialect, `qualify`), FastAPI, pytest.

**Spec:** `docs/superpowers/specs/2026-10-08-semantic-layer-bbd-parity-design.md` (§3, §5.1–5.3, §8 Phase 1). Roadmap for later phases: `docs/superpowers/plans/2026-10-08-semantic-layer-roadmap.md`.

**Repo:** `illuminate-conversational-intelligence`. Every path below is relative to its root.

## Global Constraints

- Lambda runtime is Python 3.11 (`cdk/lib/api/lambda-proxy.ts:113`); no 3.12-only syntax. Verified with sqlglot 26.0.0 (the `requirements-lambda.txt` floor) and 30.21.0.
- One task = one PR, branched from the latest `origin/main`. Open the PR, then stop until it is approved and merged. Never stack branches; never merge `main` into an open PR branch (rebase only if `main` is actually needed).
- PR descriptions start with `**Claude:**` on the first line and end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`. Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Spec and plan documents stay out of code PRs.
- Code comments: only non-obvious behaviour, contracts the signature can't express, or a one-line why. Module docstring ≤ 5 lines, function docstring ≤ 4. No narrative, no ticket or requirement IDs.
- bbd-analytics (`../illuminate-code/bbdata-bbd-analytics`) is read-only. Defects found while porting are corrected in the dataset and recorded in spec §4.1.
- Ported SQL drops every `tenant_id` and `inferred_ind` reference: neither column exists in the POC CDM (verified against the live schema and the dictionary snapshot).
- Dataset SQL may use only `{{ database }}` and `{{ ref('<dataset id>') }}`.
- Do not modify `semantic_layer/models.py`, `engine.py`, `tool.py`, `canonical/metrics.yaml` or `canonical/glossary.yaml` in this phase.

**Environment setup (once):**

```bash
python3.11 -m venv .venv   # or: uv venv -p 3.11 .venv
.venv/bin/pip install -r requirements-lambda.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q tests   # baseline: 15 passed
```

## Review Focus

Inputs the spec implies but its sections don't spell out; each is pinned by a test in the owning task.

1. An `eq` filter of `"2026-01-01"` on a timestamp-backed time dimension should match that whole day, not only midnight (Task 3: `test_time_dimension_filters_compare_whole_days`).
2. Requesting a metric and a measure that produce the same output name should fail clearly, not return one silently overwritten column (Task 3: `test_metric_and_measure_with_the_same_output_name_collide`).
3. Filtering on `start_date__month` (a grain-suffixed name) should be rejected with a message naming it, since filters take plain dimension names (Task 3: `test_grain_suffix_is_not_a_filter_dimension`).
4. Ordering by a grain-suffixed dimension that is selected should work (Task 3: `test_order_by_a_grain_dimension`).
5. A dataset whose `base_sql` ends with a `-- comment` line must still compile to valid SQL once wrapped in a CTE (Task 3: `test_base_sql_ending_in_a_line_comment_still_compiles`).

---

### Task 1 (PR 1a): Definition and contract models

**Files:**
- Create: `semantic_layer/schema.py`
- Create: `semantic_layer/contract.py`
- Create: `tests/semantic_fixtures.py`
- Test: `tests/test_semantic_schema.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `semantic_layer.schema`: `Dataset`, `DatasetDimension`, `Measure`, `DatasetFilter`, `EntityKey`, `SemanticMetric`, `Catalog(datasets: dict[str, Dataset], metrics: dict[str, SemanticMetric])`. `Dataset.dimension(name) / .measure(name) / .filter(name) -> Optional[...]`. `SemanticMetric.dataset_id`, `.measure_name`, `.short_name` (`metric.active_students.v1` → `active_students`).
  - `semantic_layer.contract`: `QueryContract`, `ContractFilter`, `TimeRange`, `OrderBy`, `Provenance(governed, datasets, metrics, measures)`, `CompiledQuery(sql, provenance)`.
  - `tests.semantic_fixtures`: `ENROLLMENTS` (dataset), `STUDENT_ENROLLMENTS` (metric), `catalog(*datasets, metrics=...)`, `dataset(**overrides)` (rebuilds and re-validates; `model_copy(update=...)` skips validation, so don't use it).

- [ ] **Step 1: Branch from the latest `main`**

```bash
git fetch origin && git checkout -b feat/semantic-models origin/main
```

- [ ] **Step 2: Write the shared test fixtures**

```python
# tests/semantic_fixtures.py
"""Small in-memory catalogs for compiler and validator tests."""

from semantic_layer.schema import Catalog, Dataset, SemanticMetric

ENROLLMENTS = Dataset(
    id="dataset.enrollments.v1",
    display_name="Enrollments",
    description="test",
    grain="one row per enrollment",
    domain="test",
    source="test",
    base_sql="SELECT pc.ID, pc.PERSON_ID, pc.COURSE_ID, pc.COURSE_ROLE, pc.ENROLLMENT_TIME, p.EMAIL "
             "FROM {{ database }}.CDM_LMS.PERSON_COURSE pc JOIN {{ database }}.CDM_LMS.PERSON p ON p.ID = pc.PERSON_ID",
    entities=[{"name": "person_course", "column": "ID", "type": "primary"}],
    dimensions=[
        {"name": "course_role", "column": "COURSE_ROLE", "type": "categorical"},
        {"name": "enrolled_at", "column": "ENROLLMENT_TIME", "type": "time", "grains": ["day", "month"]},
        {"name": "email", "column": "EMAIL", "type": "categorical"},
    ],
    measures=[
        {"name": "enrollments", "agg": "count", "expr": "ID"},
        {"name": "people", "agg": "count_distinct", "expr": "PERSON_ID"},
        {"name": "per_person", "agg": "ratio", "numerator": "enrollments", "denominator": "people"},
    ],
    filters=[{"name": "students", "sql": "COURSE_ROLE = 'S'"}],
    pii_columns=["EMAIL"],
)

STUDENT_ENROLLMENTS = SemanticMetric(
    id="metric.student_enrollments.v1",
    display_name="Student enrollments",
    description="test",
    owner="Blackboard",
    authority="vendor-canonical",
    last_reviewed="2026-10-08",
    measure="dataset.enrollments.v1:enrollments",
    default_filters=["students"],
)


def catalog(*datasets: Dataset, metrics: tuple[SemanticMetric, ...] = (STUDENT_ENROLLMENTS,)) -> Catalog:
    datasets = datasets or (ENROLLMENTS,)
    return Catalog(datasets={d.id: d for d in datasets}, metrics={m.id: m for m in metrics})


def dataset(**overrides) -> Dataset:
    return Dataset(**(ENROLLMENTS.model_dump() | overrides))
```

- [ ] **Step 3: Write the failing tests**

```python
# tests/test_semantic_schema.py
import pytest
from pydantic import ValidationError

from semantic_layer.contract import ContractFilter, QueryContract, TimeRange
from semantic_layer.schema import DatasetDimension, Measure, SemanticMetric
from tests.semantic_fixtures import ENROLLMENTS


def test_ratio_measure_requires_numerator_and_denominator():
    with pytest.raises(ValidationError, match="ratio needs numerator"):
        Measure(name="r", agg="ratio", numerator="a")


def test_plain_measure_rejects_ratio_fields():
    with pytest.raises(ValidationError, match="needs expr only"):
        Measure(name="m", agg="sum", expr="X", numerator="a")


def test_grains_rejected_on_non_time_dimension():
    with pytest.raises(ValidationError, match="only valid on time"):
        DatasetDimension(name="d", column="C", type="categorical", grains=["day"])


def test_dataset_rejects_duplicate_dimension_and_measure_names():
    raw = ENROLLMENTS.model_dump()
    raw["measures"].append({"name": "course_role", "agg": "count", "expr": "ID"})
    with pytest.raises(ValidationError, match="must be unique"):
        type(ENROLLMENTS)(**raw)


def test_dataset_rejects_ratio_over_unknown_measure():
    raw = ENROLLMENTS.model_dump()
    raw["measures"].append({"name": "bad", "agg": "ratio", "numerator": "nope", "denominator": "people"})
    with pytest.raises(ValidationError, match="non-ratio measures"):
        type(ENROLLMENTS)(**raw)


def test_unknown_fields_are_rejected():
    raw = ENROLLMENTS.model_dump() | {"tenant_id": "x"}
    with pytest.raises(ValidationError):
        type(ENROLLMENTS)(**raw)


def test_metric_measure_must_name_a_dataset_measure():
    with pytest.raises(ValidationError):
        SemanticMetric(id="metric.x.v1", display_name="x", description="x", owner="o", authority="a",
                       last_reviewed="2026-01-01", measure="enrollments")


@pytest.mark.parametrize("op,values", [("eq", []), ("between", [1]), ("is_null", [1]), ("in", [])])
def test_filter_arity_is_enforced(op, values):
    with pytest.raises(ValidationError, match="takes"):
        ContractFilter(dimension="d", op=op, values=values)


def test_filter_keeps_json_true_as_bool():
    assert ContractFilter(dimension="d", op="eq", values=[True]).values == [True]


def test_contract_needs_a_metric_or_measure():
    with pytest.raises(ValidationError, match="at least one"):
        QueryContract(dimensions=["course_role"])


@pytest.mark.parametrize("limit", [0, 1001])
def test_contract_limit_bounds(limit):
    with pytest.raises(ValidationError):
        QueryContract(metrics=["metric.x.v1"], limit=limit)


def test_time_range_rejects_inverted_bounds():
    with pytest.raises(ValidationError, match="after end"):
        TimeRange(dimension="d", start="2026-02-01", end="2026-01-01")
```

- [ ] **Step 4: Run them to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_semantic_schema.py`
Expected: collection error, `ModuleNotFoundError: No module named 'semantic_layer.schema'`.

- [ ] **Step 5: Implement the definition models**

```python
# semantic_layer/schema.py
"""Semantic model definitions: datasets, their dimensions and measures, and metrics.

A dataset is a named SQL relation over CDM_* at a declared grain. A metric is a
governed, named measure of one dataset with optional measure-scoped filters.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

DATASET_ID_PATTERN = r"^dataset\.[a-z][a-z0-9_]*\.v[0-9]+$"
METRIC_ID_PATTERN = r"^metric\.[a-z][a-z0-9_]*\.v[0-9]+$"
NAME_PATTERN = r"^[a-z][a-z0-9_]*$"

TimeGrain = Literal["hour", "day", "week", "month", "quarter", "year"]
Aggregation = Literal[
    "sum", "count", "count_distinct", "avg", "min", "max", "median", "ratio"
]


class _Definition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EntityKey(_Definition):
    name: str = Field(pattern=NAME_PATTERN)
    column: str
    type: Literal["primary", "foreign"]


class DatasetDimension(_Definition):
    name: str = Field(pattern=NAME_PATTERN)
    column: str
    type: Literal["categorical", "time", "boolean", "numeric"]
    description: str = ""
    grains: list[TimeGrain] = Field(default_factory=list)
    synonyms: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _grains_only_on_time(self) -> "DatasetDimension":
        if self.grains and self.type != "time":
            raise ValueError(f"dimension {self.name}: grains are only valid on time dimensions")
        return self


class Measure(_Definition):
    name: str = Field(pattern=NAME_PATTERN)
    agg: Aggregation
    expr: Optional[str] = None
    numerator: Optional[str] = None
    denominator: Optional[str] = None
    unit: str = ""
    description: str = ""
    synonyms: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _shape_matches_agg(self) -> "Measure":
        if self.agg == "ratio":
            if not (self.numerator and self.denominator) or self.expr:
                raise ValueError(f"measure {self.name}: ratio needs numerator and denominator, not expr")
        elif not self.expr or self.numerator or self.denominator:
            raise ValueError(f"measure {self.name}: {self.agg} needs expr only")
        return self


class DatasetFilter(_Definition):
    name: str = Field(pattern=NAME_PATTERN)
    sql: str
    description: str = ""


class Dataset(_Definition):
    id: str = Field(pattern=DATASET_ID_PATTERN)
    display_name: str
    description: str
    grain: str
    domain: str = Field(pattern=NAME_PATTERN)
    visibility: Literal["public", "internal"] = "public"
    source: str
    depends_on: list[str] = Field(default_factory=list)
    base_sql: str
    entities: list[EntityKey] = Field(default_factory=list)
    dimensions: list[DatasetDimension] = Field(default_factory=list)
    measures: list[Measure] = Field(default_factory=list)
    filters: list[DatasetFilter] = Field(default_factory=list)
    pii_columns: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _names_are_unique_and_ratios_resolve(self) -> "Dataset":
        names = [d.name for d in self.dimensions] + [m.name for m in self.measures]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"{self.id}: dimension/measure names must be unique, repeated: {dupes}")
        filter_names = [f.name for f in self.filters]
        if len(filter_names) != len(set(filter_names)):
            raise ValueError(f"{self.id}: filter names must be unique")
        plain = {m.name for m in self.measures if m.agg != "ratio"}
        for m in self.measures:
            if m.agg == "ratio" and not {m.numerator, m.denominator} <= plain:
                raise ValueError(f"{self.id}: ratio {m.name} must reference non-ratio measures of this dataset")
        return self

    def dimension(self, name: str) -> Optional[DatasetDimension]:
        return next((d for d in self.dimensions if d.name == name), None)

    def measure(self, name: str) -> Optional[Measure]:
        return next((m for m in self.measures if m.name == name), None)

    def filter(self, name: str) -> Optional[DatasetFilter]:
        return next((f for f in self.filters if f.name == name), None)


class SemanticMetric(_Definition):
    id: str = Field(pattern=METRIC_ID_PATTERN)
    display_name: str
    description: str
    owner: str
    authority: str
    last_reviewed: date
    measure: str = Field(pattern=DATASET_ID_PATTERN[:-1] + r":[a-z][a-z0-9_]*$")
    default_filters: list[str] = Field(default_factory=list)
    synonyms: list[str] = Field(default_factory=list)
    example_questions: list[str] = Field(default_factory=list)

    @property
    def dataset_id(self) -> str:
        return self.measure.split(":", 1)[0]

    @property
    def measure_name(self) -> str:
        return self.measure.split(":", 1)[1]

    @property
    def short_name(self) -> str:
        return self.id.split(".")[1]


class Catalog(BaseModel):
    model_config = ConfigDict(frozen=True)
    datasets: dict[str, Dataset]
    metrics: dict[str, SemanticMetric]
```

- [ ] **Step 6: Implement the contract models**

```python
# semantic_layer/contract.py
"""The semantic query contract: what a caller asks for, and what compiling it returns."""

from __future__ import annotations

from datetime import date
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

FilterOp = Literal[
    "eq", "neq", "in", "not_in", "gt", "gte", "lt", "lte",
    "between", "is_null", "not_null", "contains",
]
# StrictBool precedes StrictInt so JSON true stays a bool rather than becoming 1.
FilterValue = Union[StrictBool, StrictInt, float, str]

_ARITY = {
    "is_null": (0, 0), "not_null": (0, 0),
    "eq": (1, 1), "neq": (1, 1), "gt": (1, 1), "gte": (1, 1),
    "lt": (1, 1), "lte": (1, 1), "contains": (1, 1),
    "between": (2, 2), "in": (1, None), "not_in": (1, None),
}


class _Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ContractFilter(_Contract):
    dimension: str
    op: FilterOp
    values: list[FilterValue] = Field(default_factory=list)

    @model_validator(mode="after")
    def _arity(self) -> "ContractFilter":
        lo, hi = _ARITY[self.op]
        n = len(self.values)
        if n < lo or (hi is not None and n > hi):
            raise ValueError(f"filter on {self.dimension}: op {self.op} takes {lo}..{hi or 'n'} values, got {n}")
        return self


class TimeRange(_Contract):
    dimension: str
    start: Optional[date] = None
    end: Optional[date] = None

    @model_validator(mode="after")
    def _bounded(self) -> "TimeRange":
        if self.start is None and self.end is None:
            raise ValueError("time_range needs start, end, or both")
        if self.start and self.end and self.start > self.end:
            raise ValueError("time_range start is after end")
        return self


class OrderBy(_Contract):
    field: str
    direction: Literal["asc", "desc"] = "desc"


class QueryContract(_Contract):
    metrics: list[str] = Field(default_factory=list)
    measures: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[ContractFilter] = Field(default_factory=list)
    time_range: Optional[TimeRange] = None
    order_by: list[OrderBy] = Field(default_factory=list)
    limit: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode="after")
    def _asks_for_something(self) -> "QueryContract":
        if not self.metrics and not self.measures:
            raise ValueError("a query needs at least one metric or measure")
        return self


class Provenance(BaseModel):
    governed: bool = True
    datasets: list[str]
    metrics: list[str]
    measures: list[str]


class CompiledQuery(BaseModel):
    sql: str
    provenance: Provenance
```

- [ ] **Step 7: Run the tests**

Run: `.venv/bin/python -m pytest -q tests`
Expected: `31 passed`.

- [ ] **Step 8: Commit, push and open the PR**

```bash
git add semantic_layer/schema.py semantic_layer/contract.py tests/semantic_fixtures.py tests/test_semantic_schema.py
git commit -m "feat: semantic layer dataset, metric and query contract models

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin feat/semantic-models
gh pr create --base main --title "feat: semantic layer dataset, metric and query contract models" --body "$(cat <<'EOF'
**Claude:** First PR of the semantic layer walking skeleton (spec §3.2, §3.3, §5.1). Adds Pydantic models only: datasets with dimensions, measures, filters and entity keys; governed metrics over a dataset measure; and the query contract the compiler will accept. Nothing calls them yet; the legacy metric engine is untouched. Phase 1 is five PRs; this and the compiler PR are the largest.

Reviewable surface: 4 files, ~350 lines of code and tests. Spec and plan land separately in the docs PR.

Test plan: `pytest tests` passes locally (Python 3.11).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 9: Stop.** Report the PR URL and wait for approval and merge. Do not start the next task.

### Task 2 (PR 1b): Sandboxed template rendering and catalog loader

**Files:**
- Create: `semantic_layer/render.py`
- Create: `semantic_layer/catalog.py`
- Test: `tests/test_semantic_render.py`, `tests/test_semantic_catalog.py`

**Interfaces:**
- Consumes: `Catalog`, `Dataset`, `SemanticMetric` (Task 1); `tests.semantic_fixtures`.
- Produces:
  - `semantic_layer.render`: `render(template: str, database: str, on_ref: Callable[[str], str]) -> str` (strips trailing whitespace and `;`), `cte_name(dataset_id: str) -> str` (`dataset.course_filters.v1` → `DS_COURSE_FILTERS_V1`), `TemplateError(ValueError)`.
  - `semantic_layer.catalog`: `load_catalog(root: Path = CANONICAL_DIR) -> Catalog` (reads `datasets/**/*.yaml` and `metrics/*.yaml`; missing directories load as empty), `default_catalog() -> Catalog` (cached per process), `CatalogError(ValueError)`.

- [ ] **Step 1: Branch from the latest `main`**

```bash
git fetch origin && git checkout -b feat/semantic-render-catalog origin/main
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_semantic_render.py
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
```

```python
# tests/test_semantic_catalog.py
import pytest
import yaml

from semantic_layer.catalog import CatalogError, load_catalog
from tests.semantic_fixtures import ENROLLMENTS, STUDENT_ENROLLMENTS


def _write(root, datasets=(), metrics=()):
    (root / "datasets" / "test").mkdir(parents=True)
    (root / "metrics").mkdir()
    for i, ds in enumerate(datasets):
        (root / "datasets" / "test" / f"d{i}.yaml").write_text(yaml.safe_dump(ds))
    (root / "metrics" / "test.yaml").write_text(yaml.safe_dump({"metrics": list(metrics)}))


def _metric():
    return STUDENT_ENROLLMENTS.model_dump(mode="json")


def test_loads_datasets_and_metrics(tmp_path):
    _write(tmp_path, [ENROLLMENTS.model_dump()], [_metric()])
    cat = load_catalog(tmp_path)
    assert list(cat.datasets) == ["dataset.enrollments.v1"]
    assert list(cat.metrics) == ["metric.student_enrollments.v1"]


def test_missing_directories_load_as_empty(tmp_path):
    cat = load_catalog(tmp_path)
    assert cat.datasets == {} and cat.metrics == {}


def test_duplicate_dataset_id_is_an_error(tmp_path):
    _write(tmp_path, [ENROLLMENTS.model_dump(), ENROLLMENTS.model_dump()])
    with pytest.raises(CatalogError, match="duplicate dataset id"):
        load_catalog(tmp_path)


def test_invalid_definition_names_its_file(tmp_path):
    _write(tmp_path, [ENROLLMENTS.model_dump() | {"grain": None}])
    with pytest.raises(CatalogError, match="d0.yaml"):
        load_catalog(tmp_path)


def test_duplicate_metric_id_is_an_error(tmp_path):
    _write(tmp_path, [ENROLLMENTS.model_dump()], [_metric(), _metric()])
    with pytest.raises(CatalogError, match="duplicate metric id"):
        load_catalog(tmp_path)
```

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_semantic_render.py tests/test_semantic_catalog.py`
Expected: collection errors, `ModuleNotFoundError` for `semantic_layer.render` and `semantic_layer.catalog`.

- [ ] **Step 4: Implement rendering**

The allowlist walk runs before rendering; the sandbox is a second layer. `{{ ref(database) }}` is rejected because `ref` must take a string constant.

```python
# semantic_layer/render.py
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
```

- [ ] **Step 5: Implement the loader**

```python
# semantic_layer/catalog.py
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
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest -q tests`
Expected: `47 passed`.

- [ ] **Step 7: Commit, push and open the PR**

```bash
git add semantic_layer/render.py semantic_layer/catalog.py tests/test_semantic_render.py tests/test_semantic_catalog.py
git commit -m "feat: sandboxed dataset template rendering and catalog loader

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin feat/semantic-render-catalog
gh pr create --base main --title "feat: sandboxed dataset template rendering and catalog loader" --body "$(cat <<'EOF'
**Claude:** Second walking-skeleton PR (spec §3.1, §3.2 rules). Dataset SQL templates may contain only `{{ database }}` and `{{ ref('<id>') }}`: an AST allowlist rejects everything else before rendering, and rendering uses Jinja's `SandboxedEnvironment`. The legacy engine's unsandboxed overlay rendering is replaced in Phase 6b. Also adds the YAML catalog loader for `canonical/datasets/` and `canonical/metrics/` (both still empty).

Reviewable surface: 4 files, ~190 lines of code and tests. Spec and plan land separately in the docs PR.

Test plan: `pytest tests` passes locally (Python 3.11).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 8: Stop.** Report the PR URL and wait for approval and merge. Do not start the next task.

### Task 3 (PR 1c): Contract compiler

**Files:**
- Create: `semantic_layer/compiler.py`
- Test: `tests/test_semantic_compiler.py`

**Interfaces:**
- Consumes: `render`, `cte_name`, `TemplateError` (Task 2); models (Task 1).
- Produces: `semantic_layer.compiler`:
  - `compile_query(contract: QueryContract, catalog: Catalog, database: str) -> CompiledQuery`
  - `build_ctes(catalog: Catalog, dataset_ids: list[str], database: str) -> dict[str, str]`: CTE name → rendered SQL, dependencies first, each once. Raises on unknown dataset, undeclared `ref()`, or a cycle.
  - `CompileError(ValueError)`
- Output column names: dimension → its contract name (`term_name`, `start_date__month`); metric → `SemanticMetric.short_name`; measure → measure name. Snowflake returns them upper-cased.
- Limits of this phase: single-dataset queries only (cross-dataset joins are the first Phase 4 unit); no tenant overlays (Phase 6b).

- [ ] **Step 1: Branch from the latest `main`**

```bash
git fetch origin && git checkout -b feat/semantic-compiler origin/main
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_semantic_compiler.py
import sqlglot
import pytest

from semantic_layer.compiler import CompileError, build_ctes, compile_query
from semantic_layer.contract import QueryContract
from tests.semantic_fixtures import ENROLLMENTS, catalog, dataset


def _compile(cat=None, **contract):
    return compile_query(QueryContract(**contract), cat or catalog(), "DB")


def _outer(sql: str) -> str:
    return sql[sql.rindex("\nSELECT"):]


def test_metric_filters_scope_the_measure_not_the_query():
    q = _compile(metrics=["metric.student_enrollments.v1"], measures=["dataset.enrollments.v1:people"])
    outer = _outer(q.sql)
    assert "COUNT(CASE WHEN COURSE_ROLE = 'S' THEN ID END) AS student_enrollments" in outer
    assert "COUNT(DISTINCT PERSON_ID) AS people" in outer
    assert "WHERE" not in outer


def test_ratio_divides_aggregates_and_guards_zero():
    q = _compile(measures=["dataset.enrollments.v1:per_person"])
    assert "COUNT(ID) / NULLIF(COUNT(DISTINCT PERSON_ID), 0) AS per_person" in q.sql


def test_dimensions_group_and_time_grains_truncate():
    q = _compile(measures=["dataset.enrollments.v1:enrollments"], dimensions=["course_role", "enrolled_at__month"])
    outer = _outer(q.sql)
    assert "COURSE_ROLE AS course_role" in outer
    assert "DATE_TRUNC('month', ENROLLMENT_TIME) AS enrolled_at__month" in outer
    assert "GROUP BY\n  1,\n  2" in outer


def test_filter_values_are_escaped_literals():
    q = _compile(measures=["dataset.enrollments.v1:enrollments"],
                 filters=[{"dimension": "course_role", "op": "eq", "values": ["x' OR 1=1 --"]}])
    where = sqlglot.parse_one(q.sql, read="snowflake").args["where"].this
    assert isinstance(where, sqlglot.exp.EQ)
    assert where.expression.this == "x' OR 1=1 --"


@pytest.mark.parametrize("op,values,fragment", [
    ("in", ["S", "P"], "COURSE_ROLE IN ('S', 'P')"),
    ("not_in", ["S"], "NOT COURSE_ROLE IN ('S')"),
    ("between", ["A", "M"], "COURSE_ROLE BETWEEN 'A' AND 'M'"),
    ("is_null", [], "COURSE_ROLE IS NULL"),
    ("not_null", [], "NOT COURSE_ROLE IS NULL"),
    ("contains", ["st"], "CONTAINS(LOWER(COURSE_ROLE), LOWER('st'))"),
    ("gte", [3], "COURSE_ROLE >= 3"),
    ("eq", [True], "COURSE_ROLE = TRUE"),
])
def test_filter_operators(op, values, fragment):
    q = _compile(measures=["dataset.enrollments.v1:enrollments"],
                 filters=[{"dimension": "course_role", "op": op, "values": values}])
    assert fragment in q.sql


def test_time_range_compares_dates():
    q = _compile(measures=["dataset.enrollments.v1:enrollments"],
                 time_range={"dimension": "enrolled_at", "start": "2026-01-01", "end": "2026-06-30"})
    assert "CAST(ENROLLMENT_TIME AS DATE) >= '2026-01-01'" in q.sql
    assert "CAST(ENROLLMENT_TIME AS DATE) <= '2026-06-30'" in q.sql


def test_time_range_rejects_non_time_dimension():
    with pytest.raises(CompileError, match="time dimension"):
        _compile(measures=["dataset.enrollments.v1:enrollments"], time_range={"dimension": "course_role", "start": "2026-01-01"})


def test_limit_is_emitted_once():
    q = _compile(measures=["dataset.enrollments.v1:enrollments"], limit=7)
    assert q.sql.upper().count("LIMIT") == 1
    assert q.sql.rstrip().endswith("LIMIT 7")


def test_order_by_must_name_a_selected_field():
    q = _compile(measures=["dataset.enrollments.v1:enrollments"], order_by=[{"field": "enrollments", "direction": "asc"}])
    assert "ORDER BY\n  enrollments" in q.sql
    with pytest.raises(CompileError, match="order_by"):
        _compile(measures=["dataset.enrollments.v1:enrollments"], order_by=[{"field": "nope"}])


def test_pii_dimension_cannot_be_selected():
    with pytest.raises(CompileError, match="personally identifiable"):
        _compile(measures=["dataset.enrollments.v1:enrollments"], dimensions=["email"])


def test_pii_dimension_can_be_filtered():
    q = _compile(measures=["dataset.enrollments.v1:enrollments"],
                 filters=[{"dimension": "email", "op": "not_null"}])
    assert "NOT EMAIL IS NULL" in q.sql


@pytest.mark.parametrize("contract,match", [
    ({"metrics": ["metric.nope.v1"]}, "unknown metric"),
    ({"measures": ["dataset.enrollments.v1:nope"]}, "unknown measure"),
    ({"measures": ["dataset.enrollments.v1:enrollments"], "dimensions": ["nope"]}, "unknown dimension"),
    ({"measures": ["dataset.enrollments.v1:enrollments"], "dimensions": ["enrolled_at__year"]}, "grain"),
    ({"measures": ["dataset.enrollments.v1:enrollments"], "filters": [{"dimension": "nope", "op": "is_null"}]}, "unknown filter dimension"),
    ({"metrics": ["metric.student_enrollments.v1"], "measures": ["dataset.enrollments.v1:enrollments"],
      "dimensions": []}, None),
])
def test_contract_errors(contract, match):
    if match is None:
        _compile(**contract)
        return
    with pytest.raises(CompileError, match=match):
        _compile(**contract)


def test_measures_from_two_datasets_are_rejected_until_joins_exist():
    other = dataset(id="dataset.other.v1")
    with pytest.raises(CompileError, match="more than one dataset"):
        _compile(catalog(ENROLLMENTS, other),
                 measures=["dataset.enrollments.v1:enrollments", "dataset.other.v1:enrollments"])


def test_internal_datasets_cannot_be_queried():
    with pytest.raises(CompileError, match="internal"):
        _compile(catalog(dataset(visibility="internal")), measures=["dataset.enrollments.v1:enrollments"])


def test_non_cdm_tables_are_rejected():
    bad = dataset(base_sql="SELECT ID, PERSON_ID, COURSE_ID, COURSE_ROLE, ENROLLMENT_TIME, EMAIL FROM OTHER.SECRETS")
    with pytest.raises(CompileError, match="outside the CDM"):
        _compile(catalog(bad), measures=["dataset.enrollments.v1:enrollments"])


def test_refs_inline_dependencies_first_and_once():
    base = dataset(id="dataset.base.v1")
    mid = dataset(id="dataset.mid.v1", depends_on=["dataset.base.v1"],
                  base_sql="SELECT * FROM {{ ref('dataset.base.v1') }}")
    top = dataset(depends_on=["dataset.mid.v1", "dataset.base.v1"],
                  base_sql="SELECT m.* FROM {{ ref('dataset.mid.v1') }} m JOIN {{ ref('dataset.base.v1') }} b ON b.ID = m.ID")
    ctes = build_ctes(catalog(base, mid, top), ["dataset.enrollments.v1"], "DB")
    assert list(ctes) == ["DS_BASE_V1", "DS_MID_V1", "DS_ENROLLMENTS_V1"]
    q = _compile(catalog(base, mid, top), measures=["dataset.enrollments.v1:enrollments"])
    assert q.provenance.datasets == ["dataset.base.v1", "dataset.mid.v1", "dataset.enrollments.v1"]


def test_ref_must_be_declared_in_depends_on():
    base = dataset(id="dataset.base.v1")
    top = dataset(base_sql="SELECT * FROM {{ ref('dataset.base.v1') }}")
    with pytest.raises(CompileError, match="depends_on"):
        build_ctes(catalog(base, top), ["dataset.enrollments.v1"], "DB")


def test_dependency_cycles_are_rejected():
    a = dataset(id="dataset.a.v1", depends_on=["dataset.b.v1"], base_sql="SELECT * FROM {{ ref('dataset.b.v1') }}")
    b = dataset(id="dataset.b.v1", depends_on=["dataset.a.v1"], base_sql="SELECT * FROM {{ ref('dataset.a.v1') }}")
    with pytest.raises(CompileError, match="cycle"):
        build_ctes(catalog(a, b), ["dataset.a.v1"], "DB")


def test_template_injection_in_base_sql_is_a_compile_error():
    bad = dataset(base_sql="SELECT {{ ''.__class__ }} FROM {{ database }}.CDM_LMS.PERSON")
    with pytest.raises(CompileError, match="not allowed"):
        _compile(catalog(bad), measures=["dataset.enrollments.v1:enrollments"])


def test_provenance_lists_metrics_and_measures():
    q = _compile(metrics=["metric.student_enrollments.v1"])
    assert q.provenance.governed is True
    assert q.provenance.metrics == ["metric.student_enrollments.v1"]
    assert q.provenance.measures == ["dataset.enrollments.v1:enrollments"]


def test_time_dimension_filters_compare_whole_days():
    q = _compile(measures=["dataset.enrollments.v1:enrollments"],
                 filters=[{"dimension": "enrolled_at", "op": "eq", "values": ["2026-01-01"]}])
    assert "CAST(ENROLLMENT_TIME AS DATE) = '2026-01-01'" in q.sql


def test_metric_and_measure_with_the_same_output_name_collide():
    with pytest.raises(CompileError, match="collide"):
        _compile(metrics=["metric.student_enrollments.v1"],
                 measures=["dataset.enrollments.v1:enrollments", "dataset.enrollments.v1:enrollments"])


def test_grain_suffix_is_not_a_filter_dimension():
    with pytest.raises(CompileError, match="unknown filter dimension 'enrolled_at__month'"):
        _compile(measures=["dataset.enrollments.v1:enrollments"],
                 filters=[{"dimension": "enrolled_at__month", "op": "not_null"}])


def test_order_by_a_grain_dimension():
    q = _compile(measures=["dataset.enrollments.v1:enrollments"], dimensions=["enrolled_at__month"],
                 order_by=[{"field": "enrolled_at__month", "direction": "asc"}])
    assert "ORDER BY\n  enrolled_at__month" in q.sql


def test_base_sql_ending_in_a_line_comment_still_compiles():
    commented = dataset(base_sql=ENROLLMENTS.base_sql + "\n-- trailing note")
    q = _compile(catalog(commented), measures=["dataset.enrollments.v1:enrollments"])
    sqlglot.parse_one(q.sql, read="snowflake")
```

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_semantic_compiler.py`
Expected: collection error, `ModuleNotFoundError: No module named 'semantic_layer.compiler'`.

- [ ] **Step 4: Implement the compiler**

Notes for the implementer:
- Dataset SQL is inlined verbatim, never regenerated through sqlglot, so Snowflake syntax such as `STAGE:timezone::STRING` and `LATERAL SPLIT_TO_TABLE` is untouched. Each CTE body is closed on its own line (`\n)`) so a trailing `--` comment can't swallow the parenthesis.
- Snowflake accepts a `WITH` inside a CTE body; this was verified live against the POC CDM with dataset 1.
- `_check_tables` treats every CTE alias in the final tree (outer `DS_*` and a dataset's own inner CTEs) as local; any other table must be in a `CDM_*` schema.
- Metric `default_filters` scope that metric's aggregate (`COUNT(CASE WHEN <filter> THEN expr END)`), not the whole query, so two metrics with different filters can share one query.

```python
# semantic_layer/compiler.py
"""Compile a QueryContract into Snowflake SQL over the semantic catalog.

Dataset SQL is inlined verbatim as CTEs (dependencies first); the outer SELECT is
built as a sqlglot AST, so filter values are always typed literals and never
spliced into SQL text.
"""

from __future__ import annotations

from typing import Optional

import sqlglot
import sqlglot.expressions as exp

from .contract import CompiledQuery, ContractFilter, Provenance, QueryContract
from .render import TemplateError, cte_name, render
from .schema import Catalog, Dataset, DatasetDimension, Measure, SemanticMetric


class CompileError(ValueError):
    """The contract cannot be compiled against the catalog."""


def build_ctes(catalog: Catalog, dataset_ids: list[str], database: str) -> dict[str, str]:
    """Rendered SQL for the datasets and everything they depend on, keyed by CTE name, dependencies first."""
    done: dict[str, str] = {}

    def visit(ds_id: str, path: tuple[str, ...]) -> None:
        if cte_name(ds_id) in done:
            return
        if ds_id in path:
            raise CompileError(f"dataset dependency cycle: {' -> '.join(path + (ds_id,))}")
        ds = catalog.datasets.get(ds_id)
        if ds is None:
            raise CompileError(f"unknown dataset {ds_id}" + (f" (referenced by {path[-1]})" if path else ""))

        def on_ref(target: str) -> str:
            if target not in ds.depends_on:
                raise CompileError(f"{ds.id} refs {target} without listing it in depends_on")
            return cte_name(target)

        try:
            sql = render(ds.base_sql, database, on_ref)
        except TemplateError as e:
            raise CompileError(f"{ds.id}: {e}") from e
        for dep in ds.depends_on:
            visit(dep, path + (ds_id,))
        done[cte_name(ds_id)] = sql

    for ds_id in dataset_ids:
        visit(ds_id, ())
    return done


def _literal(value) -> exp.Expression:
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, (int, float)):
        return exp.Literal.number(value)
    return exp.Literal.string(str(value))


def _condition(col: exp.Expression, f: ContractFilter) -> exp.Expression:
    v = [_literal(x) for x in f.values]
    if f.op == "eq":
        return exp.EQ(this=col, expression=v[0])
    if f.op == "neq":
        return exp.NEQ(this=col, expression=v[0])
    if f.op == "gt":
        return exp.GT(this=col, expression=v[0])
    if f.op == "gte":
        return exp.GTE(this=col, expression=v[0])
    if f.op == "lt":
        return exp.LT(this=col, expression=v[0])
    if f.op == "lte":
        return exp.LTE(this=col, expression=v[0])
    if f.op == "in":
        return exp.In(this=col, expressions=v)
    if f.op == "not_in":
        return exp.Not(this=exp.In(this=col, expressions=v))
    if f.op == "between":
        return exp.Between(this=col, low=v[0], high=v[1])
    if f.op == "is_null":
        return exp.Is(this=col, expression=exp.Null())
    if f.op == "not_null":
        return exp.Not(this=exp.Is(this=col, expression=exp.Null()))
    # contains: case-insensitive substring; CONTAINS avoids LIKE wildcard escaping.
    return exp.Anonymous(this="CONTAINS", expressions=[exp.Lower(this=col), exp.Lower(this=v[0])])


def _parse(sql: str, what: str) -> exp.Expression:
    try:
        return sqlglot.parse_one(sql, read="snowflake")
    except sqlglot.errors.ParseError as e:
        raise CompileError(f"{what}: {e}") from e


def _aggregate(ds: Dataset, m: Measure, scope: Optional[exp.Expression]) -> exp.Expression:
    if m.agg == "ratio":
        num = _aggregate(ds, ds.measure(m.numerator), scope)
        den = _aggregate(ds, ds.measure(m.denominator), scope)
        return exp.Div(this=num, expression=exp.Anonymous(this="NULLIF", expressions=[den, exp.Literal.number(0)]))
    arg = _parse(m.expr, f"{ds.id}:{m.name} expr")
    if scope is not None:
        arg = exp.Case(ifs=[exp.If(this=scope.copy(), true=arg)])
    if m.agg == "count_distinct":
        return exp.Count(this=exp.Distinct(expressions=[arg]))
    return exp.Anonymous(this=m.agg.upper(), expressions=[arg])


def _resolve_dimension(ds: Dataset, ref: str) -> tuple[DatasetDimension, Optional[str]]:
    name, _, grain = ref.partition("__")
    dim = ds.dimension(name)
    if dim is None:
        raise CompileError(f"unknown dimension {name!r} on {ds.id}")
    if grain and grain not in dim.grains:
        raise CompileError(f"dimension {name!r} does not support grain {grain!r}; allowed: {dim.grains}")
    return dim, grain or None


def _check_tables(sql: str) -> None:
    tree = _parse(sql, "compiled query")
    local = {cte.alias_or_name.upper() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        schema = table.db.upper()
        if not schema and table.name.upper() in local:
            continue
        if not schema.startswith("CDM_"):
            raise CompileError(f"table {table.sql(dialect='snowflake')} is outside the CDM_* schemas")


def compile_query(contract: QueryContract, catalog: Catalog, database: str) -> CompiledQuery:
    selections: list[tuple[str, Dataset, Measure, Optional[SemanticMetric]]] = []
    for metric_id in contract.metrics:
        metric = catalog.metrics.get(metric_id)
        if metric is None:
            raise CompileError(f"unknown metric {metric_id}")
        ds = catalog.datasets.get(metric.dataset_id)
        measure = ds.measure(metric.measure_name) if ds else None
        if measure is None:
            raise CompileError(f"metric {metric_id} points at missing measure {metric.measure}")
        selections.append((metric.short_name, ds, measure, metric))
    for ref in contract.measures:
        ds_id, _, name = ref.partition(":")
        ds = catalog.datasets.get(ds_id)
        measure = ds.measure(name) if ds else None
        if measure is None:
            raise CompileError(f"unknown measure {ref}")
        selections.append((name, ds, measure, None))

    dataset_ids = sorted({ds.id for _, ds, _, _ in selections})
    if len(dataset_ids) > 1:
        raise CompileError(f"measures from more than one dataset are not supported yet: {dataset_ids}")
    ds = selections[0][1]
    if ds.visibility != "public":
        raise CompileError(f"{ds.id} is internal and cannot be queried directly")

    select: list[exp.Expression] = []
    aliases: list[str] = []
    for ref in contract.dimensions:
        dim, grain = _resolve_dimension(ds, ref)
        if dim.column in ds.pii_columns:
            raise CompileError(f"dimension {dim.name!r} is personally identifiable and cannot be selected")
        col = exp.column(dim.column)
        node = exp.Anonymous(this="DATE_TRUNC", expressions=[exp.Literal.string(grain), col]) if grain else col
        select.append(exp.alias_(node, ref))
        aliases.append(ref)

    for alias, _, measure, metric in selections:
        scope = None
        if metric and metric.default_filters:
            parts = []
            for fname in metric.default_filters:
                f = ds.filter(fname)
                if f is None:
                    raise CompileError(f"metric {metric.id} uses unknown filter {fname!r}")
                parts.append(_parse(f.sql, f"{ds.id} filter {fname}"))
            scope = exp.and_(*parts)
        select.append(exp.alias_(_aggregate(ds, measure, scope), alias))
        aliases.append(alias)
    if len(set(aliases)) != len(aliases):
        raise CompileError(f"output names collide: {aliases}")

    where: list[exp.Expression] = []
    for f in contract.filters:
        dim = ds.dimension(f.dimension)
        if dim is None:
            raise CompileError(f"unknown filter dimension {f.dimension!r} on {ds.id}")
        col = exp.column(dim.column)
        if dim.type == "time":
            col = exp.Cast(this=col, to=exp.DataType.build("DATE"))
        where.append(_condition(col, f))
    if contract.time_range:
        dim = ds.dimension(contract.time_range.dimension)
        if dim is None or dim.type != "time":
            raise CompileError(f"time_range needs a time dimension; {contract.time_range.dimension!r} is not one")
        day = exp.Cast(this=exp.column(dim.column), to=exp.DataType.build("DATE"))
        if contract.time_range.start:
            where.append(exp.GTE(this=day, expression=exp.Literal.string(contract.time_range.start.isoformat())))
        if contract.time_range.end:
            where.append(exp.LTE(this=day.copy(), expression=exp.Literal.string(contract.time_range.end.isoformat())))

    query = exp.select(*select).from_(cte_name(ds.id))
    if where:
        query = query.where(exp.and_(*where))
    if contract.dimensions:
        query = query.group_by(*[exp.Literal.number(i + 1) for i in range(len(contract.dimensions))])
    lowered = {a.lower() for a in aliases}
    for ob in contract.order_by:
        if ob.field.lower() not in lowered:
            raise CompileError(f"order_by {ob.field!r} is not one of the selected fields {aliases}")
        query = query.order_by(exp.Ordered(this=exp.column(ob.field), desc=ob.direction == "desc"))
    query = query.limit(contract.limit)

    ctes = build_ctes(catalog, [ds.id], database)
    with_clause = ",\n".join(f"{name} AS (\n{sql}\n)" for name, sql in ctes.items())
    sql = f"WITH {with_clause}\n{query.sql(dialect='snowflake', pretty=True)}"
    _check_tables(sql)

    return CompiledQuery(
        sql=sql,
        provenance=Provenance(
            datasets=[ds_id for ds_id in catalog.datasets if cte_name(ds_id) in ctes],
            metrics=list(contract.metrics),
            measures=[f"{d.id}:{m.name}" for _, d, m, _ in selections],
        ),
    )
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest -q tests`
Expected: `84 passed`.

- [ ] **Step 6: Commit, push and open the PR**

```bash
git add semantic_layer/compiler.py tests/test_semantic_compiler.py
git commit -m "feat: compile semantic query contracts to Snowflake SQL

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin feat/semantic-compiler
gh pr create --base main --title "feat: compile semantic query contracts to Snowflake SQL" --body "$(cat <<'EOF'
**Claude:** Third walking-skeleton PR (spec §5.1, §5.2). Compiles a query contract (metrics, measures, dimensions with time grains, filters, time range, order, limit) into one SELECT over the dataset CTE chain. Filter values are typed sqlglot literals, never spliced into text; metric filters scope only their own aggregate; the limit is emitted once (max 1000), which fixes the legacy `LIMIT 10000` conflict for the new path; tables outside `CDM_*` are rejected. Single-dataset queries only until the Phase 4 join unit.

Reviewable surface: 2 files, ~430 lines of code and tests (the largest Phase 1 PR). Spec and plan land separately in the docs PR.

Test plan: `pytest tests` passes locally (Python 3.11).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 7: Stop.** Report the PR URL and wait for approval and merge. Do not start the next task.

### Task 4 (PR 1d): Offline validation and dataset 1 (`course_filters`)

**Files:**
- Create: `semantic_layer/pii.py`
- Modify: `snowflake_client.py` (use the shared PII set; behaviour unchanged)
- Create: `semantic_layer/validate.py`
- Create: `scripts/build_dictionary_snapshot.py`
- Create (generated): `tests/fixtures/cdm_dictionary.json`
- Create: `canonical/datasets/course/course_filters.yaml`
- Create: `canonical/metrics/course.yaml`
- Test: `tests/test_semantic_validate.py`, `tests/test_semantic_definitions.py`

**Interfaces:**
- Consumes: `build_ctes`, `compile_query`, `CompileError` (Task 3); `load_catalog` (Task 2).
- Produces:
  - `semantic_layer.pii.PII_COLUMN_NAMES: frozenset[str]`
  - `semantic_layer.validate`: `load_snapshot(path=DICTIONARY_SNAPSHOT) -> dict`, `output_columns(ds, catalog, snapshot) -> list[str]`, `validate_dataset(ds, catalog, snapshot) -> list[str]` (empty = valid), `validate_metric(m, catalog) -> list[str]`.
  - `dataset.course_filters.v1` with measures `courses`, `ended_courses`, `share_ended`; filters `ongoing`, `fixed_duration`.
  - `metric.reportable_courses.v1`, `metric.ongoing_courses.v1`.
  - `tests/test_semantic_definitions.py` is parametrised over the real catalog, so every later dataset and metric PR is covered without new test code.

- [ ] **Step 1: Branch from the latest `main`**

```bash
git fetch origin && git checkout -b feat/semantic-validate-course-filters origin/main
```

- [ ] **Step 2: Write the failing validator tests**

```python
# tests/test_semantic_validate.py
from semantic_layer.validate import load_snapshot, output_columns, validate_dataset, validate_metric
from tests.semantic_fixtures import ENROLLMENTS, STUDENT_ENROLLMENTS, catalog, dataset

SNAPSHOT = load_snapshot()


def test_valid_dataset_has_no_errors_and_reports_outputs():
    assert validate_dataset(ENROLLMENTS, catalog(), SNAPSHOT) == []
    assert output_columns(ENROLLMENTS, catalog(), SNAPSHOT) == [
        "ID", "PERSON_ID", "COURSE_ID", "COURSE_ROLE", "ENROLLMENT_TIME", "EMAIL"]


def test_unknown_source_column_is_reported():
    bad = dataset(base_sql=ENROLLMENTS.base_sql.replace("pc.COURSE_ROLE", "pc.TERM_ID AS COURSE_ROLE"))
    errors = validate_dataset(bad, catalog(bad), SNAPSHOT)
    assert len(errors) == 1 and "TERM_ID" in errors[0]


def test_definition_columns_must_be_dataset_outputs():
    bad = dataset(dimensions=[{"name": "term", "column": "TERM_NAME", "type": "categorical"}],
                  measures=[{"name": "m", "agg": "sum", "expr": "CREDITS"}],
                  filters=[{"name": "f", "sql": "STATUS = 'X'"}], pii_columns=["EMAIL"])
    errors = "\n".join(validate_dataset(bad, catalog(bad), SNAPSHOT))
    assert "dimension term" in errors and "TERM_NAME" in errors
    assert "measure m" in errors and "CREDITS" in errors
    assert "filter f" in errors and "STATUS" in errors


def test_undeclared_pii_output_is_reported():
    bad = dataset(pii_columns=[])
    assert any("pii_columns" in e and "EMAIL" in e for e in validate_dataset(bad, catalog(bad), SNAPSHOT))


def test_validation_through_refs_uses_dependency_outputs():
    base = dataset(id="dataset.base.v1")
    top = dataset(depends_on=["dataset.base.v1"],
                  base_sql="SELECT ID, PERSON_ID, COURSE_ID, COURSE_ROLE, ENROLLMENT_TIME, EMAIL FROM {{ ref('dataset.base.v1') }}")
    assert validate_dataset(top, catalog(base, top), SNAPSHOT) == []


def test_metric_must_point_at_existing_measure_and_filters():
    assert validate_metric(STUDENT_ENROLLMENTS, catalog()) == []
    bad = type(STUDENT_ENROLLMENTS)(**(STUDENT_ENROLLMENTS.model_dump() | {"measure": "dataset.enrollments.v1:nope", "default_filters": ["zzz"]}))
    errors = "\n".join(validate_metric(bad, catalog()))
    assert "no measure 'nope'" in errors and "no filter 'zzz'" in errors
```

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_semantic_validate.py`
Expected: collection error, `ModuleNotFoundError: No module named 'semantic_layer.validate'`.

- [ ] **Step 4: Add the snapshot builder and generate the fixture**

The source is the dictionary export already committed in `illuminate-poc` (it matches the live schema: `PERSON_COURSE`'s 23 columns are identical to illuminate-mcp `describe_entity` output).

```python
# scripts/build_dictionary_snapshot.py
"""Build tests/fixtures/cdm_dictionary.json from an Illuminate data-dictionary catalog export.

Usage: python scripts/build_dictionary_snapshot.py <catalog.json>
The input is the `catalog` shape served by /api/v1/dictionary (schema -> tables -> columns).
"""

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "cdm_dictionary.json"


def main(src: str) -> None:
    raw = json.loads(Path(src).read_text())
    catalog = raw.get("catalog", raw)
    snapshot = {
        schema: {
            table: {col: meta["dataType"] for col, meta in sorted(t["columns"].items())}
            for table, t in sorted(body["tables"].items())
        }
        for schema, body in sorted(catalog.items())
        if schema.startswith("CDM_")
    }
    OUT.write_text(json.dumps(snapshot, indent=1, sort_keys=True) + "\n")
    print(f"wrote {OUT} ({sum(len(t) for t in snapshot.values())} tables)")


if __name__ == "__main__":
    main(sys.argv[1])
```

Run:

```bash
mkdir -p tests/fixtures
.venv/bin/python scripts/build_dictionary_snapshot.py ../illuminate-poc/src/data/illuminate-dictionary.json
```

Expected: `wrote .../tests/fixtures/cdm_dictionary.json (99 tables)`, about 50 KB.

- [ ] **Step 5: Share the PII column set with the execution guard**

```python
# semantic_layer/pii.py
"""Column names treated as personally identifiable wherever they appear."""

PII_COLUMN_NAMES = frozenset({
    "FIRST_NAME", "LAST_NAME", "EMAIL", "SSN", "PHONE", "ADDRESS",
    "DOB", "DATE_OF_BIRTH", "PASSWORD", "PASSWD", "PHONE_NUMBER",
    "STREET_ADDRESS", "ZIP_CODE", "ZIPCODE",
})
```

```diff
@@ -15,6 +15,8 @@
 
 import boto3
 
+from semantic_layer.pii import PII_COLUMN_NAMES
+
 logger = logging.getLogger("API-PROXY")
 
 _sf_connection = None
@@ -184,12 +186,6 @@
             }
 
     # PII column check — block bare PII columns in outermost SELECT without GROUP BY / aggregation
-    _PII_COLUMNS = {
-        "FIRST_NAME", "LAST_NAME", "EMAIL", "SSN", "PHONE", "ADDRESS",
-        "DOB", "DATE_OF_BIRTH", "PASSWORD", "PASSWD", "PHONE_NUMBER",
-        "STREET_ADDRESS", "ZIP_CODE", "ZIPCODE",
-    }
-
     # Find the outermost SELECT node
     outer_select = stmt.find(exp.Select)
     if outer_select is not None:
@@ -208,7 +204,7 @@
                 col_nodes = list(sel.find_all(exp.Column))
                 for col_node in col_nodes:
                     col_name = col_node.name.upper().strip('"').strip("'")
-                    if col_name in _PII_COLUMNS:
+                    if col_name in PII_COLUMN_NAMES:
                         return {
                             "error": (
                                 f"Column '{col_name}' contains personally identifiable information (PII). "
```

- [ ] **Step 6: Implement the validator**

`qualify(..., validate_qualify_columns=True)` raises on any column the snapshot doesn't have, including bare columns it can't resolve to a source.

```python
# semantic_layer/validate.py
"""Offline validation of catalog definitions against a CDM dictionary snapshot.

Every column a dataset reads must exist in the snapshot, and every column a
dimension, measure, filter or entity names must be one the dataset outputs.
"""

from __future__ import annotations

import json
from pathlib import Path

import sqlglot
import sqlglot.expressions as exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify

from .compiler import CompileError, build_ctes
from .pii import PII_COLUMN_NAMES
from .schema import Catalog, Dataset, SemanticMetric

DICTIONARY_SNAPSHOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "cdm_dictionary.json"
_DB = "VALIDATION_DB"


def load_snapshot(path: Path = DICTIONARY_SNAPSHOT) -> dict:
    """The snapshot as a sqlglot schema: {database: {schema: {table: {column: type}}}}."""
    return {_DB: json.loads(path.read_text())}


def _columns_in(sql: str) -> set[str]:
    return {c.name.upper() for c in sqlglot.parse_one(sql, read="snowflake").find_all(exp.Column)}


def output_columns(ds: Dataset, catalog: Catalog, snapshot: dict) -> list[str]:
    """Qualify the dataset's SQL against the snapshot and return its output column names."""
    ctes = build_ctes(catalog, [ds.id], _DB)
    *deps, own = ctes.items()
    sql = own[1]
    if deps:
        sql = "WITH " + ",\n".join(f"{n} AS (\n{s}\n)" for n, s in deps) + f"\nSELECT * FROM ({sql})"
    tree = qualify(
        sqlglot.parse_one(sql, read="snowflake"),
        schema=snapshot, dialect="snowflake", validate_qualify_columns=True,
    )
    return [c.upper() for c in tree.named_selects]


def validate_dataset(ds: Dataset, catalog: Catalog, snapshot: dict) -> list[str]:
    try:
        outputs = set(output_columns(ds, catalog, snapshot))
    except (CompileError, OptimizeError, ParseError) as e:
        return [f"{ds.id}: {e}"]

    errors: list[str] = []

    def need(cols: set[str], what: str) -> None:
        missing = sorted(cols - outputs)
        if missing:
            errors.append(f"{ds.id}: {what} uses columns the dataset does not output: {missing}")

    for e in ds.entities:
        need({e.column.upper()}, f"entity {e.name}")
    for d in ds.dimensions:
        need({d.column.upper()}, f"dimension {d.name}")
    for m in ds.measures:
        if m.expr:
            need(_columns_in(m.expr), f"measure {m.name}")
    for f in ds.filters:
        need(_columns_in(f.sql), f"filter {f.name}")
    need({c.upper() for c in ds.pii_columns}, "pii_columns")
    undeclared = sorted((outputs & PII_COLUMN_NAMES) - {c.upper() for c in ds.pii_columns})
    if undeclared:
        errors.append(f"{ds.id}: outputs PII columns not listed in pii_columns: {undeclared}")
    return errors


def validate_metric(m: SemanticMetric, catalog: Catalog) -> list[str]:
    ds = catalog.datasets.get(m.dataset_id)
    if ds is None:
        return [f"{m.id}: unknown dataset {m.dataset_id}"]
    errors = []
    if ds.measure(m.measure_name) is None:
        errors.append(f"{m.id}: {ds.id} has no measure {m.measure_name!r}")
    errors += [f"{m.id}: {ds.id} has no filter {f!r}" for f in m.default_filters if ds.filter(f) is None]
    if ds.visibility != "public":
        errors.append(f"{m.id}: {ds.id} is internal")
    return errors
```

- [ ] **Step 7: Run the validator tests**

Run: `.venv/bin/python -m pytest -q tests/test_semantic_validate.py`
Expected: `6 passed`.

- [ ] **Step 8: Write the definitions test**

```python
# tests/test_semantic_definitions.py
"""Every canonical dataset and metric passes offline validation and compiles."""

import pytest

from semantic_layer.catalog import load_catalog
from semantic_layer.compiler import compile_query
from semantic_layer.contract import QueryContract
from semantic_layer.validate import load_snapshot, validate_dataset, validate_metric

CATALOG = load_catalog()
SNAPSHOT = load_snapshot()
PUBLIC = [d for d in CATALOG.datasets.values() if d.visibility == "public"]


@pytest.mark.parametrize("ds", CATALOG.datasets.values(), ids=lambda d: d.id)
def test_dataset_is_valid(ds):
    assert validate_dataset(ds, CATALOG, SNAPSHOT) == []


@pytest.mark.parametrize("ds", PUBLIC, ids=lambda d: d.id)
def test_every_measure_and_dimension_compiles(ds):
    dims = [d.name for d in ds.dimensions if d.column not in ds.pii_columns]
    compile_query(QueryContract(measures=[f"{ds.id}:{m.name}" for m in ds.measures], dimensions=dims),
                  CATALOG, "DB")


@pytest.mark.parametrize("metric", CATALOG.metrics.values(), ids=lambda m: m.id)
def test_metric_is_valid_and_compiles(metric):
    assert validate_metric(metric, CATALOG) == []
    compile_query(QueryContract(metrics=[metric.id]), CATALOG, "DB")


def test_catalog_is_not_empty():
    assert CATALOG.datasets and CATALOG.metrics
```

Run: `.venv/bin/python -m pytest -q tests/test_semantic_definitions.py`
Expected: FAIL in `test_catalog_is_not_empty` (no definitions yet).

- [ ] **Step 9: Port dataset 1 from bbd-analytics**

Source: `../illuminate-code/bbdata-bbd-analytics/snowflake/migrations/bbd_analytics/repeatable/R__0000_TFV_FILTERS.sql`. The port keeps the logic line for line, with these changes:
- `tenant_id` and `inferred_ind` predicates removed (columns absent in the POC CDM).
- `instance` joined on `i.ID = co.INSTANCE_ID` (the source joined on `tenant_id`).
- The duplicate `pc.course_role is not null` join and the 1970 clamps are kept as in the source.

```yaml
id: dataset.course_filters.v1
display_name: Reportable courses by hierarchy and role
description: >
  Top-level courses that have started and have at least one student enrollment, with each
  course's term, institutional hierarchy nodes, course window and the roles enrolled in it.
grain: one row per course x institutional hierarchy node x course role
domain: course
source: bbdata-bbd-analytics snowflake/migrations/bbd_analytics/repeatable/R__0000_TFV_FILTERS.sql
base_sql: |
  WITH student_activity AS (
      SELECT DISTINCT c.ID AS COURSE_ID, c.FIRST_COURSE_WEEK, c.LAST_COURSE_WEEK,
             c.FIRST_COURSE_TIME, c.LAST_COURSE_TIME
      FROM {{ database }}.CDM_LMS.PERSON_COURSE pc
      JOIN {{ database }}.CDM_LMS.COURSE c ON pc.COURSE_ID = c.ID
      WHERE pc.STUDENT_IND
  ),
  course_start_end AS (
      SELECT co.ID AS COURSE_ID, co.NAME AS COURSE_NAME, co.COURSE_NUMBER,
             CONVERT_TIMEZONE(NVL(i.STAGE:timezone::STRING, 'UTC'), co.CREATED_TIME)::DATE AS COURSE_CREATION_DATE,
             te.NAME AS TERM_NAME,
             CASE WHEN co.START_TIME IS NOT NULL THEN DATE_TRUNC('week', CONVERT_TIMEZONE(NVL(i.STAGE:timezone::STRING, 'UTC'), co.START_TIME))
                  WHEN te.START_TIME IS NOT NULL THEN DATE_TRUNC('week', CONVERT_TIMEZONE(NVL(i.STAGE:timezone::STRING, 'UTC'), te.START_TIME))
             END AS START_WEEK_DATE,
             CASE WHEN co.END_TIME > CURRENT_DATE() THEN DATE_TRUNC('week', CONVERT_TIMEZONE(NVL(i.STAGE:timezone::STRING, 'UTC'), CURRENT_DATE()))
                  WHEN te.END_TIME > CURRENT_DATE() THEN DATE_TRUNC('week', CONVERT_TIMEZONE(NVL(i.STAGE:timezone::STRING, 'UTC'), CURRENT_DATE()))
                  WHEN co.END_TIME IS NOT NULL THEN DATE_TRUNC('week', CONVERT_TIMEZONE(NVL(i.STAGE:timezone::STRING, 'UTC'), co.END_TIME))
                  WHEN te.END_TIME IS NOT NULL THEN DATE_TRUNC('week', CONVERT_TIMEZONE(NVL(i.STAGE:timezone::STRING, 'UTC'), te.END_TIME))
             END AS END_WEEK_DATE,
             COALESCE(co.START_TIME, te.START_TIME) AS START_DATE,
             COALESCE(co.END_TIME, te.END_TIME) AS END_DATE,
             IFF(COALESCE(co.END_TIME, te.END_TIME) IS NOT NULL, 'Fixed', 'Continuous') AS COURSE_DURATION,
             IFF(COALESCE(co.END_TIME, te.END_TIME) <= CURRENT_DATE(), 1, 0) AS COURSE_ENDED,
             IFF(COALESCE(co.START_TIME, te.START_TIME) > CURRENT_DATE(), 'N', 'Y') AS COURSE_REPORTABLE
      FROM {{ database }}.CDM_LMS.COURSE co
      LEFT JOIN {{ database }}.CDM_LMS.TERM te ON te.ID = co.TERM_ID
      JOIN {{ database }}.CDM_LMS.INSTANCE i ON i.ID = co.INSTANCE_ID
      WHERE co.COURSE_PARENT_ID IS NULL
  ),
  course_institution_hierarchy AS (
      SELECT ihc.COURSE_ID, ih.HIERARCHY_ID_SEQ
      FROM {{ database }}.CDM_LMS.INSTITUTION_HIERARCHY_COURSE ihc
      JOIN {{ database }}.CDM_LMS.INSTITUTION_HIERARCHY ih ON ih.ID = ihc.INSTITUTION_HIERARCHY_ID
      JOIN {{ database }}.CDM_LMS.COURSE c
        ON c.ID = ihc.COURSE_ID
       AND NOT (ihc.ROW_DELETED_TIME IS NOT NULL AND c.ROW_DELETED_TIME IS NULL)
  ),
  institution_hierarchy_nodes AS (
      SELECT DISTINCT TRY_TO_NUMBER(node.VALUE) AS ID, cih.COURSE_ID
      FROM course_institution_hierarchy cih, LATERAL SPLIT_TO_TABLE(cih.HIERARCHY_ID_SEQ, '||') node
  ),
  institution_hierarchy_all AS (
      SELECT ihn.COURSE_ID, ih.HIERARCHY_ID_SEQ, ih.HIERARCHY_NAME_SEQ
      FROM institution_hierarchy_nodes ihn
      LEFT JOIN {{ database }}.CDM_LMS.INSTITUTION_HIERARCHY ih ON ih.ID = ihn.ID
  )
  SELECT DISTINCT
      co.COURSE_ID,
      co.COURSE_NAME,
      co.COURSE_NUMBER,
      NVL(ih.HIERARCHY_ID_SEQ, '||-1||') AS IH_PATH,
      NVL(ih.HIERARCHY_NAME_SEQ, 'All') AS IH_NODE_NAME,
      IFF(YEAR(co.COURSE_CREATION_DATE) < 1970, TO_DATE('1970-01-01'), co.COURSE_CREATION_DATE) AS COURSE_CREATION_DATE,
      NVL(co.TERM_NAME, '-') AS TERM_NAME,
      co.COURSE_ENDED,
      COALESCE(IFF(YEAR(co.START_WEEK_DATE) < 1970, TO_DATE('1970-01-01'), co.START_WEEK_DATE), sa.FIRST_COURSE_WEEK) AS START_WEEK,
      COALESCE(IFF(YEAR(co.END_WEEK_DATE) < 1970, TO_DATE('1970-01-01'), co.END_WEEK_DATE), sa.LAST_COURSE_WEEK) AS END_WEEK,
      COALESCE(IFF(YEAR(co.START_DATE) < 1970, TO_DATE('1970-01-01'), co.START_DATE), sa.FIRST_COURSE_TIME)::DATE AS START_DATE,
      COALESCE(IFF(YEAR(co.END_DATE) < 1970, TO_DATE('1970-01-01'), co.END_DATE), sa.LAST_COURSE_TIME)::DATE AS END_DATE,
      DATEDIFF(week, START_WEEK, END_WEEK) + 1 AS COURSE_WEEKS,
      co.COURSE_DURATION,
      pc.COURSE_ROLE
  FROM course_start_end co
  JOIN student_activity sa ON sa.COURSE_ID = co.COURSE_ID
  LEFT JOIN {{ database }}.CDM_LMS.PERSON_COURSE pc
    ON pc.COURSE_ID = co.COURSE_ID AND pc.COURSE_ROLE IS NOT NULL
  LEFT JOIN institution_hierarchy_all ih ON ih.COURSE_ID = co.COURSE_ID
  WHERE co.COURSE_REPORTABLE = 'Y'
entities:
  - {name: course, column: COURSE_ID, type: foreign}
dimensions:
  - {name: course_name, column: COURSE_NAME, type: categorical, synonyms: [course, class]}
  - {name: course_number, column: COURSE_NUMBER, type: categorical, synonyms: [course code]}
  - {name: term_name, column: TERM_NAME, type: categorical, synonyms: [term, semester]}
  - {name: ih_node_name, column: IH_NODE_NAME, type: categorical, description: "Institutional hierarchy node path names; 'All' when the course has no node", synonyms: [department, college, hierarchy]}
  - {name: course_role, column: COURSE_ROLE, type: categorical, description: "Role code of an enrollment in the course (S = student, P = instructor, ...)", synonyms: [role]}
  - {name: course_duration, column: COURSE_DURATION, type: categorical, description: "'Fixed' when the course or its term has an end date, else 'Continuous'"}
  - {name: course_ended, column: COURSE_ENDED, type: boolean, description: "1 when the course or term end date has passed"}
  - {name: course_creation_date, column: COURSE_CREATION_DATE, type: time, grains: [day, week, month, quarter, year]}
  - {name: start_date, column: START_DATE, type: time, grains: [day, week, month, quarter, year], synonyms: [course start]}
  - {name: end_date, column: END_DATE, type: time, grains: [day, week, month, quarter, year], synonyms: [course end]}
measures:
  - {name: courses, agg: count_distinct, expr: COURSE_ID, unit: courses, synonyms: [number of courses, course count]}
  - {name: ended_courses, agg: count_distinct, expr: "IFF(COURSE_ENDED = 1, COURSE_ID, NULL)", unit: courses}
  - {name: share_ended, agg: ratio, numerator: ended_courses, denominator: courses, unit: ratio}
filters:
  - {name: ongoing, sql: "COURSE_ENDED = 0", description: Courses whose end date has not passed}
  - {name: fixed_duration, sql: "COURSE_DURATION = 'Fixed'"}
```

```yaml
metrics:
  - id: metric.reportable_courses.v1
    display_name: Reportable courses
    description: Top-level courses that have started and have at least one student enrollment.
    owner: Blackboard
    authority: vendor-canonical
    last_reviewed: 2026-10-08
    measure: dataset.course_filters.v1:courses
    synonyms: [how many courses, number of courses, course count]
    example_questions: ["How many courses do we have this term?", "How many courses are there per department?"]
  - id: metric.ongoing_courses.v1
    display_name: Ongoing courses
    description: Reportable courses whose end date has not passed.
    owner: Blackboard
    authority: vendor-canonical
    last_reviewed: 2026-10-08
    measure: dataset.course_filters.v1:courses
    default_filters: [ongoing]
    synonyms: [active courses, current courses, running courses]
    example_questions: ["How many courses are running right now?"]
```

- [ ] **Step 10: Run the full suite**

Run: `.venv/bin/python -m pytest -q tests`
Expected: `95 passed`.

- [ ] **Step 11: Live smoke run**

Compile a representative query and run it through illuminate-mcp `run_query`. illuminate-mcp resolves `CDM_*` without a database prefix, so strip it:

```bash
.venv/bin/python - <<'EOF' > /tmp/course_filters_smoke.sql
from semantic_layer.catalog import load_catalog
from semantic_layer.compiler import compile_query
from semantic_layer.contract import QueryContract
q = compile_query(QueryContract(
    metrics=["metric.reportable_courses.v1", "metric.ongoing_courses.v1"],
    measures=["dataset.course_filters.v1:share_ended"],
    dimensions=["term_name", "start_date__month"],
    order_by=[{"field": "reportable_courses"}], limit=20), load_catalog(), "SMOKEDB")
print(q.sql.replace("SMOKEDB.", ""))
EOF
```

Run the file's contents with illuminate-mcp `run_query`. Expected: status `ok`, columns `TERM_NAME, START_DATE__MONTH, REPORTABLE_COURSES, ONGOING_COURSES, SHARE_ENDED`. During prototyping on 2026-10-08 this returned 17 rows in about 3 s. In the Step 12 PR body, replace `<rows>` and `<seconds>` with this run's row count and elapsed seconds.

- [ ] **Step 12: Commit, push and open the PR**

```bash
git add semantic_layer/pii.py snowflake_client.py semantic_layer/validate.py scripts/build_dictionary_snapshot.py tests/fixtures/cdm_dictionary.json canonical/datasets/course/course_filters.yaml canonical/metrics/course.yaml tests/test_semantic_validate.py tests/test_semantic_definitions.py
git commit -m "feat: offline dataset validation and first ported dataset (course_filters)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin feat/semantic-validate-course-filters
gh pr create --base main --title "feat: offline dataset validation and first ported dataset (course_filters)" --body "$(cat <<'EOF'
**Claude:** Fourth walking-skeleton PR (spec §3.5, §4 dataset 1). Adds offline validation: every column a dataset reads is checked against a committed CDM dictionary snapshot via sqlglot `qualify`, every column its definitions name must be a dataset output, and PII outputs must be declared. Ports bbd-analytics `TFV_FILTERS` as `dataset.course_filters.v1` (source cited in the YAML) with two metrics. `snowflake_client`'s PII list moves to `semantic_layer/pii.py` unchanged. Live smoke run: <rows> rows in <seconds> s via illuminate-mcp.

Reviewable surface: 8 hand-written files, ~330 lines of code, YAML and tests, plus the generated `tests/fixtures/cdm_dictionary.json` (no need to read it line by line; regenerate with `scripts/build_dictionary_snapshot.py`). Spec and plan land separately in the docs PR.

Test plan: `pytest tests` passes locally (Python 3.11).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 13: Stop.** Report the PR URL and wait for approval and merge. Do not start the next task.

### Task 5 (PR 1e): `POST /api/v1/semantic/compile`

**Files:**
- Modify: `lambda_handler.py` (one import; one new section after `dashboard_metric`)
- Modify: `requirements-dev.txt` (add `httpx>=0.27`, needed by FastAPI's `TestClient`)
- Test: `tests/test_semantic_compile_endpoint.py`

**Interfaces:**
- Consumes: `default_catalog` (Task 2), `compile_query`, `CompileError` (Task 3), dataset 1 and metrics (Task 4).
- Produces: `POST /api/v1/semantic/compile`, with a `QueryContract` JSON body and `Authorization: Bearer <JWT>`:
  - 200 `{sql, provenance}`
  - 400 `{detail}` on `CompileError`
  - 422 on a malformed contract
  - 401 on a bad token

  The `/semantic/query` and `/semantic/catalog` PRs in Phase 2 reuse `_semantic_database()`.

- [ ] **Step 1: Branch from the latest `main`**

```bash
git fetch origin && git checkout -b feat/semantic-compile-endpoint origin/main
```

- [ ] **Step 2: Add the test dependency**

Append to `requirements-dev.txt`:

```text
httpx>=0.27
```

Run: `.venv/bin/pip install -r requirements-dev.txt`

- [ ] **Step 3: Write the failing tests**

```python
# tests/test_semantic_compile_endpoint.py
import pytest
from fastapi.testclient import TestClient

import lambda_handler

AUTH = {"Authorization": "Bearer test"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_DATABASE", "TESTDB")
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: {"sub": "u1"})
    return TestClient(lambda_handler.app)


def test_compiles_a_metric(client):
    r = client.post("/api/v1/semantic/compile", headers=AUTH,
                    json={"metrics": ["metric.reportable_courses.v1"], "dimensions": ["term_name"]})
    assert r.status_code == 200
    body = r.json()
    assert "TESTDB.CDM_LMS.COURSE" in body["sql"]
    assert body["provenance"] == {
        "governed": True,
        "datasets": ["dataset.course_filters.v1"],
        "metrics": ["metric.reportable_courses.v1"],
        "measures": ["dataset.course_filters.v1:courses"],
    }


def test_unknown_metric_is_a_400(client):
    r = client.post("/api/v1/semantic/compile", headers=AUTH, json={"metrics": ["metric.nope.v1"]})
    assert r.status_code == 400
    assert "unknown metric" in r.json()["detail"]


def test_malformed_contract_is_a_422(client):
    r = client.post("/api/v1/semantic/compile", headers=AUTH, json={"metrics": ["metric.x.v1"], "limit": 5000})
    assert r.status_code == 422


def test_requires_a_valid_token(client, monkeypatch):
    monkeypatch.setattr(lambda_handler, "_get_user_from_token", lambda a: None)
    r = client.post("/api/v1/semantic/compile", headers=AUTH, json={"metrics": ["metric.reportable_courses.v1"]})
    assert r.status_code == 401
```

- [ ] **Step 4: Run them to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_semantic_compile_endpoint.py`
Expected: 4 failed (404 Not Found for the route; the 401 test also fails on 404).

- [ ] **Step 5: Add the endpoint**

`_semantic_database()` prefers `SNOWFLAKE_DATABASE` so tests and callers don't import `chat_engine`, which fetches the data dictionary over HTTP at import time.

```diff
@@ -20,6 +20,8 @@
 from fastapi.responses import StreamingResponse
 from pydantic import BaseModel
 
+from semantic_layer.contract import QueryContract
+
 # JWT validation
 from jose import jwt, JWTError
 import requests as http_requests
@@ -845,6 +847,37 @@
     except Exception as e:
         logger.error(f"Dashboard metric {request.metric_id} failed: {e}")
         return {"error": str(e), "sql_attempted": sql}
+
+
+# =============================================================================
+# Semantic layer
+# =============================================================================
+
+
+def _semantic_database() -> str:
+    """Snowflake database the semantic layer's {{ database }} placeholder resolves to."""
+    db = os.environ.get("SNOWFLAKE_DATABASE")
+    if db:
+        return db
+    from chat_engine import _database
+    return _database
+
+
+@app.post("/api/v1/semantic/compile")
+async def semantic_compile(contract: QueryContract, authorization: str = Header(...)) -> dict:
+    """Compile a semantic query contract to SQL without executing it."""
+    user = _get_user_from_token(authorization)
+    if not user:
+        raise HTTPException(status_code=401, detail="Invalid or expired token")
+
+    from semantic_layer.catalog import default_catalog
+    from semantic_layer.compiler import CompileError, compile_query
+
+    try:
+        compiled = compile_query(contract, default_catalog(), _semantic_database())
+    except CompileError as e:
+        raise HTTPException(status_code=400, detail=str(e))
+    return compiled.model_dump()
 
 
 # =============================================================================
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest -q tests`
Expected: `99 passed` (one `StarletteDeprecationWarning` from the installed FastAPI `TestClient` is expected).

- [ ] **Step 7: Commit, push and open the PR**

```bash
git add lambda_handler.py requirements-dev.txt tests/test_semantic_compile_endpoint.py
git commit -m "feat: POST /api/v1/semantic/compile

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin feat/semantic-compile-endpoint
gh pr create --base main --title "feat: POST /api/v1/semantic/compile" --body "$(cat <<'EOF'
**Claude:** Last walking-skeleton PR (spec §5.3). Exposes the compiler: a semantic query contract in, `{sql, provenance}` out, without executing. The frontend's View SQL panels will use it so the SQL shown is the SQL that runs. Auth matches the existing endpoints; a bad contract is 422, a contract the catalog can't satisfy is 400 with the compiler's message.

Reviewable surface: 3 files, ~80 lines of code and tests. Spec and plan land separately in the docs PR.

Test plan: `pytest tests` passes locally (Python 3.11).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 8: Stop.** Report the PR URL and wait for approval and merge. Do not start the next task.

---

## After Phase 1

When PR 1e merges, write the Phase 2 detailed plan (`/semantic/query`, `/semantic/catalog`) against what merged. The roadmap lists every remaining unit.
