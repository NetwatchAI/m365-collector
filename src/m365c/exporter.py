"""prometheus_client custom collector that renders the snapshot cache and health state.

``/metrics`` only reads in-memory state here; it never calls Graph.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from prometheus_client import CollectorRegistry
from prometheus_client.metrics_core import Metric
from prometheus_client.registry import Collector

from m365c import __version__
from m365c.state import AppState

BUILTIN_HELP: dict[str, str] = {
    "m365_collector_success": "1 if the collector's last run succeeded, else 0",
    "m365_collector_duration_seconds": "Duration of the collector's last run",
    "m365_collector_last_success_timestamp": "Unix time of the collector's last successful run",
    "m365_collector_samples": "Samples produced by the collector's last successful run",
    "m365_collector_interval_seconds": "Configured run interval of the collector",
    "m365_tenant_auth_ok": "0 if the tenant's last runs hit a token or Graph 401/403 error",
    "m365_threshold": "Alert threshold resolved for the tenant's customer",
    "m365_collector_build_info": "m365c build information",
}


class _Families:
    def __init__(self, help_texts: dict[str, str]) -> None:
        self._help = help_texts
        self._families: dict[str, Metric] = {}
        self._seen: set[tuple[str, frozenset[tuple[str, str]]]] = set()

    def add(self, name: str, labels: dict[str, str], value: float) -> None:
        # A duplicate series would make Prometheus reject the whole scrape: keep the first.
        key = (name, frozenset(labels.items()))
        if key in self._seen:
            return
        self._seen.add(key)
        family = self._families.get(name)
        if family is None:
            family = self._families[name] = Metric(name, self._help.get(name, name), "gauge")
        family.add_sample(name, labels, value)

    def __iter__(self) -> Iterator[Metric]:
        return iter(self._families.values())


class M365Exporter(Collector):
    def __init__(self, state: AppState) -> None:
        self._state = state

    def _help_texts(self) -> dict[str, str]:
        texts = dict(BUILTIN_HELP)
        for collector in self._state.collectors.values():
            texts.update(collector.metric_help)
        return texts

    def collect(self) -> Iterable[Metric]:
        state = self._state
        out = _Families(self._help_texts())

        for snap in state.cache.snapshots():
            tenant = state.tenants.get(snap.tenant_id)
            if tenant is None or not tenant.enabled:
                continue
            base = {"customer": tenant.customer, "tenant": tenant.name}
            for sample in snap.samples:
                out.add(sample.name, {**base, **sample.labels}, sample.value)

        for (tenant_id, collector), run in list(state.health.runs.items()):
            tenant = state.tenants.get(tenant_id)
            if tenant is None or not tenant.enabled:
                continue
            labels = {"customer": tenant.customer, "tenant": tenant.name, "collector": collector}
            out.add("m365_collector_success", labels, 1.0 if run.success else 0.0)
            out.add("m365_collector_duration_seconds", labels, run.duration_s)
            out.add("m365_collector_samples", labels, float(run.samples))
            if collector in state.intervals:
                out.add("m365_collector_interval_seconds", labels, state.intervals[collector])
            if run.last_success_ts is not None:
                out.add("m365_collector_last_success_timestamp", labels, run.last_success_ts)

        for tenant in state.active_tenants():
            base = {"customer": tenant.customer, "tenant": tenant.name}
            auth_ok = state.health.auth_ok(tenant.id)
            if auth_ok is not None:
                out.add("m365_tenant_auth_ok", base, 1.0 if auth_ok else 0.0)
            for check, value in state.thresholds.resolve(tenant.customer).items():
                out.add("m365_threshold", {**base, "check": check}, float(value))

        out.add("m365_collector_build_info", {"version": __version__}, 1.0)
        return list(out)


def register(state: AppState, registry: CollectorRegistry) -> M365Exporter:
    exporter = M365Exporter(state)
    registry.register(exporter)
    return exporter
