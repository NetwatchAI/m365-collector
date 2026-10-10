"""Acceptance: a tenant with ``auth.method: client_secret`` whose token holds *only* the four
template-parity roles collects license, users, mailbox, mailtraffic and onedrive (report
source), and skips servicehealth, securescore and appsecrets with the right metrics.

Everything is offline: the real TenantTokenProvider and azure-identity ClientSecretCredential
talk to a fake token endpoint that issues a JWT-shaped token with those roles; Graph is
served from fixture files."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from m365c.auth import TenantTokenProvider
from m365c.cache import SnapshotCache
from m365c.collectors import PARITY_COLLECTORS, all_collectors
from m365c.config import load_all
from m365c.exporter import register
from m365c.fixtures import fixture_transport
from m365c.graph import GraphClient
from m365c.scheduler import Scheduler
from m365c.state import AppState

from .conftest import GRAPH_FIXTURES, TENANT_ID
from .identity import FAKE_SECRET, PARITY_ROLES, FakeIdentity, token_ok

CLIENT = "33333333-3333-3333-3333-333333333333"
SKIPPED = {
    "servicehealth": "ServiceHealth.Read.All",
    "securescore": "SecurityEvents.Read.All",
    "appsecrets": "Application.Read.All",
}


def write_config(cfg: Path, secret: Path) -> None:
    cfg.mkdir()
    # No certificate anywhere: the default certificate is optional when nobody uses it.
    (cfg / "settings.yaml").write_text(
        "app:\n  client_id: 00000000-0000-0000-0000-000000000000\nhttp:\n  max_retries: 0\n",
        encoding="utf-8",
    )
    (cfg / "tenants.yaml").write_text(
        f"""tenants:
  - id: {TENANT_ID}
    name: contoso.onmicrosoft.com
    customer: contoso
    auth:
      method: client_secret
      client_id: {CLIENT}
      client_secret: file:{secret.as_posix()}
    collectors: all
""",
        encoding="utf-8",
    )


def no_p1(inner: httpx.MockTransport) -> httpx.MockTransport:
    """Graph refuses signInActivity (tenant without Entra ID P1), everything else as usual."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users") and "signInActivity" in request.url.params.get(
            "$select", ""
        ):
            return httpx.Response(
                403,
                json={"error": {"code": "Authentication_RequestFromNonPremiumTenantOrB2CTenant",
                                "message": "P1 required"}},
            )  # fmt: skip
        return inner.handle_request(request)

    return httpx.MockTransport(handler)


def scrape(registry: CollectorRegistry) -> dict[str, list[tuple[dict[str, str], float]]]:
    out: dict[str, list[tuple[dict[str, str], float]]] = {}
    for family in text_string_to_metric_families(generate_latest(registry).decode()):
        for s in family.samples:
            out.setdefault(s.name, []).append((s.labels, s.value))
    return out


@pytest.mark.parametrize("license_role", ["Organization.Read.All", "LicenseAssignment.Read.All"])
@pytest.mark.parametrize("signin", [True, False], ids=["with-signin", "without-signin-p1"])
async def test_client_secret_tenant_with_parity_roles_only(
    tmp_path: Path, signin: bool, license_role: str
) -> None:
    roles = [r for r in PARITY_ROLES if r != "Organization.Read.All"] + [license_role]
    secret = tmp_path / "contoso_client_secret"
    secret.write_text(FAKE_SECRET + "\n", encoding="utf-8")
    cfg_dir = tmp_path / "config"
    write_config(cfg_dir, secret)
    collectors = all_collectors()
    cfg = load_all(cfg_dir, list(collectors))

    identity = FakeIdentity(token_ok(roles))
    tokens = TenantTokenProvider(credential_kwargs={"transport": identity, "retry_total": 0})
    transport = fixture_transport(GRAPH_FIXTURES)
    graph = GraphClient(
        tokens, cfg.settings.http, transport=transport if signin else no_p1(transport)
    )
    state = AppState(collectors=collectors, cache=SnapshotCache(), thresholds=cfg.thresholds)
    registry = CollectorRegistry()
    register(state, registry)
    scheduler = Scheduler(state, graph, cfg.settings)
    await scheduler.apply_tenants(cfg.tenants)
    tenant = cfg.tenants[0]
    try:
        # mailbox before users, so the users run can fall back to mailbox activity.
        order = ["license", "mailbox", "users", "mailtraffic", "onedrive", *SKIPPED]
        results = {name: await scheduler.run_once(tenant, name) for name in order}
    finally:
        await scheduler.stop()
        await graph.aclose()
        await tokens.aclose()

    assert identity.secrets_seen and set(identity.secrets_seen) == {FAKE_SECRET}
    assert {r["client_id"] for r in identity.requests} == {CLIENT}
    for name in PARITY_COLLECTORS:
        r = results[name]
        assert r.ok and not r.skipped and r.samples, (name, r.error)
    for name, permission in SKIPPED.items():
        assert results[name].ok and results[name].skipped == ((permission,),)

    m = scrape(registry)
    base = {"customer": "contoso", "tenant": "contoso.onmicrosoft.com"}
    enabled = {x[0]["collector"]: x[1] for x in m["m365_collector_enabled"]}
    assert enabled == {**dict.fromkeys(PARITY_COLLECTORS, 1.0), **dict.fromkeys(SKIPPED, 0.0)}
    assert sorted(
        (x[0]["collector"], x[0]["permission"], x[0]["reason"]) for x in m["m365_collector_skipped"]
    ) == sorted((c, p, "missing_permission") for c, p in SKIPPED.items())
    success = {x[0]["collector"]: x[1] for x in m["m365_collector_success"]}
    assert success == dict.fromkeys(PARITY_COLLECTORS, 1.0)  # skipped ones absent: no alerts
    assert m["m365_tenant_auth_ok"] == [(base, 1.0)]
    granted = {x[0]["permission"] for x in m["m365_tenant_permission"] if x[1] == 1.0}
    assert granted == set(roles)

    def has(name: str, **labels: str) -> bool:
        return any(all(x[0].get(k) == v for k, v in labels.items()) for x in m.get(name, []))

    alice = "alice@contoso.onmicrosoft.com"
    assert has("m365_license_total", sku="SPB")
    assert has("m365_user_license", upn=alice, sku="SPB")
    assert has("m365_mailbox_info", upn=alice, display_name="Example, Alice")
    assert has("m365_mail_sent", upn=alice, period="D7")
    assert has("m365_onedrive_total_bytes", upn=alice)
    assert has("m365_user_last_activity_timestamp", upn=alice)
    assert has("m365_tenant_feature", feature="signin_activity")
    feature = next(v for lbl, v in m["m365_tenant_feature"] if lbl["feature"] == "signin_activity")
    assert feature == (1.0 if signin else 0.0)
    assert has("m365_user_last_signin_timestamp", upn=alice) is signin
    # The concealment check used the heuristic: ReportSettings.Read.All is not granted.
    sources = {x[0]["source"] for x in m["m365_reports_concealed"]}
    assert sources == {"heuristic"}
    assert FAKE_SECRET not in generate_latest(registry).decode()
