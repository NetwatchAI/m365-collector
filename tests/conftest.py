from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog

from m365c.auth import StaticTokenProvider
from m365c.cache import SnapshotCache
from m365c.collectors import all_collectors
from m365c.collectors.base import CollectorContext
from m365c.config import HttpConfig, TenantConfig, Thresholds
from m365c.fixtures import fixture_transport
from m365c.graph import GraphClient
from m365c.state import AppState

FIXTURES = Path(__file__).parent / "fixtures"
GRAPH_FIXTURES = FIXTURES / "graph"
CONFIG_FIXTURES = FIXTURES / "config"

TENANT_ID = "11111111-1111-1111-1111-111111111111"


class SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def tenant() -> TenantConfig:
    return TenantConfig(id=TENANT_ID, name="contoso.onmicrosoft.com", customer="contoso")


@pytest.fixture
def sleeper() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
def http_config() -> HttpConfig:
    return HttpConfig(max_retries=3)


@pytest.fixture
async def respx_graph(
    http_config: HttpConfig, sleeper: SleepRecorder
) -> AsyncIterator[GraphClient]:
    """GraphClient on httpx's default transport (so respx can intercept it)."""
    client = GraphClient(StaticTokenProvider("test-token"), http_config, sleep=sleeper)
    yield client
    await client.aclose()


def make_fixture_graph(
    directory: Path = GRAPH_FIXTURES, transport: httpx.AsyncBaseTransport | None = None
) -> GraphClient:
    return GraphClient(
        StaticTokenProvider("test-token"),
        HttpConfig(max_retries=0),
        transport=transport or fixture_transport(directory),
        sleep=SleepRecorder(),
    )


@pytest.fixture
async def fixture_graph() -> AsyncIterator[GraphClient]:
    client = make_fixture_graph()
    yield client
    await client.aclose()


def make_ctx(
    graph: GraphClient, tenant: TenantConfig, state: dict[str, Any] | None = None
) -> CollectorContext:
    return CollectorContext(
        tenant=tenant,
        graph=graph.for_tenant(tenant),
        log=structlog.get_logger("test").bind(tenant=tenant.name),
        state=state if state is not None else {},
    )


@pytest.fixture
def app_state(tenant: TenantConfig) -> AppState:
    state = AppState(collectors=all_collectors(), cache=SnapshotCache(), thresholds=Thresholds())
    state.tenants = {tenant.id: tenant}
    state.config_loaded = True
    return state
