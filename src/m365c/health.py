"""Collector and tenant health state, plus the process-wide metric registry.

Health state is kept per ``(tenant_id, collector)`` in plain Python and rendered by the
exporter, so removing a tenant removes its health series too. The Graph request
counters are ordinary prometheus_client Counters on the same registry.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from prometheus_client import CollectorRegistry, Counter

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


@dataclass
class RunHealth:
    success: bool
    duration_s: float
    samples: int
    last_success_ts: float | None
    auth_failed: bool = False
    error: str | None = None


@dataclass
class HealthState:
    runs: dict[tuple[str, str], RunHealth] = field(default_factory=dict)
    attempted_any: bool = False

    def record_success(self, tenant_id: str, collector: str, duration: float, samples: int) -> None:
        self.attempted_any = True
        self.runs[(tenant_id, collector)] = RunHealth(
            success=True, duration_s=duration, samples=samples, last_success_ts=time.time()
        )

    def record_failure(
        self, tenant_id: str, collector: str, duration: float, error: str, *, auth_failed: bool
    ) -> None:
        self.attempted_any = True
        prev = self.runs.get((tenant_id, collector))
        self.runs[(tenant_id, collector)] = RunHealth(
            success=False,
            duration_s=duration,
            samples=prev.samples if prev else 0,
            last_success_ts=prev.last_success_ts if prev else None,
            auth_failed=auth_failed,
            error=error,
        )

    def auth_ok(self, tenant_id: str) -> bool | None:
        """False if any collector's last run hit an auth error; None before any run."""
        runs = [h for (t, _), h in self.runs.items() if t == tenant_id]
        if not runs:
            return None
        return not any(h.auth_failed for h in runs)

    def drop_tenant(self, tenant_id: str) -> None:
        for key in [k for k in self.runs if k[0] == tenant_id]:
            del self.runs[key]

    def drop(self, tenant_id: str, collector: str) -> None:
        self.runs.pop((tenant_id, collector), None)


def forget_graph_counters(tenant: str, customer: str) -> None:
    """Remove Graph counter series for a tenant that was removed from config."""
    for counter in (GRAPH_REQUESTS, GRAPH_THROTTLED):
        for labels in list(counter._metrics):  # prometheus_client exposes no public lister
            if labels[0] == tenant and labels[1] == customer:
                counter.remove(*labels)
