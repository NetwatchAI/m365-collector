"""Collector interface and shared types."""

from __future__ import annotations

import abc
from collections.abc import Callable
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


# Any one of these Graph application roles satisfies the requirement.
Requirement = tuple[str, ...]


# Broader Graph roles that Graph also accepts for the calls m365c makes with a role. They
# satisfy a requirement but are never recommended: `m365c permissions` lists only the least
# privileged roles. Legacy (Zabbix) app registrations often hold only Directory.Read.All.
BROADER_ROLES: dict[str, tuple[str, ...]] = {
    # GET /subscribedSkus (LICENSE_READ is "Organization.Read.All or LicenseAssignment.Read.All")
    "LicenseAssignment.Read.All": (
        "LicenseAssignment.ReadWrite.All",
        "Organization.ReadWrite.All",
        "Directory.Read.All",
        "Directory.ReadWrite.All",
    ),
    # GET /users
    "User.Read.All": ("User.ReadWrite.All", "Directory.Read.All", "Directory.ReadWrite.All"),
}


def accepted_roles(requirement: Requirement) -> set[str]:
    """The requirement's roles plus the broader roles that also satisfy it."""
    return {r for role in requirement for r in (role, *BROADER_ROLES.get(role, ()))}


def satisfied(roles: frozenset[str], requirement: Requirement) -> bool:
    return bool(roles & accepted_roles(requirement))


@dataclass(frozen=True)
class TenantOptions:
    """Per-tenant collector options, resolved from settings and the tenant's overrides."""

    report_period: str = "D7"
    onedrive_source: str = "report"


def _no_peers(collector: str) -> tuple[MetricSample, ...]:
    return ()


@dataclass
class CollectorContext:
    tenant: TenantConfig
    graph: TenantGraph
    log: structlog.stdlib.BoundLogger
    # Per (tenant, collector) memory that survives between runs, e.g. warn-once flags.
    state: dict[str, Any]
    # Graph application roles in the tenant's token; None when unknown (not a JWT).
    roles: frozenset[str] | None = None
    options: TenantOptions = field(default_factory=TenantOptions)
    # Read-only view of another collector's last snapshot for the same tenant.
    peer_samples: Callable[[str], tuple[MetricSample, ...]] = _no_peers

    def granted(self, requirement: Requirement) -> bool:
        """True if the token holds one of the roles, or the roles are unknown."""
        return self.roles is None or satisfied(self.roles, requirement)


class BaseCollector(abc.ABC):
    name: ClassVar[str]
    default_interval: ClassVar[float]
    # Every group must be satisfied (by any one role in it) for the collector to run.
    required_permissions: ClassVar[tuple[Requirement, ...]]
    # Groups that, when granted, add data: (requirement, what it adds).
    optional_permissions: ClassVar[tuple[tuple[Requirement, str], ...]] = ()
    metric_help: ClassVar[dict[str, str]]

    def requirements(
        self, options: TenantOptions, roles: frozenset[str] | None
    ) -> tuple[Requirement, ...]:
        """Requirements for this tenant (a collector may depend on its configured source)."""
        return self.required_permissions

    def missing_permissions(
        self, options: TenantOptions, roles: frozenset[str] | None
    ) -> tuple[Requirement, ...]:
        """Unsatisfied requirement groups; empty when the roles are unknown."""
        if roles is None:
            return ()
        return tuple(g for g in self.requirements(options, roles) if not satisfied(roles, g))

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
