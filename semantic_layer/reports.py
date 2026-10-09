"""Report definitions (canonical/reports/*.yaml): pages of visuals, each a set of named query contracts.

Filter-bar values merge into a visual's contracts here, so validation and the run endpoint share one
implementation.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .catalog import CANONICAL_DIR
from .compiler import CompileError, _output_name, compile_query
from .contract import ContractFilter, FilterValue, QueryContract, TimeRange
from .schema import Catalog

REPORTS_DIR = CANONICAL_DIR / "reports"
TRANSFORM_KINDS = {"period_over_period", "percent_of_total", "unpivot", "top_n_other"}
_VALIDATION_DB = "VALIDATION_DB"


class ReportError(ValueError):
    """A report file is malformed or duplicates an id."""


class _Definition(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReportFilter(_Definition):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    control: Literal["multi_select", "select", "date_range"]
    dimension: Optional[str] = None
    time_dimension: Optional[str] = None
    default: Optional[Union[Literal["current_term", "last_30_days"], list[FilterValue]]] = None

    @model_validator(mode="after")
    def _targets(self) -> "ReportFilter":
        if self.control == "date_range" and not self.time_dimension:
            raise ValueError(f"filter {self.id}: a date_range needs a time_dimension")
        if self.control != "date_range" and not self.dimension:
            raise ValueError(f"filter {self.id}: needs a dimension")
        return self


class Visual(_Definition):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: Literal["kpi", "bar", "line", "combo", "pie", "table", "pivot", "heatmap", "histogram", "scatter", "treemap", "text"]
    title: str
    text: str = ""
    # Query name -> a QueryContract, plus an optional `time_dimension` the date-range filter applies to.
    queries: dict[str, dict[str, Any]] = Field(default_factory=dict)
    transform: Optional[dict[str, Any]] = None
    encode: dict[str, Any] = Field(default_factory=dict)
    filters_ignored: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _shape(self) -> "Visual":
        if (self.type == "text") == bool(self.queries):
            raise ValueError(f"visual {self.id}: text visuals take no queries; every other visual needs one")
        if self.transform is not None and self.transform.get("kind") not in TRANSFORM_KINDS:
            raise ValueError(f"visual {self.id}: unknown transform {self.transform.get('kind')!r}")
        return self


class Page(_Definition):
    title: str
    visuals: list[Visual]


class Report(_Definition):
    id: str = Field(pattern=r"^report\.[a-z][a-z0-9_]*\.v[0-9]+$")
    title: str
    area: Literal["learning", "teaching", "leading"]
    description: str
    filters: list[ReportFilter] = Field(default_factory=list)
    pages: list[Page]

    @model_validator(mode="after")
    def _references(self) -> "Report":
        ids = [v.id for p in self.pages for v in p.visuals]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{self.id}: visual ids must be unique")
        filter_ids = {f.id for f in self.filters}
        for v in self.visuals():
            if not set(v.filters_ignored) <= filter_ids:
                raise ValueError(f"{self.id}: visual {v.id} ignores unknown filters {set(v.filters_ignored) - filter_ids}")
        return self

    def visuals(self) -> list[Visual]:
        return [v for p in self.pages for v in p.visuals]

    def visual(self, visual_id: str) -> Optional[Visual]:
        return next((v for v in self.visuals() if v.id == visual_id), None)


def load_reports(root: Path = REPORTS_DIR) -> dict[str, Report]:
    reports: dict[str, Report] = {}
    for path in sorted(root.glob("*.yaml")):
        try:
            report = Report(**yaml.safe_load(path.read_text()))
        except ValidationError as e:
            raise ReportError(f"{path}: {e}") from e
        if report.id in reports:
            raise ReportError(f"{path}: duplicate report id {report.id}")
        reports[report.id] = report
    return reports


def query_contract(spec: dict[str, Any]) -> tuple[QueryContract, Optional[str]]:
    """A visual query's contract and its optional time dimension override."""
    spec = dict(spec)
    time_dimension = spec.pop("time_dimension", None)
    return QueryContract(**spec), time_dimension


def merged_contract(report: Report, visual: Visual, query_name: str, values: dict[str, Any], catalog: Catalog,
                    database: str = _VALIDATION_DB) -> tuple[QueryContract, list[str]]:
    """The query with each filter-bar value applied, and the filter ids it ignored because its datasets
    cannot reach the filter's dimension (found by compiling with and without the filter)."""
    contract, time_dimension = query_contract(visual.queries[query_name])
    ignored: list[str] = []
    for f in report.filters:
        value = values.get(f.id)
        if f.id in visual.filters_ignored or not value:
            continue
        if f.control == "date_range":
            if not (value.get("start") or value.get("end")):
                continue
            update = {"time_range": TimeRange(dimension=time_dimension or f.time_dimension,
                                              start=value.get("start"), end=value.get("end"))}
        else:
            values_list = value if isinstance(value, list) else [value]
            update = {"filters": [*contract.filters, ContractFilter(dimension=f.dimension, op="in", values=values_list)]}
        candidate = contract.model_copy(update=update)
        try:
            compile_query(candidate, catalog, database, allow_identity=True)
        except CompileError:
            ignored.append(f.id)
            continue
        contract = candidate
    return contract, ignored


def _output_names(contract: QueryContract, catalog: Catalog) -> set[str]:
    names = {_output_name(d) for d in contract.dimensions}
    names |= {m.split(".")[1] for m in contract.metrics}
    names |= {m.rpartition(":")[2] for m in contract.measures}
    return names


def validate_report(report: Report, catalog: Catalog, database: str = _VALIDATION_DB) -> list[str]:
    """Problems as '<visual>/<query>: <message>'; empty when every query compiles with every filter applied."""
    sample = {f.id: ({"start": "2026-01-01", "end": "2026-01-31"} if f.control == "date_range" else ["sample"])
              for f in report.filters}
    problems = []
    for v in report.visuals():
        returned: set[str] = set()
        for name in v.queries:
            try:
                contract, _ = query_contract(v.queries[name])
                compile_query(contract, catalog, database, allow_identity=True)
                merged, _ = merged_contract(report, v, name, sample, catalog, database)
                compile_query(merged, catalog, database, allow_identity=True)
            except (CompileError, ValidationError, ValueError) as e:
                problems.append(f"{v.id}/{name}: {e}")
                continue
            returned |= _output_names(contract, catalog)
        if v.transform is None and returned:
            missing = sorted(c for c in v.encode.values() if isinstance(c, str) and c not in returned)
            if missing:
                problems.append(f"{v.id}/encode: columns {missing} are not returned by its queries")
    return problems


def _as_date(value: Any) -> Optional[date]:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def resolve_defaults(report: Report, today: date, terms: list[dict[str, Any]]) -> dict[str, Any]:
    """Filter id -> its default value. current_term: the terms spanning today, else every term sharing the
    latest end date before today. terms rows carry term_name, term_start and term_end."""
    dated = [(t["term_name"], _as_date(t["term_start"]), _as_date(t["term_end"])) for t in terms]
    dated = [t for t in dated if t[1] and t[2]]
    current = [name for name, start, end in dated if start <= today <= end]
    if not current:
        ended = [end for _, _, end in dated if end < today]
        latest = max(ended, default=None)
        current = [name for name, _, end in dated if end == latest]
    defaults: dict[str, Any] = {}
    for f in report.filters:
        if f.default == "current_term":
            if current:
                defaults[f.id] = current
        elif f.default == "last_30_days":
            defaults[f.id] = {"start": (today - timedelta(days=30)).isoformat(), "end": today.isoformat()}
        elif f.default is not None:
            defaults[f.id] = f.default
    return defaults
