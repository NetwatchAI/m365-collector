"""Collector interface and shared types."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, ClassVar

import structlog

from m365c.config import TenantConfig
from m365c.graph import TenantGraph


@dataclass(frozen=True, slots=True)
class MetricSample:
    """One gauge value. ``customer`` and ``tenant`` labels are added by the exporter."""

    name: str
    value: float
    labels: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "labels": dict(self.labels)}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> MetricSample:
        return cls(
            name=str(data["name"]),
            value=float(data["value"]),
            labels={str(k): str(v) for k, v in dict(data.get("labels", {})).items()},
        )


@dataclass
class CollectorContext:
    tenant: TenantConfig
    graph: TenantGraph
    log: structlog.stdlib.BoundLogger
    # Per (tenant, collector) memory that survives between runs, e.g. warn-once flags.
    state: dict[str, Any]


class BaseCollector(abc.ABC):
    name: ClassVar[str]
    default_interval: ClassVar[float]
    required_permissions: ClassVar[tuple[str, ...]]
    metric_help: ClassVar[dict[str, str]]

    @abc.abstractmethod
    async def collect(self, ctx: CollectorContext) -> list[MetricSample]: ...


def to_timestamp(value: str | None) -> float | None:
    """ISO-8601 datetime or ``YYYY-MM-DD`` date to Unix seconds. Empty/None -> None."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        if len(value) == 10:
            d = date.fromisoformat(value)
            return datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp()
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def to_bool(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def to_float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        return float(value)
    except ValueError:
        return None


def bool_label(value: bool) -> str:
    return "true" if value else "false"
