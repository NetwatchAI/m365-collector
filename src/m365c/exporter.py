"""prometheus_client custom collector that renders the snapshot cache and health state.

``/metrics`` only reads in-memory state here; it never calls Graph.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime

import structlog
from prometheus_client import CollectorRegistry
from prometheus_client.metrics_core import Metric
from prometheus_client.registry import Collector

from m365c import __version__
from m365c.collectors import PARITY_COLLECTORS, permissions
from m365c.health import FailureKind
from m365c.state import AppState

log = structlog.get_logger(__name__)

BUILTIN_HELP: dict[str, str] = {
    "m365_collector_success": "1 if the collector's last run succeeded, else 0",
    "m365_collector_duration_seconds": "Duration of the collector's last run",
    "m365_collector_last_success_timestamp": "Unix time of the collector's last successful run",
    "m365_collector_samples": "Samples produced by the collector's last successful run",
    "m365_collector_interval_seconds": "Configured run interval of the collector",
    "m365_tenant_auth_ok": "0 if the tenant's last runs hit a token or Graph 401/403 error",
    "m365_threshold": "Alert threshold resolved for the tenant's customer",
    "m365_collector_build_info": "m365c build information",
    "m365_collector_permission_ok": "0 if the collector's last run got a Graph 403",
    "m365_exporter_duplicate_series": (
        "Samples dropped because another sample had the same name and labels"
    ),
    "m365_cert_expiry_timestamp": "Unix time when the app registration certificate expires",
    "m365_tenant_auth_error": "1 for the reason the tenant's authentication is failing",
    "m365_collector_enabled": (
        "1 if the collector runs for the tenant, 0 if skipped for a missing permission"
    ),
    "m365_collector_skipped": (
        "1 per missing permission group (alternatives joined by |) of a skipped collector"
    ),
    "m365_collector_expected": (
        "1 if the tenant is expected to run the collector (explicit list, or a template-parity "
        "collector under `collectors: all`)"
    ),
    "m365_tenant_cert_expiry_timestamp": (
        "Unix time when the tenant's own certificate (auth.cert) expires"
    ),
    "m365_tenant_permission": (
        "1 if the tenant's token holds the Graph application role, 0 if not (roles m365c uses)"
    ),
    "m365_client_secret_expiry_timestamp": (
        "Unix time when the tenant's client secret expires (from auth.client_secret_expires)"
    ),
}


class _Families:
    def __init__(self, help_texts: dict[str, str]) -> None:
        self._help = help_texts
        self._families: dict[str, Metric] = {}
        self._seen: set[tuple[str, frozenset[tuple[str, str]]]] = set()

    def add(self, name: str, labels: dict[str, str], value: float) -> bool:
        """Add a sample; returns False if it duplicates an existing series."""
        # A duplicate series would make Prometheus reject the whole scrape: keep the first.
        key = (name, frozenset(labels.items()))
        if key in self._seen:
            return False
        self._seen.add(key)
        family = self._families.get(name)
        if family is None:
            family = self._families[name] = Metric(name, self._help.get(name, name), "gauge")
        family.add_sample(name, labels, value)
        return True

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
            dropped = 0
            for sample in snap.samples:
                if not out.add(sample.name, {**base, **sample.labels}, sample.value):
                    dropped += 1
            if dropped:
                log.warning(
                    "duplicate_series_dropped",
                    tenant=tenant.name,
                    customer=tenant.customer,
                    collector=snap.collector,
                    dropped=dropped,
                )
                out.add(
                    "m365_exporter_duplicate_series",
                    {**base, "collector": snap.collector},
                    float(dropped),
                )

        for (tenant_id, collector), run in list(state.health.runs.items()):
            tenant = state.tenants.get(tenant_id)
            if tenant is None or not tenant.enabled:
                continue
            labels = {"customer": tenant.customer, "tenant": tenant.name, "collector": collector}
            if not run.restored:  # restored snapshots only carry their collection time
                out.add("m365_collector_enabled", labels, 1.0)
                out.add("m365_collector_success", labels, 1.0 if run.success else 0.0)
                out.add("m365_collector_duration_seconds", labels, run.duration_s)
                out.add("m365_collector_samples", labels, float(run.samples))
                permission_ok = run.failure is not FailureKind.FORBIDDEN
                out.add("m365_collector_permission_ok", labels, 1.0 if permission_ok else 0.0)
            if collector in state.intervals:
                out.add("m365_collector_interval_seconds", labels, state.intervals[collector])
            if run.last_success_ts is not None:
                out.add("m365_collector_last_success_timestamp", labels, run.last_success_ts)

        for (tenant_id, collector), missing in list(state.health.skipped.items()):
            tenant = state.tenants.get(tenant_id)
            if tenant is None or not tenant.enabled:
                continue
            labels = {"customer": tenant.customer, "tenant": tenant.name, "collector": collector}
            out.add("m365_collector_enabled", labels, 0.0)
            if collector in state.intervals:
                out.add("m365_collector_interval_seconds", labels, state.intervals[collector])
            for group in missing:
                out.add(
                    "m365_collector_skipped",
                    {**labels, "reason": "missing_permission", "permission": "|".join(group)},
                    1.0,
                )

        known_permissions = permissions()
        tenant_certs = state.tenant_certificates()
        names = list(state.collectors)
        for tenant in state.active_tenants():
            base = {"customer": tenant.customer, "tenant": tenant.name}
            # Which configured collectors the tenant is *expected* to run: an explicit list
            # means all of them; `all` means the template-parity set. A skip of an expected
            # collector is a loss of monitoring (M365CollectorSkipped).
            for collector in tenant.enabled_collectors(names):
                expected = tenant.collectors != "all" or collector in PARITY_COLLECTORS
                out.add(
                    "m365_collector_expected",
                    {**base, "collector": collector},
                    1.0 if expected else 0.0,
                )
            cert = tenant_certs.get(tenant.id)
            if cert is not None and tenant.auth is not None and tenant.auth.cert is not None:
                out.add(
                    "m365_tenant_cert_expiry_timestamp",
                    {**base, "thumbprint": cert.thumbprint},
                    cert.not_after,
                )
            roles = state.health.tenant_roles.get(tenant.id)
            if roles is not None:
                for permission in known_permissions:
                    out.add(
                        "m365_tenant_permission",
                        {**base, "permission": permission},
                        1.0 if permission in roles else 0.0,
                    )
            auth_ok = state.health.auth_ok(tenant.id)
            if auth_ok is not None:
                out.add("m365_tenant_auth_ok", base, 1.0 if auth_ok else 0.0)
            if auth_ok is False:
                reason = state.health.auth_reason(tenant.id) or "unknown"
                out.add("m365_tenant_auth_error", {**base, "reason": reason}, 1.0)
            auth = state.auth.get(tenant.id)
            if auth is not None and auth.client_secret_expires is not None:
                expires = auth.client_secret_expires
                out.add(
                    "m365_client_secret_expiry_timestamp",
                    {**base, "client_id": auth.client_id},
                    datetime(expires.year, expires.month, expires.day, tzinfo=UTC).timestamp(),
                )
            for check, value in state.thresholds.resolve(tenant.customer).items():
                out.add("m365_threshold", {**base, "check": check}, float(value))

        if state.certificate is not None:
            out.add(
                "m365_cert_expiry_timestamp",
                {"thumbprint": state.certificate.thumbprint},
                state.certificate.not_after,
            )
        out.add("m365_collector_build_info", {"version": __version__}, 1.0)
        return list(out)


def register(state: AppState, registry: CollectorRegistry) -> M365Exporter:
    exporter = M365Exporter(state)
    registry.register(exporter)
    return exporter
