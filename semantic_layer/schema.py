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
