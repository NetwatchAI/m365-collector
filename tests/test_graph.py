from __future__ import annotations

import httpx
import pytest
import respx

from m365c.config import TenantConfig
from m365c.graph import (
    GraphAuthError,
    GraphClient,
    GraphError,
    GraphNotFoundError,
    parse_csv,
    parse_retry_after,
)
from m365c.health import GRAPH_REQUESTS, GRAPH_THROTTLED

from .conftest import SleepRecorder

V1 = "https://graph.microsoft.com/v1.0"


def _count(counter: object, *labels: str) -> float:
    return float(counter.labels(*labels)._value.get())  # type: ignore[attr-defined]


@respx.mock
async def test_get_all_follows_next_link(respx_graph: GraphClient, tenant: TenantConfig) -> None:
    first = respx.get(f"{V1}/users", params={"$top": "2"}).mock(
        return_value=httpx.Response(
            200,
            json={"value": [{"id": 1}, {"id": 2}], "@odata.nextLink": f"{V1}/users?$skiptoken=p2"},
        )
    )
    second = respx.get(f"{V1}/users", params={"$skiptoken": "p2"}).mock(
        return_value=httpx.Response(
            200, json={"value": [{"id": 3}], "@odata.nextLink": f"{V1}/users?$skiptoken=p3"}
        )
    )
    third = respx.get(f"{V1}/users", params={"$skiptoken": "p3"}).mock(
        return_value=httpx.Response(200, json={"value": [{"id": 4}]})
    )
    items = await respx_graph.for_tenant(tenant).get_all("/users", {"$top": "2"})
    assert [i["id"] for i in items] == [1, 2, 3, 4]
    assert first.call_count == second.call_count == third.call_count == 1
    # nextLink carries its own query: the original params are not re-sent
    assert "%24top" not in str(second.calls.last.request.url)
    assert second.calls.last.request.headers["Authorization"] == "Bearer test-token"


@respx.mock
async def test_429_honours_retry_after(
    respx_graph: GraphClient, tenant: TenantConfig, sleeper: SleepRecorder
) -> None:
    throttled_before = _count(GRAPH_THROTTLED, tenant.name, tenant.customer)
    route = respx.get(f"{V1}/subscribedSkus").mock(
        side_effect=[
            httpx.Response(
                429, headers={"Retry-After": "7"}, json={"error": {"code": "TooManyRequests"}}
            ),
            httpx.Response(200, json={"value": []}),
        ]
    )
    data = await respx_graph.for_tenant(tenant).get_json("/subscribedSkus")
    assert data == {"value": []}
    assert route.call_count == 2
    assert sleeper.calls == [7.0]
    assert _count(GRAPH_THROTTLED, tenant.name, tenant.customer) == throttled_before + 1
    assert _count(GRAPH_REQUESTS, tenant.name, tenant.customer, "429") >= 1


@respx.mock
async def test_503_without_retry_after_uses_exponential_backoff(
    respx_graph: GraphClient, tenant: TenantConfig, sleeper: SleepRecorder
) -> None:
    route = respx.get(f"{V1}/subscribedSkus").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json={"value": [1]}),
        ]
    )
    data = await respx_graph.for_tenant(tenant).get_json("/subscribedSkus")
    assert data["value"] == [1]
    assert route.call_count == 3
    assert len(sleeper.calls) == 2
    # attempt 0: 1s..2s, attempt 1: 2s..4s
    assert 1.0 <= sleeper.calls[0] <= 2.0
    assert 2.0 <= sleeper.calls[1] <= 4.0


@respx.mock
async def test_retries_exhausted_raises(
    respx_graph: GraphClient, tenant: TenantConfig, sleeper: SleepRecorder
) -> None:
    route = respx.get(f"{V1}/subscribedSkus").mock(return_value=httpx.Response(504))
    with pytest.raises(GraphError) as info:
        await respx_graph.for_tenant(tenant).get_json("/subscribedSkus")
    assert info.value.status == 504
    assert route.call_count == 4  # 1 + max_retries(3)
    assert len(sleeper.calls) == 3


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_errors_are_typed(
    respx_graph: GraphClient, tenant: TenantConfig, status: int
) -> None:
    respx.get(f"{V1}/users").mock(
        return_value=httpx.Response(
            status,
            json={
                "error": {
                    "code": "Authorization_RequestDenied",
                    "message": "Insufficient privileges",
                }
            },
        )
    )
    with pytest.raises(GraphAuthError) as info:
        await respx_graph.for_tenant(tenant).get_json("/users")
    assert info.value.status == status
    assert info.value.code == "Authorization_RequestDenied"
    assert info.value.message == "Insufficient privileges"


@respx.mock
async def test_404_is_typed(respx_graph: GraphClient, tenant: TenantConfig) -> None:
    respx.get(f"{V1}/users/x/drive").mock(
        return_value=httpx.Response(
            404, json={"error": {"code": "itemNotFound", "message": "nope"}}
        )
    )
    with pytest.raises(GraphNotFoundError):
        await respx_graph.for_tenant(tenant).get_json("/users/x/drive")


@respx.mock
async def test_report_redirect_drops_authorization(
    respx_graph: GraphClient, tenant: TenantConfig
) -> None:
    download = "https://reportsncu.office.com/data/abc.csv?tempauth=SECRET"
    report = respx.get(f"{V1}/reports/getMailboxUsageDetail(period='D7')").mock(
        return_value=httpx.Response(302, headers={"Location": download})
    )
    csv_body = (
        "﻿User Principal Name,Display Name,Storage Used (Byte)\r\n"
        'a@contoso.onmicrosoft.com,"Doe, Jane",10\r\n'
        'b@contoso.onmicrosoft.com,"Line ""quoted""",20\r\n'
    ).encode()
    fetched = respx.get(download).mock(return_value=httpx.Response(200, content=csv_body))

    rows = await respx_graph.for_tenant(tenant).get_report_csv(
        "/reports/getMailboxUsageDetail(period='D7')"
    )

    assert report.calls.last.request.headers["Authorization"] == "Bearer test-token"
    assert "Authorization" not in fetched.calls.last.request.headers
    assert rows == [
        {
            "User Principal Name": "a@contoso.onmicrosoft.com",
            "Display Name": "Doe, Jane",
            "Storage Used (Byte)": "10",
        },
        {
            "User Principal Name": "b@contoso.onmicrosoft.com",
            "Display Name": 'Line "quoted"',
            "Storage Used (Byte)": "20",
        },
    ]


async def test_httpx_auto_redirect_strips_auth_cross_origin() -> None:
    """Documents httpx behaviour we rely on as a second line of defence."""
    seen: dict[str, httpx.Headers] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.headers
        if request.url.host == "graph.microsoft.com":
            return httpx.Response(
                302, headers={"Location": "https://download.example.invalid/r.csv"}
            )
        return httpx.Response(200, text="ok")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as c:
        await c.get(f"{V1}/reports/x", headers={"Authorization": "Bearer t"})
    assert "authorization" in seen["graph.microsoft.com"]
    assert "authorization" not in seen["download.example.invalid"]


def test_parse_csv_handles_bom_and_commas() -> None:
    raw = '﻿User Principal Name,Assigned Products\nx@y.z,"A, B"\n'.encode()
    assert parse_csv(raw) == [{"User Principal Name": "x@y.z", "Assigned Products": "A, B"}]


def test_parse_retry_after() -> None:
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None
    assert parse_retry_after("99999") == 600.0
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0
