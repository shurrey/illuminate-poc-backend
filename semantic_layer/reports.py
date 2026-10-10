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
from .compiler import CompileError, _output_name, compile_query, filter_dimension
from .contract import ContractFilter, FilterValue, QueryContract, TimeRange
from .schema import Catalog

REPORTS_DIR = CANONICAL_DIR / "reports"
TRANSFORM_KINDS = {"period_over_period", "percent_of_total", "unpivot", "top_n_other", "side_by_side", "per_weekday_average",
                   "average_by", "part_of_whole", "join"}
_VALIDATION_DB = "VALIDATION_DB"


class ReportError(ValueError):
    """A report file is malformed or duplicates an id."""


class ReportValueError(ValueError):
    """A filter-bar value has the wrong shape or type for its filter."""


class _Definition(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FilterDimension(_Definition):
    """One way a filter can apply: a dimension (dataset-qualified, or bare to resolve on the query's own
    datasets) and how values match it. `contains` and `path` take exactly one value; `path` matches a
    ';||A||B||'-style hierarchy list on the whole prefix from level 1, built from the depends_on values."""
    ref: str
    op: Literal["in", "contains", "path"] = "in"


class ReportFilter(_Definition):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    # number: a value (e.g. a threshold) that queries use through param_filters, not a filter on its own.
    # choice: one of `options` (default `[option]`) that queries use through measure_from.
    control: Literal["multi_select", "select", "date_range", "number", "choice"]
    options: list[str] = Field(default_factory=list)
    dimension: Optional[str] = None
    # Ordered alternatives to `dimension`: a query applies the first one its datasets can reach.
    dimensions: list[FilterDimension] = Field(default_factory=list)
    # Filters whose values narrow this filter's options (a hierarchy level's parents).
    depends_on: list[str] = Field(default_factory=list)
    # Option values the filter does not offer (placeholders such as '-' for "no node at this level").
    exclude_values: list[str] = Field(default_factory=list)
    time_dimension: Optional[str] = None
    default: Optional[Union[Literal["current_term", "last_30_days", "previous_30_days", "last_month", "month_before_last"], list[FilterValue], float]] = None

    @model_validator(mode="after")
    def _targets(self) -> "ReportFilter":
        if self.control == "date_range" and not self.time_dimension:
            raise ValueError(f"filter {self.id}: a date_range needs a time_dimension")
        if self.control == "number":
            if not isinstance(self.default, (int, float)):
                raise ValueError(f"filter {self.id}: a number control needs a numeric default")
            return self
        if self.control == "choice":
            if not self.options or not (isinstance(self.default, list) and len(self.default) == 1
                                        and self.default[0] in self.options):
                raise ValueError(f"filter {self.id}: a choice needs options and a default of [one option]")
            return self
        if self.control != "date_range" and bool(self.dimension) == bool(self.dimensions):
            raise ValueError(f"filter {self.id}: needs a dimension or a list of dimensions, not both")
        return self

    def alternatives(self) -> list[FilterDimension]:
        return [FilterDimension(ref=self.dimension)] if self.dimension else list(self.dimensions)


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


class QuerySpec(BaseModel):
    """A visual query: its contract, the time dimension its date range applies to, and which date-range
    filter it takes (default: the report's first)."""
    contract: QueryContract
    time_dimension: Optional[str] = None
    date_filter: Optional[str] = None
    # {start, end}: the date range applies as start <= range end AND end >= range start; with a
    # time_dimension too, it bounds that dimension as well.
    time_overlap: Optional[dict[str, str]] = None
    # Filters this query alone does not take (e.g. the whole in a part-of-whole visual).
    filters_ignored: list[str] = []
    # [{dimension, op, param}]: a filter whose value is a number control's current value.
    param_filters: list[dict[str, str]] = []
    # {filters, dimensions, as}: group by dimensions[k], k = how many leading filters have a value;
    # /run returns that column as `as`.
    child_of: Optional[dict[str, Any]] = None
    # {filter, measures: {option: ref}, as}: the chosen option's measure first; /run returns it as `as`.
    measure_from: Optional[dict[str, Any]] = None


def query_contract(spec: dict[str, Any]) -> QuerySpec:
    spec = dict(spec)
    time_dimension, date_filter = spec.pop("time_dimension", None), spec.pop("date_filter", None)
    time_overlap, filters_ignored = spec.pop("time_overlap", None), spec.pop("filters_ignored", [])
    param_filters, child_of = spec.pop("param_filters", []), spec.pop("child_of", None)
    measure_from = spec.pop("measure_from", None)
    if measure_from and measure_from.get("measures"):
        # The first option stands in until merged_contract puts the chosen one in its place.
        spec["measures"] = [next(iter(measure_from["measures"].values())), *spec.get("measures", [])]
    return QuerySpec(contract=QueryContract(**spec), time_dimension=time_dimension, date_filter=date_filter,
                     time_overlap=time_overlap, filters_ignored=filters_ignored, param_filters=param_filters,
                     child_of=child_of, measure_from=measure_from)


def _chosen_measure(report: Report, measure_from: dict[str, Any], values: dict[str, Any]) -> str:
    """The measure ref for the choice's current value (or its default). Raises ReportValueError for a value
    that is not one of its options."""
    choice = next((f for f in report.filters if f.id == measure_from["filter"] and f.control == "choice"), None)
    if choice is None:
        raise ReportValueError(f"measure_from: {measure_from['filter']!r} is not a choice control")
    raw = values.get(choice.id) or choice.default
    picked = raw[0] if isinstance(raw, list) and raw else raw
    if picked not in measure_from["measures"]:
        raise ReportValueError(f"filter {choice.id}: {picked!r} is not one of {sorted(measure_from['measures'])}")
    return measure_from["measures"][picked]


def _child_dimension(child_of: dict[str, Any], values: dict[str, Any]) -> str:
    depth = 0
    for fid in child_of["filters"]:
        if not values.get(fid):
            break
        depth += 1
    dims = child_of["dimensions"]
    return dims[min(depth, len(dims) - 1)]


def output_renames(report: Report, visual: Visual, query_name: str, values: dict[str, Any]) -> dict[str, str]:
    """Result column -> the name /run returns it under (child_of and measure_from columns become their alias)."""
    spec = query_contract(visual.queries[query_name])
    renames = {}
    if spec.child_of:
        renames[_child_dimension(spec.child_of, values).rpartition(":")[2]] = spec.child_of["as"]
    if spec.measure_from:
        renames[_chosen_measure(report, spec.measure_from, values).rpartition(":")[2]] = spec.measure_from["as"]
    return renames


def _coerce(filter_id: str, values: list, dim_type: str) -> list:
    """Values as the dimension's type; the frontend sends strings from the URL and option lists."""
    out = []
    for v in values:
        if dim_type == "boolean":
            if isinstance(v, bool):
                out.append(v)
            elif str(v).lower() in ("true", "false"):
                out.append(str(v).lower() == "true")
            else:
                raise ReportValueError(f"filter {filter_id}: {v!r} is not true or false")
        elif dim_type == "numeric":
            try:
                number = float(v)
            except (TypeError, ValueError):
                raise ReportValueError(f"filter {filter_id}: {v!r} is not a number") from None
            out.append(int(number) if number.is_integer() else number)
        else:
            out.append(str(v) if not isinstance(v, (bool, int, float)) else v)
    return out


def merged_contract(report: Report, visual: Visual, query_name: str, values: dict[str, Any], catalog: Catalog,
                    database: str = _VALIDATION_DB) -> tuple[QueryContract, list[str]]:
    """The query with each filter-bar value applied, and the ids of filters ignored because the query's
    datasets cannot reach their dimension. Raises ReportValueError for a value of the wrong shape or type."""
    spec = query_contract(visual.queries[query_name])
    contract, time_dimension = spec.contract, spec.time_dimension
    date_filters = [f.id for f in report.filters if f.control == "date_range"]
    date_filter = spec.date_filter or (date_filters[0] if date_filters else None)
    ignored: list[str] = []
    filters = list(contract.filters)
    time_range = contract.time_range
    for f in report.filters:
        value = values.get(f.id)
        if f.control in ("number", "choice") or f.id in visual.filters_ignored or f.id in spec.filters_ignored or not value:
            continue
        if f.control == "date_range":
            if f.id != date_filter:
                continue
            if not isinstance(value, dict):
                raise ReportValueError(f"filter {f.id}: a date range is {{start, end}}")
            if not (value.get("start") or value.get("end")):
                continue
            if spec.time_overlap:
                start_ref, end_ref = spec.time_overlap["start"], spec.time_overlap["end"]
                dims = [filter_dimension(contract, r, catalog) for r in (start_ref, end_ref)]
                if any(d is None or d.type != "time" for d in dims):
                    ignored.append(f.id)
                    continue
                if value.get("end"):
                    filters.append(ContractFilter(dimension=start_ref, op="lte", values=[str(value["end"])]))
                if value.get("start"):
                    filters.append(ContractFilter(dimension=end_ref, op="gte", values=[str(value["start"])]))
                # With a time_dimension as well, the range also bounds that dimension (e.g. activity in the range).
                if not time_dimension:
                    continue
            ref = time_dimension or f.time_dimension
            dim = filter_dimension(contract, ref, catalog)
            if dim is None or dim.type != "time":
                ignored.append(f.id)
                continue
            try:
                time_range = TimeRange(dimension=ref, start=value.get("start"), end=value.get("end"))
            except ValidationError as e:
                raise ReportValueError(f"filter {f.id}: {e.errors()[0]['msg']}") from None
        else:
            raw = value if isinstance(value, list) else [value]
            reached = next(((alt, dim) for alt in f.alternatives()
                            if (dim := filter_dimension(contract, alt.ref, catalog)) is not None), None)
            if reached is None or (reached[0].op != "in" and len(raw) != 1):
                ignored.append(f.id)
                continue
            alt, dim = reached
            if alt.op == "path":
                parents = [values[p][0] for p in f.depends_on if isinstance(values.get(p), list) and len(values[p]) == 1]
                filters.append(ContractFilter(dimension=alt.ref, op="contains",
                                              values=[";||" + "||".join(map(str, [*parents, raw[0]])) + "||"]))
            else:
                match = [str(raw[0])] if alt.op == "contains" else _coerce(f.id, raw, dim.type)
                filters.append(ContractFilter(dimension=alt.ref, op=alt.op, values=match))
    for pf in spec.param_filters:
        param = next((f for f in report.filters if f.id == pf["param"] and f.control == "number"), None)
        if param is None:
            raise ReportValueError(f"query {query_name}: param {pf['param']!r} is not a number control")
        raw = values.get(param.id) or [param.default]
        filters.append(ContractFilter(dimension=pf["dimension"], op=pf["op"],
                                      values=_coerce(param.id, raw if isinstance(raw, list) else [raw], "numeric")[:1]))
    update: dict[str, Any] = {"filters": filters, "time_range": time_range}
    order_by = list(contract.order_by)
    if spec.child_of:
        ref = _child_dimension(spec.child_of, values)
        update["dimensions"] = [ref, *contract.dimensions]
        order_by = _renamed_order(order_by, spec.child_of["as"], ref)
    if spec.measure_from:
        ref = _chosen_measure(report, spec.measure_from, values)
        update["measures"] = [ref, *contract.measures[1:]]
        order_by = _renamed_order(order_by, spec.measure_from["as"], ref)
    update["order_by"] = order_by
    return contract.model_copy(update=update), ignored


def _renamed_order(order_by: list, alias: str, ref: str) -> list:
    name = ref.rpartition(":")[2]
    return [o.model_copy(update={"field": name}) if o.field == alias else o for o in order_by]


def _dimension_named(ref: str, catalog: Catalog):
    """The dimension a ref names: on its dataset when qualified, else the first dataset that has it."""
    ds_id, _, name = ref.rpartition(":")
    if ds_id:
        ds = catalog.datasets.get(ds_id)
        return ds.dimension(name) if ds else None
    return next((d for ds in catalog.datasets.values() if (d := ds.dimension(name)) is not None), None)


def _filter_target(f: ReportFilter, catalog: Catalog):
    """The dimension a filter definition names (its first alternative); None when any alternative does
    not exist or a single dimension or date range is not dataset-qualified."""
    if f.control == "date_range" or f.dimension:
        ref = f.time_dimension if f.control == "date_range" else f.dimension
        return _dimension_named(ref, catalog) if ":" in ref else None
    found = [_dimension_named(alt.ref, catalog) for alt in f.alternatives()]
    return found[0] if all(found) else None


def _output_names(contract: QueryContract, catalog: Catalog) -> set[str]:
    names = {_output_name(d) for d in contract.dimensions}
    names |= {m.split(".")[1] for m in contract.metrics}
    names |= {m.rpartition(":")[2] for m in contract.measures}
    return names


_SAMPLE = {"boolean": [True], "numeric": [1], "time": ["2026-01-01"]}
# Encode keys that name result columns; the rest (unit, donut, stacked…) are display options.
_ENCODE_COLUMNS = ("x", "y", "value", "label", "series", "x_sort", "columns")


def validate_report(report: Report, catalog: Catalog, database: str = _VALIDATION_DB) -> list[str]:
    """Problems as '<visual>/<query>: …' or 'filter <id>: …'; empty when every query compiles with every
    filter applied, every filter names a real dimension some visual applies, and every transform reads
    queries the visual has."""
    problems = []
    sample: dict[str, Any] = {}
    for f in report.filters:
        if f.control in ("number", "choice"):
            continue
        dim = _filter_target(f, catalog)
        unknown_parents = sorted(set(f.depends_on) - {g.id for g in report.filters})
        if unknown_parents:
            problems.append(f"filter {f.id}: depends on unknown filters {unknown_parents}")
        if dim is None:
            refs = [f.time_dimension] if f.control == "date_range" else [a.ref for a in f.alternatives()]
            problems.append(f"filter {f.id}: {', '.join(refs)} is not a dimension that exists (single dimensions must be dataset-qualified)")
            continue
        sample[f.id] = ({"start": "2026-01-01", "end": "2026-01-31"} if f.control == "date_range"
                        else _SAMPLE.get(dim.type, ["sample"]))
    date_ids = [f.id for f in report.filters if f.control == "date_range"]
    applied: set[str] = set()
    for v in report.visuals():
        returned: set[str] = set()
        for name in v.queries:
            try:
                spec = query_contract(v.queries[name])
                contract = spec.contract
                if spec.date_filter is not None and spec.date_filter not in date_ids:
                    raise ValueError(f"date_filter {spec.date_filter!r} is not one of the report's date ranges {date_ids}")
                if spec.measure_from:
                    _check_measure_from(report, spec)
                if not spec.child_of and not spec.measure_from:
                    compile_query(contract, catalog, database, allow_identity=True)
                merged, ignored = merged_contract(report, v, name, sample, catalog, database)
                compile_query(merged, catalog, database, allow_identity=True)
                if spec.child_of:
                    _check_child_of(report, spec, catalog, database)
            except (CompileError, ValidationError, ValueError) as e:
                problems.append(f"{v.id}/{name}: {e}")
                continue
            own_date = spec.date_filter or (date_ids[0] if date_ids else None)
            applied |= {fid for fid in sample if fid not in ignored and fid not in v.filters_ignored
                        and fid not in spec.filters_ignored and (fid not in date_ids or fid == own_date)}
            aliased = [x for x in (spec.child_of, spec.measure_from) if x]
            returned |= _output_names(contract, catalog) | {x["as"] for x in aliased}
        if v.transform is not None:
            inputs = [v.transform.get(k) for k in ("value", "baseline", "query", "days_query") if v.transform.get(k)]
            named = v.transform.get("queries") or []
            inputs += list(named.values()) if isinstance(named, dict) else list(named)
            missing = sorted(str(q) for q in inputs if q not in v.queries)
            if missing:
                problems.append(f"{v.id}/transform: reads queries {missing} the visual does not have")
        elif returned:
            named = [v.encode.get(k) for k in _ENCODE_COLUMNS]
            columns = [c for n in named for c in (n if isinstance(n, list) else [n]) if isinstance(c, str)]
            missing = sorted(c for c in columns if c not in returned)
            if missing:
                problems.append(f"{v.id}/encode: columns {missing} are not returned by its queries")
    for f in report.filters:
        if f.id in sample and f.id not in applied:
            problems.append(f"filter {f.id}: no visual can apply it")
    return problems


def _check_measure_from(report: Report, spec: QuerySpec) -> None:
    mf = spec.measure_from
    choice = next((f for f in report.filters if f.id == mf.get("filter") and f.control == "choice"), None)
    if choice is None or not mf.get("as") or set(mf.get("measures", {})) != set(choice.options):
        raise ValueError(f"measure_from needs `as`, a choice filter and one measure per option "
                         f"({choice.options if choice else 'no such choice'})")


def _check_child_of(report: Report, spec: QuerySpec, catalog: Catalog, database: str) -> None:
    child_of, ids = spec.child_of, {f.id for f in report.filters}
    filters, dims = child_of.get("filters", []), child_of.get("dimensions", [])
    if not child_of.get("as") or not dims or len(filters) != len(dims) or not set(filters) <= ids:
        raise ValueError(f"child_of needs `as` and equal-length filters (known: {sorted(ids)}) and dimensions")
    order = [o for o in spec.contract.order_by if o.field != child_of["as"]]
    for ref in dims:
        compile_query(spec.contract.model_copy(update={"dimensions": [ref, *spec.contract.dimensions], "order_by": order}),
                      catalog, database, allow_identity=True)


def _as_date(value: Any) -> Optional[date]:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def resolve_defaults(report: Report, today: date, terms: list[dict[str, Any]]) -> dict[str, Any]:
    """Filter id -> its default value. current_term: the terms spanning today, else every term sharing the
    latest end date before today (rows carry term_name, term_start, term_end). last_30_days and
    previous_30_days are consecutive 30-day windows ending today; last_month and month_before_last are
    the two latest completed calendar months."""
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
            defaults[f.id] = {"start": (today - timedelta(days=29)).isoformat(), "end": today.isoformat()}
        elif f.default == "previous_30_days":
            defaults[f.id] = {"start": (today - timedelta(days=59)).isoformat(), "end": (today - timedelta(days=30)).isoformat()}
        elif f.default in ("last_month", "month_before_last"):
            end = today.replace(day=1) - timedelta(days=1)
            if f.default == "month_before_last":
                end = end.replace(day=1) - timedelta(days=1)
            defaults[f.id] = {"start": end.replace(day=1).isoformat(), "end": end.isoformat()}
        elif isinstance(f.default, (int, float)):
            defaults[f.id] = [f.default]
        elif f.default is not None:
            defaults[f.id] = f.default
    return defaults
