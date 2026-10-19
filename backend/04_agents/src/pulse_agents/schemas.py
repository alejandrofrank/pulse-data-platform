"""Validated intent contract, independent of database access and model providers."""

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_MONTHS = 120
MAX_FIRM_NAME = 200
Month = Annotated[str, Field(strict=True, pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]


class Metric(StrEnum):
    REVENUE_EUR = "revenue_eur"
    CLIENTES_ACTIVOS = "clientes_activos"
    CLIENTES_NUEVOS = "clientes_nuevos"
    CLIENTES_BAJA = "clientes_baja"
    DECLARACIONES_RENTA = "declaraciones_renta"
    HORAS_TRABAJADAS = "horas_trabajadas"


class Operation(StrEnum):
    SERIES = "series"
    TOTAL = "total"
    RANKING = "ranking"
    DECLINE = "decline"


def month_count(start: str, end: str) -> int:
    """Inclusive number of calendar months; inputs must already be validated."""
    return (int(end[:4]) - int(start[:4])) * 12 + int(end[5:]) - int(start[5:]) + 1


class QueryPlan(BaseModel):
    """Missing intent fields remain missing so execution can ask for clarification.

    Dates are inclusive and never inferred here. Decline means a strict decrease
    in a firm's active-client stock in each consecutive calendar month: three
    declines need four non-null observations. A longer interval searches all
    qualifying windows, including overlapping windows in a longer decline.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["query", "investigate", "clarify", "unsupported"]
    metric: Metric | None = None
    operation: Operation | None = None
    start: Month | None = None
    end: Month | None = None
    firm_id: Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)] | None = None
    firm_name: Annotated[str, Field(strict=True, min_length=1, max_length=MAX_FIRM_NAME)] | None = (
        None
    )
    months_declining: Annotated[int, Field(strict=True, ge=1, le=12)] = 3
    clarification: Annotated[str, Field(strict=True, max_length=500)] = ""

    @field_validator("start", "end")
    @classmethod
    def valid_calendar_month(cls, value: str | None) -> str | None:
        if value is not None:
            date.fromisoformat(value + "-01")  # Reject year 0000 as well as invalid months.
        return value

    @field_validator("firm_name")
    @classmethod
    def valid_name(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if not value or "\x00" in value:
                raise ValueError("firm_name must contain non-NUL, non-whitespace text")
        return value

    @model_validator(mode="after")
    def valid_period(self) -> Self:
        if self.start is not None and self.end is not None:
            months = month_count(self.start, self.end)
            if not 1 <= months <= MAX_MONTHS:
                raise ValueError("Period must be ordered and span at most 120 inclusive months")
        return self


class InvestigationTarget(BaseModel):
    """Explicit controls require no natural-language interpretation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    firm_id: Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)]
    month: Month

    @field_validator("month")
    @classmethod
    def valid_calendar_month(cls, value: str) -> str:
        date.fromisoformat(value + "-01")
        return value


def query_plan_json_schema() -> dict:
    """Complete closed schema with every field required for strict-output adapters.

    Nullable intent fields can explicitly be null. Runtime defaults are retained
    on QueryPlan but omitted from this schema. Provider-specific adaptation of
    validation keywords belongs to the caller; runtime validation always applies.
    """
    schema = QueryPlan.model_json_schema()
    schema["required"] = list(schema["properties"])
    for field in schema["properties"].values():
        field.pop("default", None)
    return schema
