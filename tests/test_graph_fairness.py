from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
import respx

from m365c.auth import StaticTokenProvider
from m365c.config import HttpConfig, TenantConfig
from m365c.graph import GraphAuthError, GraphClient, GraphError
from m365c.logging import configure_logging

V1 = "https://graph.microsoft.com/v1.0"


class SlowTransport(httpx.AsyncBaseTransport):
    """Every request takes ``delay`` seconds; records concurrency per tenant path prefix."""

    def __init__(self, delay: float) -> None:
        self.delay = delay

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(self.delay)
        return httpx.Response(200, json={"value": []})


async def test_one_tenant_backlog_does_not_starve_another() -> None:
    """Tenant A queues 200 requests (per-tenant limit 4); a single request from tenant B
    must not wait behind A's backlog (review H1: it used to take ~2.8s)."""
    http = HttpConfig(max_concurrency=20, per_tenant_concurrency=4, max_retries=0)
    graph = GraphClient(StaticTokenProvider(), http, transport=SlowTransport(0.05))
    a = TenantConfig(id="11111111-1111-1111-1111-111111111111", name="a", customer="a")
    b = TenantConfig(id="22222222-2222-2222-2222-222222222222", name="b", customer="b")

    backlog = [
        asyncio.create_task(graph.for_tenant(a).get_json("/users/x/drive")) for _ in range(200)
    ]
    await asyncio.sleep(0.01)  # A's backlog is queued
    start = time.perf_counter()
    await graph.for_tenant(b).get_json("/admin/serviceAnnouncement/healthOverviews")
    b_latency = time.perf_counter() - start
    await asyncio.gather(*backlog)
    await graph.aclose()
    assert b_latency < 0.5, f"tenant B waited {b_latency:.2f}s behind tenant A"


@respx.mock
async def test_report_download_path_is_never_logged(
    respx_graph: GraphClient, tenant: TenantConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("DEBUG", json=True)
    download = "https://reports.office.com/data/download/JDFKdf2_eJXKS034dbc7e0t__XDe"
    respx.get(f"{V1}/reports/getMailboxUsageDetail(period='D7')").mock(
        return_value=httpx.Response(302, headers={"Location": download})
    )
    respx.get(download).mock(
        return_value=httpx.Response(200, content=b"\xef\xbb\xbfA,B\r\n1,2\r\n")
    )
    await respx_graph.for_tenant(tenant, "mailbox").get_report_csv(
        "/reports/getMailboxUsageDetail(period='D7')"
    )
    out = capsys.readouterr().out
    assert "JDFKdf2_eJXKS034dbc7e0t__XDe" not in out
    events = [json.loads(line) for line in out.splitlines() if '"graph_request"' in line]
    assert {e["path"] for e in events} == {
        "/v1.0/reports/getMailboxUsageDetail(period='D7')",
        "<report-download>",
    }
    assert all(e["collector"] == "mailbox" and e["customer"] == "contoso" for e in events)


@respx.mock
async def test_download_host_403_is_not_a_tenant_auth_error(
    respx_graph: GraphClient, tenant: TenantConfig
) -> None:
    download = "https://reports.office.com/data/download/expired"
    respx.get(f"{V1}/reports/getEmailActivityUserDetail(period='D7')").mock(
        return_value=httpx.Response(302, headers={"Location": download})
    )
    respx.get(download).mock(return_value=httpx.Response(403))
    with pytest.raises(GraphError) as info:
        await respx_graph.for_tenant(tenant).get_report_csv(
            "/reports/getEmailActivityUserDetail(period='D7')"
        )
    assert not isinstance(info.value, GraphAuthError)


async def test_token_is_fetched_outside_the_semaphores() -> None:
    """A slow token endpoint must not hold Graph slots (review L2)."""
    release = asyncio.Event()

    class SlowTokens:
        async def get_token(self, tenant_id: str) -> str:
            if tenant_id.startswith("1"):
                await release.wait()
            return "t"

        async def aclose(self) -> None:
            return None

    http = HttpConfig(max_concurrency=1, per_tenant_concurrency=1, max_retries=0)
    graph = GraphClient(SlowTokens(), http, transport=SlowTransport(0.0))
    a = TenantConfig(id="11111111-1111-1111-1111-111111111111", name="a", customer="a")
    b = TenantConfig(id="22222222-2222-2222-2222-222222222222", name="b", customer="b")
    stuck = asyncio.create_task(graph.for_tenant(a).get_json("/x"))
    await asyncio.sleep(0.01)
    await asyncio.wait_for(graph.for_tenant(b).get_json("/x"), timeout=1)  # not blocked by A
    release.set()
    await stuck
    await graph.aclose()
