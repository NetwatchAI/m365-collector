"""Permission-aware collection: requirement groups, gating from the token's roles, skip and
enable metrics, and collectors adapting to optional permissions."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from m365c.auth import StaticTokenProvider
from m365c.cache import SnapshotCache
from m365c.collectors import PARITY_COLLECTORS, PARITY_PERMISSIONS, all_collectors
from m365c.collectors.base import MetricSample, TenantOptions
from m365c.collectors.onedrive import effective_source
from m365c.config import HttpConfig, Settings, TenantConfig
from m365c.fixtures import fake_jwt, fixture_transport
from m365c.graph import GraphClient
from m365c.scheduler import Scheduler
from m365c.state import AppState

from .conftest import GRAPH_FIXTURES, SleepRecorder, make_ctx
from .identity import PARITY_ROLES
from .test_collectors import find, value
from .test_exporter import scrape

V1 = "https://graph.microsoft.com/v1.0"
CERT_SETTINGS = Settings(
    app={"client_id": "00000000-0000-0000-0000-000000000000"},
    secrets={"cert": "file:/run/secrets/m365c_cert"},
)


def roles(*names: str) -> frozenset[str]:
    return frozenset(names)


def test_requirement_groups() -> None:
    c = all_collectors()
    default = TenantOptions()
    assert c["license"].missing_permissions(default, roles("Organization.Read.All")) == ()
    assert c["license"].missing_permissions(default, roles("LicenseAssignment.Read.All")) == ()
    assert c["license"].missing_permissions(default, roles("User.Read.All")) == (
        ("Organization.Read.All", "LicenseAssignment.Read.All"),
    )
    assert c["users"].missing_permissions(default, roles("User.Read.All")) == ()  # sign-in optional
    assert c["servicehealth"].missing_permissions(default, None) == ()  # unknown roles: no gating


def test_parity_set_runs_exactly_the_parity_collectors() -> None:
    granted = frozenset(PARITY_ROLES)
    assert all(any(p in granted for p in group) for group in PARITY_PERMISSIONS)
    running = {
        name
        for name, c in all_collectors().items()
        if not c.missing_permissions(TenantOptions(), granted)
    }
    assert running == set(PARITY_COLLECTORS)
    # Organization.Read.All may be replaced by LicenseAssignment.Read.All.
    alt = (granted - {"Organization.Read.All"}) | {"LicenseAssignment.Read.All"}
    assert not all_collectors()["license"].missing_permissions(TenantOptions(), alt)


@pytest.mark.parametrize(
    ("source", "granted", "effective", "missing"),
    [
        ("report", {"Reports.Read.All"}, "report", ()),
        ("report", {"User.Read.All", "Files.Read.All"}, "report", (("Reports.Read.All",),)),
        ("quota", {"Reports.Read.All", "User.Read.All"}, "quota", (("Files.Read.All",),)),
        ("quota", {"User.Read.All", "Files.Read.All"}, "quota", ()),
        ("auto", {"Reports.Read.All", "User.Read.All", "Files.Read.All"}, "quota", ()),
        ("auto", {"Reports.Read.All", "User.Read.All"}, "report", ()),
        ("auto", set(), "report", (("Reports.Read.All",),)),
    ],
)
def test_onedrive_source_gating(
    source: str, granted: set[str], effective: str, missing: tuple[tuple[str, ...], ...]
) -> None:
    options = TenantOptions(onedrive_source=source)
    assert effective_source(source, frozenset(granted)) == effective
    assert all_collectors()["onedrive"].missing_permissions(options, frozenset(granted)) == missing


class SwitchableTokens(StaticTokenProvider):
    """A token provider whose roles can be changed, like a new token after new consent."""

    def __init__(self, granted: list[str]) -> None:
        super().__init__(fake_jwt(granted))

    def grant(self, granted: list[str]) -> None:
        self.__init__(granted)  # type: ignore[misc]


async def make_scheduler(
    tokens: StaticTokenProvider, tenants: list[TenantConfig]
) -> tuple[Scheduler, AppState, GraphClient]:
    graph = GraphClient(
        tokens, HttpConfig(max_retries=0), transport=fixture_transport(GRAPH_FIXTURES),
        sleep=SleepRecorder(),
    )  # fmt: skip
    state = AppState(collectors=all_collectors(), cache=SnapshotCache())
    scheduler = Scheduler(state, graph, CERT_SETTINGS, sleep=SleepRecorder())
    await scheduler.apply_tenants(tenants)
    return scheduler, state, graph


async def test_skip_metrics_warning_once_and_recovery(tenant: TenantConfig) -> None:
    tokens = SwitchableTokens(PARITY_ROLES)
    scheduler, state, graph = await make_scheduler(tokens, [tenant])
    try:
        with capture_logs() as logs:
            first = await scheduler.run_once(tenant, "servicehealth")
            again = await scheduler.run_once(tenant, "servicehealth")
        assert first.ok and first.skipped == (("ServiceHealth.Read.All",),)
        assert again.skipped and not again.samples
        skipped_logs = [x for x in logs if x["event"] == "collector_skipped"]
        assert len(skipped_logs) == 1  # one warning per tenant and collector
        assert skipped_logs[0]["missing"] == ["ServiceHealth.Read.All"]

        m = scrape(state)
        base = {"customer": "contoso", "tenant": "contoso.onmicrosoft.com"}
        labels = {**base, "collector": "servicehealth"}
        assert (labels, 0.0) in m["m365_collector_enabled"]
        assert m["m365_collector_skipped"] == [
            (
                {**labels, "reason": "missing_permission", "permission": "ServiceHealth.Read.All"},
                1.0,
            )
        ]
        assert not [x for x in m.get("m365_collector_success", []) if x[0] == labels]
        assert not [x for x in m.get("m365_collector_last_success_timestamp", []) if x[0] == labels]
        perms = dict((x[0]["permission"], x[1]) for x in m["m365_tenant_permission"])
        assert perms["Reports.Read.All"] == 1.0 and perms["ServiceHealth.Read.All"] == 0.0
        assert m["m365_tenant_auth_ok"] == [(base, 1.0)]

        # Consent granted later: the next token carries the role, no restart needed.
        tokens.grant([*PARITY_ROLES, "ServiceHealth.Read.All"])
        with capture_logs() as logs:
            ok = await scheduler.run_once(tenant, "servicehealth")
        assert ok.ok and not ok.skipped and ok.samples
        assert any(x["event"] == "collector_enabled" for x in logs)
        m = scrape(state)
        assert (labels, 1.0) in m["m365_collector_enabled"]
        assert "m365_collector_skipped" not in m
    finally:
        await scheduler.stop()
        await graph.aclose()


async def test_skip_drops_previous_snapshot(tenant: TenantConfig) -> None:
    tokens = SwitchableTokens([*PARITY_ROLES, "ServiceHealth.Read.All"])
    scheduler, state, graph = await make_scheduler(tokens, [tenant])
    try:
        assert (await scheduler.run_once(tenant, "servicehealth")).samples
        assert state.cache.get(tenant.id, "servicehealth") is not None
        tokens.grant(PARITY_ROLES)  # consent for the role revoked
        assert (await scheduler.run_once(tenant, "servicehealth")).skipped
        assert state.cache.get(tenant.id, "servicehealth") is None
    finally:
        await scheduler.stop()
        await graph.aclose()


async def test_explicit_collector_list_with_missing_permission_does_not_crash() -> None:
    tenant = TenantConfig(
        id="11111111-1111-1111-1111-111111111111", name="contoso.onmicrosoft.com",
        customer="contoso", collectors=["license", "appsecrets"],
    )  # fmt: skip
    tokens = SwitchableTokens(PARITY_ROLES)
    scheduler, state, graph = await make_scheduler(tokens, [tenant])
    try:
        assert scheduler.task_keys == {(tenant.id, "license"), (tenant.id, "appsecrets")}
        result = await scheduler.run_once(tenant, "appsecrets")
        assert result.ok and result.skipped == (("Application.Read.All",),)
        assert state.health.skipped[(tenant.id, "appsecrets")]
    finally:
        await scheduler.stop()
        await graph.aclose()


async def test_skipped_collector_rechecks_hourly(tenant: TenantConfig) -> None:
    tokens = SwitchableTokens(PARITY_ROLES)
    sleeps = SleepRecorder()
    graph = GraphClient(
        tokens, HttpConfig(max_retries=0), transport=fixture_transport(GRAPH_FIXTURES)
    )
    state = AppState(collectors=all_collectors(), cache=SnapshotCache())
    state.tenants = {tenant.id: tenant}
    calls = 0

    async def sleep(seconds: float) -> None:
        nonlocal calls
        sleeps.calls.append(seconds)
        calls += 1
        if calls > 2:
            raise RuntimeError("stop")

    scheduler = Scheduler(state, graph, CERT_SETTINGS, rng=lambda: 0.0, sleep=sleep)
    with pytest.raises(RuntimeError):
        await scheduler._loop((tenant.id, "appsecrets"))  # default interval 24 h
    assert sleeps.calls[1] == 3600.0
    await graph.aclose()


# ------------------------------------------------------------------ collectors and roles


@respx.mock
async def test_users_without_auditlog_role_never_requests_signin(
    respx_graph: GraphClient, tenant: TenantConfig
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "signInActivity" not in request.url.params.get("$select", "")
        return httpx.Response(200, json={"value": [
            {"id": "1", "userPrincipalName": "Alice@x", "accountEnabled": False,
             "assignedLicenses": [{"skuId": "CBDC14AB-d96c-4c30-b9f4-6ada7cdc1d46"},
                                  {"skuId": "11111111-2222-3333-4444-555555555555"}]},
            {"id": "2", "userPrincipalName": "bob@x", "accountEnabled": True,
             "assignedLicenses": []},
        ]})  # fmt: skip

    respx.get(f"{V1}/users").mock(side_effect=handler)
    skus = respx.get(f"{V1}/subscribedSkus").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [{"skuId": "cbdc14ab-d96c-4c30-b9f4-6ada7cdc1d46", "skuPartNumber": "SPB"}]
            },
        )
    )
    mailbox_peer = (
        MetricSample("m365_mailbox_last_activity_timestamp", 1_790_000_000.0, {"upn": "alice@x"}),
    )
    ctx = make_ctx(
        respx_graph, tenant, roles=roles("User.Read.All", "Organization.Read.All"),
        peers={"mailbox": mailbox_peer},
    )  # fmt: skip
    s = await all_collectors()["users"].collect(ctx)
    assert skus.call_count == 1  # once per run, not per user
    assert value(s, "m365_tenant_feature", feature="signin_activity") == 0
    assert value(s, "m365_user_license", upn="alice@x", sku="SPB") == 1
    # Unknown SKU (not in subscribedSkus): sku falls back to the ID.
    lic = find(s, "m365_user_license", sku_id="11111111-2222-3333-4444-555555555555")
    assert lic[0].labels["sku"] == "11111111-2222-3333-4444-555555555555"
    # Product display name from the bundled map; unknown to it: falls back to sku.
    assert find(s, "m365_user_license", sku="SPB")[0].labels["sku_name"] == (
        "Microsoft 365 Business Premium"
    )
    assert lic[0].labels["sku_name"] == "11111111-2222-3333-4444-555555555555"
    # No sign-in data: last activity comes from the mailbox report.
    assert value(s, "m365_user_last_activity_timestamp", upn="alice@x") == 1_790_000_000.0
    assert not find(s, "m365_user_last_activity_timestamp", upn="bob@x")
    assert not find(s, "m365_user_last_signin_timestamp")


@respx.mock
async def test_users_without_license_role_uses_sku_ids(
    respx_graph: GraphClient, tenant: TenantConfig
) -> None:
    respx.get(f"{V1}/users").mock(return_value=httpx.Response(200, json={"value": [
        {"id": "1", "userPrincipalName": "a@x", "accountEnabled": True,
         "assignedLicenses": [{"skuId": "cbdc14ab-d96c-4c30-b9f4-6ada7cdc1d46"}]},
    ]}))  # fmt: skip
    skus = respx.get(f"{V1}/subscribedSkus").mock(return_value=httpx.Response(500))
    ctx = make_ctx(respx_graph, tenant, roles=roles("User.Read.All"))
    s = await all_collectors()["users"].collect(ctx)
    assert skus.call_count == 0
    sku_id = "cbdc14ab-d96c-4c30-b9f4-6ada7cdc1d46"
    assert value(s, "m365_user_license", upn="a@x", sku=sku_id, sku_id=sku_id) == 1
    # The display name needs no Graph call: the bundled map is keyed by skuId.
    assert value(s, "m365_user_license", upn="a@x", sku_name="Microsoft 365 Business Premium") == 1


@respx.mock
async def test_users_multi_page_with_licences(
    respx_graph: GraphClient, tenant: TenantConfig
) -> None:
    def page(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("$skiptoken") == "p2":
            return httpx.Response(200, json={"value": [
                {"id": "3", "userPrincipalName": "c@x", "accountEnabled": False,
                 "assignedLicenses": [{"skuId": "4b9405b0-7788-4568-add1-99614e613b69"}]},
            ]})  # fmt: skip
        return httpx.Response(200, json={
            "@odata.nextLink": f"{V1}/users?$skiptoken=p2",
            "value": [
                {"id": "1", "userPrincipalName": "a@x", "accountEnabled": True,
                 "assignedLicenses": [{"skuId": "cbdc14ab-d96c-4c30-b9f4-6ada7cdc1d46"}]},
                {"id": "2", "userPrincipalName": "b@x", "accountEnabled": True,
                 "assignedLicenses": []},
            ],
        })  # fmt: skip

    route = respx.get(url__regex=rf"{V1}/users(\?.*)?$").mock(side_effect=page)
    respx.get(f"{V1}/subscribedSkus").mock(
        return_value=httpx.Response(
            200, content=(GRAPH_FIXTURES / "subscribedSkus.json").read_bytes()
        )
    )
    s = await all_collectors()["users"].collect(make_ctx(respx_graph, tenant))
    assert route.call_count == 2
    assert value(s, "m365_user_license", upn="a@x", sku="SPB") == 1
    assert value(s, "m365_user_license", upn="c@x", sku="EXCHANGESTANDARD") == 1  # page 2
    assert value(s, "m365_users_disabled_licensed") == 1


def _mock_mailtraffic(csv: str, settings: Any = None) -> tuple[respx.Route, respx.Route]:
    download = "https://reports.office.com/data/download/abc"
    report_settings = respx.get(f"{V1}/admin/reportSettings").mock(
        return_value=settings or httpx.Response(200, json={"displayConcealedNames": False})
    )
    respx.get(url__regex=r".*/reports/getEmailActivityUserDetail\(period='D30'\)").mock(
        return_value=httpx.Response(302, headers={"Location": download})
    )
    respx.get(download).mock(return_value=httpx.Response(200, content=csv.encode()))
    return report_settings, respx.routes[-1]


CSV = (
    "﻿Report Refresh Date,User Principal Name,Display Name,Is Deleted,Send Count,Receive Count,Read Count\n"
    '2026-10-06,a@x,"Example, Alice",False,0,12,3\n'
    '2026-10-06,b@x,"Quiet, Bob",False,,,\n'
)


@respx.mock
async def test_mailtraffic_period_zero_rows_and_no_report_settings_role(
    respx_graph: GraphClient, tenant: TenantConfig
) -> None:
    settings_route, _ = _mock_mailtraffic(CSV)
    ctx = make_ctx(
        respx_graph, tenant, roles=roles("Reports.Read.All"),
        options=TenantOptions(report_period="D30"),
    )  # fmt: skip
    s = await all_collectors()["mailtraffic"].collect(ctx)
    assert settings_route.call_count == 0  # role not granted: not even tried
    assert s[0] == MetricSample(
        "m365_reports_concealed", 0.0, {"report": "mailtraffic", "source": "heuristic"}
    )
    assert value(s, "m365_mail_sent", upn="a@x", period="D30") == 0  # zero row kept
    assert value(s, "m365_mail_received", upn="a@x", period="D30") == 12
    assert value(s, "m365_mail_sent", upn="b@x", period="D30") == 0  # empty counts kept as 0


@respx.mock
async def test_report_settings_403_falls_back_to_heuristic(
    respx_graph: GraphClient, tenant: TenantConfig
) -> None:
    forbidden = httpx.Response(403, json={"error": {"code": "Forbidden", "message": "no"}})
    hashed = CSV.replace("a@x", "5A3F9C0E1B2D4F6A8B7C9D0E1F2A3B4C").replace(
        "b@x", "0123456789abcdef0123456789abcdef"
    )
    _mock_mailtraffic(hashed, forbidden)
    state: dict[str, object] = {}
    ctx = make_ctx(respx_graph, tenant, state, options=TenantOptions(report_period="D30"))
    with capture_logs() as logs:
        s = await all_collectors()["mailtraffic"].collect(ctx)
    assert s == [
        MetricSample(
            "m365_reports_concealed", 1.0, {"report": "mailtraffic", "source": "heuristic"}
        )
    ]
    assert {x["event"] for x in logs} >= {"report_settings_unavailable", "reports_concealed"}


async def test_onedrive_report_source(fixture_graph: GraphClient, tenant: TenantConfig) -> None:
    ctx = make_ctx(fixture_graph, tenant, roles=frozenset(PARITY_ROLES))
    s = await all_collectors()["onedrive"].collect(ctx)
    alice = "alice@contoso.onmicrosoft.com"
    assert value(s, "m365_reports_concealed", report="onedrive", source="heuristic") == 0
    assert value(s, "m365_onedrive_info", upn=alice, display_name="Example, Alice") == 1
    assert value(s, "m365_onedrive_used_bytes", upn=alice) == 2147483648
    assert value(s, "m365_onedrive_total_bytes", upn=alice) == 1099511627776
    assert value(s, "m365_onedrive_file_count", upn=alice) == 4321
    assert value(s, "m365_onedrive_last_activity_timestamp", upn=alice) == 1791072000.0
    bob = "bob@contoso.onmicrosoft.com"
    assert value(s, "m365_onedrive_file_count", upn=bob) == 0
    assert not find(s, "m365_onedrive_last_activity_timestamp", upn=bob)  # never active
    assert not find(s, "m365_onedrive_used_bytes", upn="ghost@contoso.onmicrosoft.com")  # deleted
    assert not find(s, "m365_onedrive_deleted_bytes")  # quota source only


async def test_mailbox_info_and_deleted_count(
    fixture_graph: GraphClient, tenant: TenantConfig
) -> None:
    s = await all_collectors()["mailbox"].collect(make_ctx(fixture_graph, tenant))
    alice = "alice@contoso.onmicrosoft.com"
    info = find(s, "m365_mailbox_info", upn=alice)
    assert info[0].labels == {"upn": alice, "display_name": "Example, Alice", "has_archive": "true"}
    assert value(s, "m365_mailbox_deleted") == 1
