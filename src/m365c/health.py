"""Collector and tenant health state, plus the process-wide metric registry.

Health state is kept per ``(tenant_id, collector)`` in plain Python and rendered by the
exporter, so removing a tenant removes its health series too. The Graph request
counters are ordinary prometheus_client Counters on the same registry.

Auth is split in two signals:

- tenant auth (``m365_tenant_auth_ok``): 0 while the tenant has a definitive token failure
  (tenant-wide: cleared by the next run of *any* collector that gets a token, so it recovers
  quickly) or while *any* collector's last run got a Graph 401 (per collector: cleared only
  by that collector's next run, so one collector's 401 cannot be hidden by another
  collector's success).
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
    # Tenants with at least one recorded outcome (auth_ok is None before that).
    tenant_auth: dict[str, bool] = field(default_factory=dict)
    # tenant_id -> reason of the current definitive token failure (AADSTS mapping,
    # "secret_unavailable", "no_roles", "identity_unreachable", ...).
    token_failures: dict[str, str] = field(default_factory=dict)
    # (tenant_id, collector) -> reason, while that collector's last run got a Graph 401.
    unauthorized: dict[tuple[str, str], str] = field(default_factory=dict)
    # Graph application roles in the tenant's current token, when it is a JWT.
    tenant_roles: dict[str, frozenset[str]] = field(default_factory=dict)
    # (tenant_id, collector) -> missing any-of permission groups, while skipped.
    skipped: dict[tuple[str, str], tuple[tuple[str, ...], ...]] = field(default_factory=dict)
    attempted_any: bool = False

    def _got_token(self, tenant_id: str, collector: str) -> None:
        """The run obtained a token (and, unless skipped, Graph did not answer 401)."""
        self.tenant_auth[tenant_id] = True
        self.token_failures.pop(tenant_id, None)
        self.unauthorized.pop((tenant_id, collector), None)

    def record_success(self, tenant_id: str, collector: str, duration: float, samples: int) -> None:
        self.attempted_any = True
        self._got_token(tenant_id, collector)
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
        self._got_token(tenant_id, collector)
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
        key = (tenant_id, collector)
        if kind is FailureKind.TOKEN:
            self.tenant_auth[tenant_id] = False
            self.token_failures[tenant_id] = reason or kind.value
        elif kind is FailureKind.UNAUTHORIZED:
            self.tenant_auth[tenant_id] = False
            self.unauthorized[key] = reason or kind.value
        elif kind is FailureKind.FORBIDDEN:
            self._got_token(tenant_id, collector)  # we got a token and Graph answered
        if key in self.skipped and kind in (FailureKind.TOKEN, FailureKind.TRANSIENT_TOKEN):
            # No token, so no fresh verdict on permissions: a collector that was skipped
            # stays skipped instead of showing a failed run it was never meant to make.
            return
        self.skipped.pop(key, None)
        prev = self.runs.get(key)
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
        """False while a token failure or any collector's 401 is unresolved; None before any
        run."""
        if tenant_id not in self.tenant_auth:
            return None
        if tenant_id in self.token_failures:
            return False
        return not any(k[0] == tenant_id for k in self.unauthorized)

    def auth_reason(self, tenant_id: str) -> str | None:
        if tenant_id in self.token_failures:
            return self.token_failures[tenant_id]
        return next((r for k, r in sorted(self.unauthorized.items()) if k[0] == tenant_id), None)

    def drop_tenant(self, tenant_id: str) -> None:
        for key in [k for k in self.runs if k[0] == tenant_id]:
            del self.runs[key]
        for key in [k for k in self.skipped if k[0] == tenant_id]:
            del self.skipped[key]
        for key in [k for k in self.unauthorized if k[0] == tenant_id]:
            del self.unauthorized[key]
        self.tenant_auth.pop(tenant_id, None)
        self.token_failures.pop(tenant_id, None)
        self.tenant_roles.pop(tenant_id, None)

    def drop(self, tenant_id: str, collector: str) -> None:
        self.runs.pop((tenant_id, collector), None)
        self.skipped.pop((tenant_id, collector), None)
        self.unauthorized.pop((tenant_id, collector), None)
