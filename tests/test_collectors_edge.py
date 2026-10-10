from __future__ import annotations

import httpx
import pytest
import respx

from m365c.collectors import all_collectors
from m365c.collectors.base import MetricSample
from m365c.config import TenantConfig
from m365c.graph import GraphClient, GraphError

from .conftest import QUOTA, make_ctx
from .test_collectors import collect, find, ts, value

V1 = "https://graph.microsoft.com/v1.0"
MAILTRAFFIC = f"{V1}/reports/getEmailActivityUserDetail(period='D7')"
DOWNLOAD = "https://reports.office.com/data/download/abc"


async def test_users_created_timestamp_and_successful_signin_only(
    fixture_graph: GraphClient, tenant: TenantConfig
) -> None:
    s = await collect("users", fixture_graph, tenant)
    carol = "carol@contoso.onmicrosoft.com"
    assert value(s, "m365_user_created_timestamp", upn=carol) == ts("2023-05-05T09:00:00")
    assert not find(s, "m365_user_last_signin_timestamp", upn=carol)  # never signed in
    # Alice's later non-interactive *attempt* (2026-10-05) is ignored.
    assert value(s, "m365_user_last_signin_timestamp", upn="alice@contoso.onmicrosoft.com") == ts(
        "2026-10-01T08:00:00"
    )


@respx.mock
async def test_servicehealth_status_is_normalised(
    respx_graph: GraphClient, tenant: TenantConfig
) -> None:
    respx.get(f"{V1}/admin/serviceAnnouncement/healthOverviews").mock(
        return_value=httpx.Response(
            200,
            json={"value": [
                {"service": "Exchange Online", "status": "ServiceOperational"},
                {"service": "Microsoft Teams", "status": "ServiceDegradation"},
            ]},
        )
    )  # fmt: skip
    s = await all_collectors()["servicehealth"].collect(make_ctx(respx_graph, tenant))
    assert {x.labels["status"] for x in s} == {"serviceOperational", "serviceDegradation"}


def _mock_report(settings: dict[str, object], csv: str) -> respx.Route:
    respx.get(f"{V1}/admin/reportSettings").mock(return_value=httpx.Response(200, json=settings))
    respx.get(MAILTRAFFIC).mock(return_value=httpx.Response(302, headers={"Location": DOWNLOAD}))
    return respx.get(DOWNLOAD).mock(return_value=httpx.Response(200, content=csv.encode()))


CSV_HEAD = "User Principal Name,Is Deleted,Send Count,Receive Count,Read Count\n"


@respx.mock
async def test_report_settings_value_wrapper_is_understood(
    respx_graph: GraphClient, tenant: TenantConfig
) -> None:
    download = _mock_report({"value": {"displayConcealedNames": True}}, CSV_HEAD)
    s = await all_collectors()["mailtraffic"].collect(make_ctx(respx_graph, tenant))
    assert s == [
        MetricSample("m365_reports_concealed", 1.0, {"report": "mailtraffic", "source": "setting"})
    ]
    assert download.call_count == 0


@respx.mock
async def test_report_settings_without_flag_fails_closed(
    respx_graph: GraphClient, tenant: TenantConfig
) -> None:
    _mock_report({"id": "reportSettings"}, CSV_HEAD)
    with pytest.raises(GraphError, match="displayConcealedNames"):
        await all_collectors()["mailtraffic"].collect(make_ctx(respx_graph, tenant))


@respx.mock
async def test_hashed_upns_are_detected_as_concealed(
    respx_graph: GraphClient, tenant: TenantConfig
) -> None:
    csv = CSV_HEAD + "5a3f9c0e1b2d4f6a8b7c9d0e1f2a3b4c,False,3,4,5\nB1C2D3E4F5A6,False,1,1,1\n"
    _mock_report({"displayConcealedNames": False}, csv)
    state: dict[str, object] = {}
    s = await all_collectors()["mailtraffic"].collect(make_ctx(respx_graph, tenant, state))
    assert s == [
        MetricSample(
            "m365_reports_concealed", 1.0, {"report": "mailtraffic", "source": "heuristic"}
        )
    ]
    assert state["concealed_warned"] is True


@respx.mock
async def test_rows_without_real_upn_are_skipped(
    respx_graph: GraphClient, tenant: TenantConfig
) -> None:
    csv = CSV_HEAD + "a@contoso.onmicrosoft.com,False,3,4,5\n5a3f9c0e1b2d4f6a,False,9,9,9\n"
    _mock_report({"displayConcealedNames": False}, csv)
    s = await all_collectors()["mailtraffic"].collect(make_ctx(respx_graph, tenant))
    assert {x.labels.get("upn") for x in s if x.name == "m365_mail_sent"} == {
        "a@contoso.onmicrosoft.com"
    }


def _users(n: int) -> dict[str, object]:
    return {
        "value": [
            {
                "id": f"u{i}",
                "userPrincipalName": f"u{i}@x",
                "accountEnabled": True,
                "assignedLicenses": [{}],
            }
            for i in range(n)
        ]
    }


@respx.mock
async def test_onedrive_keeps_results_on_isolated_errors(
    respx_graph: GraphClient, tenant: TenantConfig
) -> None:
    respx.get(f"{V1}/users").mock(return_value=httpx.Response(200, json=_users(50)))

    def drive(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/u7/drive"):
            return httpx.Response(500)
        if request.url.path.endswith("/u8/drive"):
            return httpx.Response(403, json={"error": {"code": "accessDenied", "message": "x"}})
        return httpx.Response(200, json={"quota": {"used": 1, "total": 10, "deleted": 0}})

    respx.get(url__regex=r".*/users/u\d+/drive.*").mock(side_effect=drive)
    s = await all_collectors()["onedrive"].collect(make_ctx(respx_graph, tenant, options=QUOTA))
    assert len(find(s, "m365_onedrive_used_bytes")) == 48  # 2 failed, 48 kept


@respx.mock
async def test_onedrive_fails_when_too_many_users_fail(
    respx_graph: GraphClient, tenant: TenantConfig
) -> None:
    respx.get(f"{V1}/users").mock(return_value=httpx.Response(200, json=_users(10)))

    def drive(request: httpx.Request) -> httpx.Response:
        if request.url.path.split("/")[-2] in {"u1", "u2"}:
            return httpx.Response(500)
        return httpx.Response(200, json={"quota": {"used": 1, "total": 10}})

    respx.get(url__regex=r".*/users/u\d+/drive.*").mock(side_effect=drive)
    with pytest.raises(GraphError, match="2 of 10 drives failed"):
        await all_collectors()["onedrive"].collect(make_ctx(respx_graph, tenant, options=QUOTA))


@respx.mock
async def test_onedrive_concurrency_is_bounded(
    respx_graph: GraphClient, tenant: TenantConfig
) -> None:
    respx.get(f"{V1}/users").mock(return_value=httpx.Response(200, json=_users(30)))
    in_flight = peak = 0

    async def drive(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        import asyncio

        await asyncio.sleep(0.005)
        in_flight -= 1
        return httpx.Response(200, json={"quota": {"used": 1, "total": 10}})

    respx.get(url__regex=r".*/users/u\d+/drive.*").mock(side_effect=drive)
    await all_collectors()["onedrive"].collect(make_ctx(respx_graph, tenant, options=QUOTA))
    assert peak <= respx_graph.per_tenant_concurrency


@respx.mock
async def test_securescore_picks_newest(respx_graph: GraphClient, tenant: TenantConfig) -> None:
    respx.get(f"{V1}/security/secureScores").mock(
        return_value=httpx.Response(
            200,
            json={"value": [
                {"createdDateTime": "2026-10-01T00:00:00Z", "currentScore": 10, "maxScore": 80},
                {"createdDateTime": "2026-10-07T00:00:00Z", "currentScore": 42, "maxScore": 80},
                {"createdDateTime": "2026-10-03T00:00:00Z", "currentScore": 20, "maxScore": 80},
            ]},
        )
    )  # fmt: skip
    s = await all_collectors()["securescore"].collect(make_ctx(respx_graph, tenant))
    assert value(s, "m365_securescore_current") == 42
