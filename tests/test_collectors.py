from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from m365c.collectors import all_collectors, permissions
from m365c.collectors.base import MetricSample
from m365c.config import TenantConfig
from m365c.graph import GraphAuthError, GraphClient

from .conftest import QUOTA, make_ctx

V1 = "https://graph.microsoft.com/v1.0"


def ts(value: str) -> float:
    return datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp()


def find(samples: list[MetricSample], name: str, **labels: str) -> list[MetricSample]:
    return [
        s
        for s in samples
        if s.name == name and all(s.labels.get(k) == v for k, v in labels.items())
    ]


def value(samples: list[MetricSample], name: str, **labels: str) -> float:
    matches = find(samples, name, **labels)
    assert len(matches) == 1, f"{name} {labels}: {matches}"
    return matches[0].value


async def collect(name: str, graph: GraphClient, tenant: TenantConfig) -> list[MetricSample]:
    return await all_collectors()[name].collect(make_ctx(graph, tenant))


def test_every_collector_declares_contract() -> None:
    collectors = all_collectors()
    assert set(collectors) == {
        "license", "users", "mailbox", "onedrive", "mailtraffic",
        "servicehealth", "securescore", "appsecrets",
    }  # fmt: skip
    for c in collectors.values():
        assert c.default_interval > 0
        assert c.required_permissions
        assert c.metric_help
        assert all(m.startswith("m365_") for m in c.metric_help)
    assert "Reports.Read.All" in permissions()
    assert permissions(["license"]) == ["LicenseAssignment.Read.All", "Organization.Read.All"]


async def test_license(fixture_graph: GraphClient, tenant: TenantConfig) -> None:
    s = await collect("license", fixture_graph, tenant)
    assert value(s, "m365_license_total", sku="SPB") == 25
    assert value(s, "m365_license_consumed", sku="SPB") == 24
    assert value(s, "m365_license_available", sku="SPB") == 1
    assert value(s, "m365_license_available", sku="EXCHANGESTANDARD") == 0
    assert value(s, "m365_license_suspended", sku="EXCHANGESTANDARD") == 2
    assert value(s, "m365_license_warning", sku="EXCHANGESTANDARD") == 1
    assert {k for x in s for k in x.labels} == {"sku"}


async def test_users(fixture_graph: GraphClient, tenant: TenantConfig) -> None:
    s = await collect("users", fixture_graph, tenant)
    info = find(s, "m365_user_info")
    assert {i.labels["upn"] for i in info} == {
        "alice@contoso.onmicrosoft.com",
        "bob@contoso.onmicrosoft.com",
        "carol@contoso.onmicrosoft.com",
        "dave@contoso.onmicrosoft.com",
    }  # paged, lower-cased, agent user excluded
    alice = find(s, "m365_user_info", upn="alice@contoso.onmicrosoft.com")[0]
    assert alice.labels == {
        "upn": "alice@contoso.onmicrosoft.com",
        "display_name": "Example, Alice",
        "department": "Finance",
        "enabled": "true",
        "licensed": "true",
    }
    # most recent of interactive / non-interactive
    assert value(s, "m365_user_last_signin_timestamp", upn="alice@contoso.onmicrosoft.com") == ts(
        "2026-10-01T08:00:00"
    )
    assert not find(s, "m365_user_last_signin_timestamp", upn="carol@contoso.onmicrosoft.com")
    assert value(s, "m365_users_total", enabled="true", licensed="true") == 2
    assert value(s, "m365_users_total", enabled="false", licensed="true") == 1
    assert value(s, "m365_users_total", enabled="true", licensed="false") == 1
    assert value(s, "m365_users_total", enabled="false", licensed="false") == 0
    assert value(s, "m365_users_disabled_licensed") == 1
    assert value(s, "m365_tenant_feature", feature="signin_activity") == 1
    # department / display_name only on the info metric
    for sample in s:
        if sample.name != "m365_user_info":
            assert "department" not in sample.labels and "display_name" not in sample.labels


@respx.mock
async def test_users_without_signin_activity_falls_back(
    respx_graph: GraphClient, tenant: TenantConfig
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        select = request.url.params.get("$select", "")
        if "signInActivity" in select:
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": "Authentication_RequestFromNonPremiumTenantOrB2CTenant",
                        "message": "P1 needed",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "1",
                        "userPrincipalName": "a@x",
                        "accountEnabled": True,
                        "assignedLicenses": [],
                    }
                ]
            },
        )

    route = respx.get(f"{V1}/users").mock(side_effect=handler)
    respx.get(f"{V1}/subscribedSkus").mock(return_value=httpx.Response(200, json={"value": []}))
    state: dict[str, object] = {}
    collector = all_collectors()["users"]
    s = await collector.collect(make_ctx(respx_graph, tenant, state))
    assert value(s, "m365_tenant_feature", feature="signin_activity") == 0
    assert value(s, "m365_users_total", enabled="true", licensed="false") == 1
    assert not find(s, "m365_user_last_signin_timestamp")
    assert route.call_count == 2
    assert state["signin_warned"] is True
    # second run warns no more but still works
    s = await collector.collect(make_ctx(respx_graph, tenant, state))
    assert value(s, "m365_tenant_feature", feature="signin_activity") == 0


@respx.mock
async def test_users_agent_filter_unsupported_falls_back(
    respx_graph: GraphClient, tenant: TenantConfig
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "$filter" in request.url.params:
            assert request.headers["ConsistencyLevel"] == "eventual"
            return httpx.Response(
                400, json={"error": {"code": "Request_UnsupportedQuery", "message": "no"}}
            )
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "1",
                        "userPrincipalName": "a@x",
                        "accountEnabled": True,
                        "assignedLicenses": [],
                    },
                    {
                        "@odata.type": "#microsoft.graph.agentUser",
                        "id": "2",
                        "userPrincipalName": "bot@x",
                    },
                ]
            },
        )

    route = respx.get(f"{V1}/users").mock(side_effect=handler)
    respx.get(f"{V1}/subscribedSkus").mock(return_value=httpx.Response(200, json={"value": []}))
    state: dict[str, object] = {}
    s = await all_collectors()["users"].collect(make_ctx(respx_graph, tenant, state))
    assert [x.labels["upn"] for x in find(s, "m365_user_info")] == ["a@x"]
    assert state["server_filter"] is False
    assert route.call_count == 2
    await all_collectors()["users"].collect(make_ctx(respx_graph, tenant, state))
    assert route.call_count == 3  # filter not retried once known unsupported


@respx.mock
async def test_users_real_permission_error_propagates(
    respx_graph: GraphClient, tenant: TenantConfig
) -> None:
    respx.get(f"{V1}/users").mock(
        return_value=httpx.Response(
            403, json={"error": {"code": "Authorization_RequestDenied", "message": "x"}}
        )
    )
    with pytest.raises(GraphAuthError):
        await all_collectors()["users"].collect(make_ctx(respx_graph, tenant))


async def test_mailbox(fixture_graph: GraphClient, tenant: TenantConfig) -> None:
    s = await collect("mailbox", fixture_graph, tenant)
    assert value(s, "m365_reports_concealed", report="mailbox") == 0
    alice = "alice@contoso.onmicrosoft.com"
    assert value(s, "m365_mailbox_used_bytes", upn=alice) == 48318382080
    assert value(s, "m365_mailbox_quota_bytes", upn=alice) == 53687091200
    assert value(s, "m365_mailbox_items", upn=alice) == 12345
    assert value(s, "m365_mailbox_last_activity_timestamp", upn=alice) == ts("2026-10-05T00:00:00")
    assert value(s, "m365_mailbox_has_archive", upn=alice) == 1
    bob = "bob@contoso.onmicrosoft.com"
    assert value(s, "m365_mailbox_has_archive", upn=bob) == 0
    assert not find(s, "m365_mailbox_last_activity_timestamp", upn=bob)  # empty date: no sample
    assert not find(s, "m365_mailbox_used_bytes", upn="ghost@contoso.onmicrosoft.com")  # deleted


async def test_mailtraffic(fixture_graph: GraphClient, tenant: TenantConfig) -> None:
    s = await collect("mailtraffic", fixture_graph, tenant)
    alice = "alice@contoso.onmicrosoft.com"
    assert value(s, "m365_reports_concealed", report="mailtraffic") == 0
    assert value(s, "m365_mail_sent", upn=alice, period="D7") == 950
    assert value(s, "m365_mail_received", upn=alice, period="D7") == 1200
    assert value(s, "m365_mail_read", upn=alice, period="D7") == 1100
    assert value(s, "m365_mail_received", upn="bob@contoso.onmicrosoft.com") == 15
    assert not find(s, "m365_mail_sent", upn="ghost@contoso.onmicrosoft.com")


@respx.mock
@pytest.mark.parametrize("name", ["mailbox", "mailtraffic"])
async def test_concealed_reports_skip_user_series(
    respx_graph: GraphClient, tenant: TenantConfig, name: str
) -> None:
    respx.get(f"{V1}/admin/reportSettings").mock(
        return_value=httpx.Response(200, json={"displayConcealedNames": True})
    )
    report = respx.get(url__regex=r".*/reports/.*").mock(return_value=httpx.Response(500))
    state: dict[str, object] = {}
    s = await all_collectors()[name].collect(make_ctx(respx_graph, tenant, state))
    assert s == [MetricSample("m365_reports_concealed", 1.0, {"report": name, "source": "setting"})]
    assert report.call_count == 0
    assert state["concealed_warned"] is True


async def test_onedrive_quota(fixture_graph: GraphClient, tenant: TenantConfig) -> None:
    ctx = make_ctx(fixture_graph, tenant, options=QUOTA)
    s = await all_collectors()["onedrive"].collect(ctx)
    alice = "alice@contoso.onmicrosoft.com"
    assert value(s, "m365_onedrive_used_bytes", upn=alice) == 549755813888
    assert value(s, "m365_onedrive_total_bytes", upn=alice) == 1099511627776
    assert value(s, "m365_onedrive_deleted_bytes", upn=alice) == 1048576
    # bob: licensed+enabled but drive 404 -> skipped; carol disabled; dave unlicensed; agent excluded
    assert {x.labels["upn"] for x in s} == {alice}


@respx.mock
async def test_onedrive_all_denied_fails(respx_graph: GraphClient, tenant: TenantConfig) -> None:
    respx.get(f"{V1}/users").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "u1",
                        "userPrincipalName": "a@x",
                        "accountEnabled": True,
                        "assignedLicenses": [{}],
                    }
                ]
            },
        )
    )
    respx.get(f"{V1}/users/u1/drive").mock(
        return_value=httpx.Response(403, json={"error": {"code": "accessDenied", "message": "no"}})
    )
    with pytest.raises(GraphAuthError):
        await all_collectors()["onedrive"].collect(make_ctx(respx_graph, tenant, options=QUOTA))


async def test_servicehealth(fixture_graph: GraphClient, tenant: TenantConfig) -> None:
    s = await collect("servicehealth", fixture_graph, tenant)
    assert (
        value(s, "m365_service_status", service="Microsoft Teams", status="serviceDegradation") == 1
    )
    assert (
        value(s, "m365_service_status", service="Exchange Online", status="serviceOperational") == 1
    )
    assert len(s) == 3


async def test_securescore(fixture_graph: GraphClient, tenant: TenantConfig) -> None:
    s = await collect("securescore", fixture_graph, tenant)
    assert value(s, "m365_securescore_current") == 42.5
    assert value(s, "m365_securescore_max") == 80


async def test_appsecrets(fixture_graph: GraphClient, tenant: TenantConfig) -> None:
    s = await collect("appsecrets", fixture_graph, tenant)
    assert len(s) == 2
    pwd = find(s, "m365_app_credential_expiry_timestamp", credential_type="password")[0]
    assert pwd.labels == {
        "app": "Payroll Connector",
        "app_id": "cccccccc-0000-4000-8000-000000000001",
        "credential_type": "password",
        "key_id": "dddddddd-0000-4000-8000-000000000001",
    }
    assert pwd.value == ts("2026-11-01T00:00:00")
    assert value(s, "m365_app_credential_expiry_timestamp", credential_type="certificate") == ts(
        "2027-03-15T10:00:00"
    )
