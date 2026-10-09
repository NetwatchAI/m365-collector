from __future__ import annotations

from aiohttp.test_utils import TestClient, TestServer
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from m365c.cache import Snapshot
from m365c.collectors.base import MetricSample
from m365c.config import TenantConfig, ThresholdOverrides, Thresholds
from m365c.exporter import register
from m365c.health import FailureKind
from m365c.main import build_app
from m365c.state import AppState


def scrape(state: AppState) -> dict[str, list[tuple[dict[str, str], float]]]:
    registry = CollectorRegistry()
    register(state, registry)
    text = generate_latest(registry).decode()
    out: dict[str, list[tuple[dict[str, str], float]]] = {}
    for family in text_string_to_metric_families(text):
        for s in family.samples:
            out.setdefault(s.name, []).append((s.labels, s.value))
    return out


def test_exporter_renders_cache_health_thresholds(
    app_state: AppState, tenant: TenantConfig
) -> None:
    app_state.thresholds = Thresholds(
        customers={"contoso": ThresholdOverrides(storage_used_warn=0.85)}
    )
    app_state.cache.replace(
        Snapshot(
            tenant.id,
            "license",
            0.0,
            (
                MetricSample("m365_license_total", 25, {"sku": "SPB"}),
                MetricSample("m365_license_consumed", 24, {"sku": "SPB"}),
            ),
        )
    )
    app_state.health.record_success(tenant.id, "license", 1.5, 2)
    app_state.intervals = {"license": 3600.0}
    app_state.health.record_failure(tenant.id, "users", 0.2, "boom", FailureKind.UNAUTHORIZED)

    m = scrape(app_state)
    base = {"customer": "contoso", "tenant": "contoso.onmicrosoft.com"}
    assert m["m365_license_total"] == [({**base, "sku": "SPB"}, 25.0)]
    assert ({**base, "collector": "license"}, 1.0) in m["m365_collector_success"]
    assert ({**base, "collector": "users"}, 0.0) in m["m365_collector_success"]
    assert ({**base, "collector": "license"}, 1.5) in m["m365_collector_duration_seconds"]
    assert ({**base, "collector": "license"}, 2.0) in m["m365_collector_samples"]
    assert len(m["m365_collector_last_success_timestamp"]) == 1  # users never succeeded
    assert m["m365_collector_interval_seconds"] == [({**base, "collector": "license"}, 3600.0)]
    assert m["m365_tenant_auth_ok"] == [(base, 0.0)]
    thresholds = {labels["check"]: v for labels, v in m["m365_threshold"]}
    assert thresholds["storage_used_warn"] == 0.85
    assert thresholds["license_util_warn"] == 0.95
    assert all(labels["customer"] == "contoso" for labels, _ in m["m365_threshold"])
    assert m["m365_collector_build_info"][0][1] == 1.0


def test_exporter_drops_duplicate_series(app_state: AppState, tenant: TenantConfig) -> None:
    dup = MetricSample("m365_app_credential_expiry_timestamp", 1.0, {"app": "a", "key_id": "k"})
    app_state.cache.replace(Snapshot(tenant.id, "appsecrets", 0.0, (dup, dup)))
    assert len(scrape(app_state)["m365_app_credential_expiry_timestamp"]) == 1


def test_exporter_skips_removed_and_disabled_tenants(
    app_state: AppState, tenant: TenantConfig
) -> None:
    app_state.cache.replace(
        Snapshot("gone", "license", 0.0, (MetricSample("m365_license_total", 1),))
    )
    app_state.tenants[tenant.id] = tenant.model_copy(update={"enabled": False})
    app_state.cache.replace(
        Snapshot(tenant.id, "license", 0.0, (MetricSample("m365_license_total", 1),))
    )
    m = scrape(app_state)
    assert "m365_license_total" not in m
    assert "m365_threshold" not in m


async def test_http_endpoints(app_state: AppState) -> None:
    app_state.health.attempted_any = False
    async with TestClient(TestServer(build_app(app_state))) as client:
        assert (await client.get("/healthz")).status == 200
        assert (await client.get("/readyz")).status == 503
        app_state.health.attempted_any = True
        assert (await client.get("/readyz")).status == 200
        resp = await client.get("/metrics")
        assert resp.status == 200
        assert "text/plain" in resp.headers["Content-Type"]
        assert "m365_graph_requests" in await resp.text()


def test_permission_and_restored_series(app_state: AppState, tenant: TenantConfig) -> None:
    app_state.health.record_failure(tenant.id, "securescore", 0.1, "403", FailureKind.FORBIDDEN)
    app_state.health.seed_restored(tenant.id, "license", 1000.0, 5)
    m = scrape(app_state)
    base = {"customer": "contoso", "tenant": "contoso.onmicrosoft.com"}
    assert m["m365_collector_permission_ok"] == [({**base, "collector": "securescore"}, 0.0)]
    assert m["m365_tenant_auth_ok"] == [(base, 1.0)]
    # restored: last success known, but no success/duration/samples claimed
    assert ({**base, "collector": "license"}, 1000.0) in m["m365_collector_last_success_timestamp"]
    assert all(labels["collector"] != "license" for labels, _ in m["m365_collector_success"])


def test_duplicate_series_are_counted(app_state: AppState, tenant: TenantConfig) -> None:
    a = MetricSample("m365_mail_sent_7d", 1.0, {"upn": "a@x"})
    b = MetricSample("m365_mail_sent_7d", 5000.0, {"upn": "a@x"})
    app_state.cache.replace(Snapshot(tenant.id, "mailtraffic", 0.0, (a, b)))
    m = scrape(app_state)
    base = {"customer": "contoso", "tenant": "contoso.onmicrosoft.com"}
    assert m["m365_exporter_duplicate_series"] == [({**base, "collector": "mailtraffic"}, 1.0)]


def test_certificate_expiry_metric(app_state: AppState) -> None:
    from m365c.auth import CertificateInfo

    app_state.certificate = CertificateInfo("CN=x", "AB" * 20, 0.0, 1_900_000_000.0)
    m = scrape(app_state)
    assert m["m365_cert_expiry_timestamp"] == [({"thumbprint": "AB" * 20}, 1_900_000_000.0)]


async def test_http_metrics_renders_registered_exporter(
    app_state: AppState, tenant: TenantConfig
) -> None:
    registry = CollectorRegistry()
    register(app_state, registry)
    app_state.cache.replace(
        Snapshot(tenant.id, "license", 0.0, (MetricSample("m365_license_total", 7, {"sku": "X"}),))
    )
    async with TestClient(TestServer(build_app(app_state, registry))) as client:
        body = await (await client.get("/metrics")).text()
    assert (
        'm365_license_total{customer="contoso",sku="X",tenant="contoso.onmicrosoft.com"} 7.0'
        in body
    )


async def test_readyz_with_no_tenants(app_state: AppState) -> None:
    app_state.tenants = {}
    app_state.health.attempted_any = False
    async with TestClient(TestServer(build_app(app_state, CollectorRegistry()))) as client:
        assert (await client.get("/readyz")).status == 200
