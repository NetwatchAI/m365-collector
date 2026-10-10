"""Mutable runtime state shared by the scheduler, exporter and config watcher."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from m365c.auth import CertificateInfo
from m365c.cache import SnapshotCache
from m365c.collectors.base import BaseCollector
from m365c.config import ResolvedAuth, TenantConfig, Thresholds
from m365c.health import HealthState


@dataclass
class AppState:
    collectors: dict[str, BaseCollector]
    cache: SnapshotCache
    thresholds: Thresholds = field(default_factory=Thresholds)
    health: HealthState = field(default_factory=HealthState)
    tenants: dict[str, TenantConfig] = field(default_factory=dict)
    auth: dict[str, ResolvedAuth] = field(default_factory=dict)  # effective auth per tenant
    intervals: dict[str, float] = field(default_factory=dict)
    config_loaded: bool = False
    certificate: CertificateInfo | None = None
    # tenant_id -> certificate loaded for that tenant (set from the token provider).
    tenant_certificates: Callable[[], dict[str, CertificateInfo]] = dict

    def active_tenants(self) -> list[TenantConfig]:
        return [t for t in self.tenants.values() if t.enabled]
