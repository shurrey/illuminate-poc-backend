"""The semantic query contract: what a caller asks for, and what compiling it returns."""

from __future__ import annotations

from datetime import date
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StrictBool, StrictInt, model_validator

FilterOp = Literal[
    "eq", "neq", "in", "not_in", "gt", "gte", "lt", "lte",
    "between", "is_null", "not_null", "contains",
]
# StrictBool precedes StrictInt so JSON true stays a bool rather than becoming 1.
FilterValue = Union[StrictBool, StrictInt, FiniteFloat, str]

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
    overlays: list[str] = Field(default_factory=list, description="Tenant overlays applied, as '<target>@v<version>'")


class CompiledQuery(BaseModel):
    sql: str
    provenance: Provenance
