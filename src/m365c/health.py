"""Collector and tenant health state, plus the process-wide metric registry.

Health state is kept per ``(tenant_id, collector)`` in plain Python and rendered by the
exporter, so removing a tenant removes its health series too. The Graph request
counters are ordinary prometheus_client Counters on the same registry.

Auth is split in two signals:

- tenant auth (``m365_tenant_auth_ok``): the most recent token request / Graph call of
  *any* collector for the tenant. A token failure or Graph 401 sets it to 0; any
  successful call sets it back to 1, so it recovers within one servicehealth cycle.
- collector permission (``m365_collector_permission_ok``): 0 while the collector's last
  run got a Graph 403 (a missing application permission or licence-gated feature).

Collectors whose required permissions are not in the tenant's token are *skipped*, not run
(``m365_collector_enabled 0``, ``m365_collector_skipped{permission}``).
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field

from prometheus_client import CollectorRegistry, Counter, disable_created_metrics

disable_created_metrics()  # type: ignore[no-untyped-call]  # no *_created series

REGISTRY = CollectorRegistry()

GRAPH_REQUESTS = Counter(
    "m365_graph_requests",
    "Microsoft Graph HTTP requests, by response status",
    ["tenant", "customer", "status"],
    registry=REGISTRY,
)
GRAPH_THROTTLED = Counter(
    "m365_graph_throttled",
    "Microsoft Graph responses that asked us to back off (429, 503)",
    ["tenant", "customer"],
    registry=REGISTRY,
)

_request_labels: set[tuple[str, str, str]] = set()
_throttled_labels: set[tuple[str, str]] = set()


def count_request(tenant: str, customer: str, status: str) -> None:
    _request_labels.add((tenant, customer, status))
    GRAPH_REQUESTS.labels(tenant, customer, status).inc()


def count_throttled(tenant: str, customer: str) -> None:
    _throttled_labels.add((tenant, customer))
    GRAPH_THROTTLED.labels(tenant, customer).inc()


def forget_graph_counters(tenant: str, customer: str) -> None:
    """Remove Graph counter series for a tenant that was removed or disabled."""
    for labels in [x for x in _request_labels if x[:2] == (tenant, customer)]:
        GRAPH_REQUESTS.remove(*labels)
        _request_labels.discard(labels)
    if (tenant, customer) in _throttled_labels:
        GRAPH_THROTTLED.remove(tenant, customer)
        _throttled_labels.discard((tenant, customer))


class FailureKind(enum.Enum):
    TOKEN = "token"  # definitive token failure (AADSTS: consent, cert, tenant)
    UNAUTHORIZED = "unauthorized"  # Graph 401
    FORBIDDEN = "forbidden"  # Graph 403: permission missing / licence-gated
    TRANSIENT_TOKEN = "transient_token"  # identity service unavailable
    OTHER = "other"


@dataclass
class RunHealth:
    success: bool
    duration_s: float
    samples: int
    last_success_ts: float | None
    failure: FailureKind | None = None
    error: str | None = None
    restored: bool = False  # seeded from a persisted snapshot; no run happened yet


@dataclass
class HealthState:
    runs: dict[tuple[str, str], RunHealth] = field(default_factory=dict)
    tenant_auth: dict[str, bool] = field(default_factory=dict)
    # Why the tenant's auth is failing (AADSTS mapping, "secret_unavailable", "unauthorized").
    tenant_auth_reason: dict[str, str] = field(default_factory=dict)
    # Graph application roles in the tenant's current token, when it is a JWT.
    tenant_roles: dict[str, frozenset[str]] = field(default_factory=dict)
    # (tenant_id, collector) -> missing any-of permission groups, while skipped.
    skipped: dict[tuple[str, str], tuple[tuple[str, ...], ...]] = field(default_factory=dict)
    attempted_any: bool = False

    def _auth_ok(self, tenant_id: str) -> None:
        self.tenant_auth[tenant_id] = True
        self.tenant_auth_reason.pop(tenant_id, None)

    def record_success(self, tenant_id: str, collector: str, duration: float, samples: int) -> None:
        self.attempted_any = True
        self._auth_ok(tenant_id)
        self.skipped.pop((tenant_id, collector), None)
        self.runs[(tenant_id, collector)] = RunHealth(
            success=True, duration_s=duration, samples=samples, last_success_ts=time.time()
        )

    def record_skipped(
        self, tenant_id: str, collector: str, missing: tuple[tuple[str, ...], ...]
    ) -> None:
        """The collector was not run: the tenant's token lacks a required permission. Its
        run health is dropped, so neither CollectorFailing nor CollectorStale fires."""
        self.attempted_any = True
        self._auth_ok(tenant_id)  # we got a token
        self.skipped[(tenant_id, collector)] = missing
        self.runs.pop((tenant_id, collector), None)

    def record_failure(
        self,
        tenant_id: str,
        collector: str,
        duration: float,
        error: str,
        kind: FailureKind,
        reason: str | None = None,
    ) -> None:
        self.attempted_any = True
        if kind in (FailureKind.TOKEN, FailureKind.UNAUTHORIZED):
            self.tenant_auth[tenant_id] = False
            self.tenant_auth_reason[tenant_id] = reason or kind.value
        elif kind is FailureKind.FORBIDDEN:
            self._auth_ok(tenant_id)  # we got a token and Graph answered
        self.skipped.pop((tenant_id, collector), None)
        prev = self.runs.get((tenant_id, collector))
        self.runs[(tenant_id, collector)] = RunHealth(
            success=False,
            duration_s=duration,
            samples=prev.samples if prev else 0,
            last_success_ts=prev.last_success_ts if prev else None,
            failure=kind,
            error=error,
        )

    def seed_restored(
        self, tenant_id: str, collector: str, collected_at: float, samples: int
    ) -> None:
        """Remember when a persisted snapshot was collected, so staleness is visible after
        a restart even if the collector keeps failing."""
        self.runs.setdefault(
            (tenant_id, collector),
            RunHealth(
                success=True,
                duration_s=0.0,
                samples=samples,
                last_success_ts=collected_at,
                restored=True,
            ),
        )

    def auth_ok(self, tenant_id: str) -> bool | None:
        """Latest auth outcome for the tenant; None before any run."""
        return self.tenant_auth.get(tenant_id)

    def drop_tenant(self, tenant_id: str) -> None:
        for key in [k for k in self.runs if k[0] == tenant_id]:
            del self.runs[key]
        for key in [k for k in self.skipped if k[0] == tenant_id]:
            del self.skipped[key]
        self.tenant_auth.pop(tenant_id, None)
        self.tenant_auth_reason.pop(tenant_id, None)
        self.tenant_roles.pop(tenant_id, None)

    def drop(self, tenant_id: str, collector: str) -> None:
        self.runs.pop((tenant_id, collector), None)
        self.skipped.pop((tenant_id, collector), None)
