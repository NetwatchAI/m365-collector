from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import httpx
import pytest

from m365c import health as health_module
from m365c.auth import TokenError
from m365c.cache import Snapshot, SnapshotCache
from m365c.collectors.base import MetricSample
from m365c.config import HttpConfig, Settings, TenantConfig
from m365c.fixtures import fixture_transport
from m365c.graph import GraphClient
from m365c.health import FailureKind
from m365c.scheduler import Scheduler, next_delay
from m365c.state import AppState

from .conftest import GRAPH_FIXTURES, SleepRecorder, make_fixture_graph


def settings() -> Settings:
    return Settings(
        app={"client_id": "00000000-0000-0000-0000-000000000000"}, secrets={"cert": "file:x"}
    )


def upns(state: AppState, tenant: TenantConfig) -> set[str]:
    snap = state.cache.get(tenant.id, "users")
    assert snap is not None
    return {s.labels["upn"] for s in snap.samples if s.name == "m365_user_info"}


async def test_successful_run_replaces_snapshot(
    tmp_path: Path, app_state: AppState, tenant: TenantConfig
) -> None:
    fixtures = tmp_path / "graph"
    shutil.copytree(GRAPH_FIXTURES, fixtures)
    graph = make_fixture_graph(fixtures)
    scheduler = Scheduler(app_state, graph, settings())

    assert (await scheduler.run_once(tenant, "users")).ok
    assert "dave@contoso.onmicrosoft.com" in upns(app_state, tenant)

    # Dave leaves the company: page 2 now only holds the agent user.
    page2 = fixtures / "users__page2.json"
    data = json.loads(page2.read_text(encoding="utf-8"))
    data["value"] = [
        u for u in data["value"] if u["userPrincipalName"] != "dave@contoso.onmicrosoft.com"
    ]
    page2.write_text(json.dumps(data), encoding="utf-8")

    assert (await scheduler.run_once(tenant, "users")).ok
    assert "dave@contoso.onmicrosoft.com" not in upns(app_state, tenant)
    assert app_state.health.runs[(tenant.id, "users")].success
    await graph.aclose()


async def test_failed_run_keeps_previous_snapshot(
    app_state: AppState, tenant: TenantConfig
) -> None:
    good = make_fixture_graph()
    scheduler = Scheduler(app_state, good, settings())
    assert (await scheduler.run_once(tenant, "license")).ok
    before = app_state.cache.get(tenant.id, "license")
    assert before is not None
    last_success = app_state.health.runs[(tenant.id, "license")].last_success_ts

    broken = make_fixture_graph(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    scheduler = Scheduler(app_state, broken, settings())
    result = await scheduler.run_once(tenant, "license")
    assert not result.ok
    assert app_state.cache.get(tenant.id, "license") is before
    health = app_state.health.runs[(tenant.id, "license")]
    assert not health.success and health.failure is FailureKind.OTHER
    assert health.last_success_ts == last_success
    assert app_state.health.auth_ok(tenant.id) is True
    await good.aclose()
    await broken.aclose()


def _error_graph(status: int, code: str) -> GraphClient:
    return make_fixture_graph(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(status, json={"error": {"code": code, "message": "x"}})
        )
    )


async def test_403_is_a_permission_problem_not_auth(
    app_state: AppState, tenant: TenantConfig
) -> None:
    denied = _error_graph(403, "Authorization_RequestDenied")
    scheduler = Scheduler(app_state, denied, settings())
    assert not (await scheduler.run_once(tenant, "license")).ok
    assert app_state.health.runs[(tenant.id, "license")].failure is FailureKind.FORBIDDEN
    assert app_state.health.auth_ok(tenant.id) is True  # token fine, Graph answered
    await denied.aclose()


async def test_auth_failure_and_fast_recovery(app_state: AppState, tenant: TenantConfig) -> None:
    unauthorized = _error_graph(401, "InvalidAuthenticationToken")
    scheduler = Scheduler(app_state, unauthorized, settings())
    assert not (await scheduler.run_once(tenant, "appsecrets")).ok
    assert app_state.health.auth_ok(tenant.id) is False

    # Consent fixed: the next run of *any* collector (servicehealth every 5m) recovers it,
    # without waiting for the daily appsecrets run.
    good = make_fixture_graph()
    scheduler = Scheduler(app_state, good, settings())
    assert (await scheduler.run_once(tenant, "servicehealth")).ok
    assert app_state.health.auth_ok(tenant.id) is True
    await unauthorized.aclose()
    await good.aclose()


class _FailingTokens:
    def __init__(self, transient: bool) -> None:
        self.transient = transient

    async def get_token(self, tenant_id: str) -> str:
        raise TokenError(tenant_id, "AADSTS700016: app not found", transient=self.transient)

    async def aclose(self) -> None:
        return None


@pytest.mark.parametrize(("transient", "auth_ok"), [(False, False), (True, None)])
async def test_token_failures(
    app_state: AppState, tenant: TenantConfig, transient: bool, auth_ok: bool | None
) -> None:
    graph = GraphClient(_FailingTokens(transient), HttpConfig(max_retries=0))
    scheduler = Scheduler(app_state, graph, settings())
    assert not (await scheduler.run_once(tenant, "license")).ok
    # A transient identity-service error says nothing about consent: auth_ok stays unknown.
    assert app_state.health.auth_ok(tenant.id) is auth_ok
    await graph.aclose()


def test_retry_delay_after_failures() -> None:
    assert next_delay(86400, 0) == 86400
    assert [next_delay(86400, n) for n in (1, 2, 3, 4, 5)] == [300, 600, 1200, 2400, 3600]
    assert next_delay(300, 3) == 300  # never later than the normal interval


async def test_failed_run_retries_sooner(app_state: AppState, tenant: TenantConfig) -> None:
    sleeps: list[float] = []
    done = asyncio.Event()

    async def record(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 4:
            done.set()
            await asyncio.Event().wait()

    graph = make_fixture_graph(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    scheduler = Scheduler(app_state, graph, settings(), sleep=record, rng=lambda: 0.0)
    await scheduler.apply_tenants([tenant.model_copy(update={"collectors": ["appsecrets"]})])
    await asyncio.wait_for(done.wait(), timeout=5)
    assert sleeps[:4] == [0.0, 300.0, 600.0, 1200.0]  # jitter, then backoff, not 24h
    await scheduler.stop()
    await graph.aclose()


async def test_disabled_tenant_forgets_graph_counters(
    app_state: AppState, tenant: TenantConfig
) -> None:
    graph = make_fixture_graph()
    scheduler = Scheduler(app_state, graph, settings())
    await scheduler.apply_tenants([tenant])
    await scheduler.run_once(tenant, "license")
    assert ("contoso.onmicrosoft.com", "contoso", "200") in health_module._request_labels
    await scheduler.apply_tenants([tenant.model_copy(update={"enabled": False})])
    assert not [x for x in health_module._request_labels if x[1] == "contoso"]
    await scheduler.stop()
    await graph.aclose()


async def test_dry_run_writes_nothing(app_state: AppState, tenant: TenantConfig) -> None:
    graph = make_fixture_graph()
    scheduler = Scheduler(app_state, graph, settings())
    result = await scheduler.run_once(tenant, "license", write=False)
    assert result.ok and result.samples
    assert app_state.cache.get(tenant.id, "license") is None
    assert not app_state.health.runs
    await graph.aclose()


async def test_apply_tenants_starts_and_drops(app_state: AppState, tenant: TenantConfig) -> None:
    graph = make_fixture_graph()
    sleeper = SleepRecorder()

    async def never(_: float) -> None:
        sleeper.calls.append(_)
        await asyncio.Event().wait()

    scheduler = Scheduler(app_state, graph, settings(), sleep=never, rng=lambda: 0.5)
    other = TenantConfig(
        id="22222222-2222-2222-2222-222222222222",
        name="fabrikam.onmicrosoft.com",
        customer="fabrikam",
        collectors=["license", "servicehealth"],
    )
    await scheduler.apply_tenants([tenant, other])
    assert len(scheduler.task_keys) == 8 + 2
    await asyncio.sleep(0)
    # first-run jitter is rng * min(interval, 5m): servicehealth (5m) -> 150s, license (1h) -> 150s
    assert set(sleeper.calls) == {150.0}

    app_state.cache.replace(Snapshot(other.id, "license", 0.0, (MetricSample("m365_x", 1.0),)))
    app_state.health.record_success(other.id, "license", 0.1, 1)
    await scheduler.apply_tenants([tenant])
    assert {k[0] for k in scheduler.task_keys} == {tenant.id}
    assert app_state.cache.get(other.id, "license") is None
    assert (other.id, "license") not in app_state.health.runs

    # narrowing a tenant's collectors drops just those tasks
    narrowed = tenant.model_copy(update={"collectors": ["license"]})
    await scheduler.apply_tenants([narrowed])
    assert scheduler.task_keys == {(tenant.id, "license")}

    await scheduler.apply_tenants([narrowed.model_copy(update={"enabled": False})])
    assert scheduler.task_keys == set()
    await scheduler.stop()
    await graph.aclose()


async def test_scheduler_loop_survives_collector_crash(
    app_state: AppState, tenant: TenantConfig
) -> None:
    calls = 0
    done = asyncio.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls >= 3:
            done.set()
        return httpx.Response(200, content=b"not json")  # makes the collector raise

    graph = make_fixture_graph(transport=httpx.MockTransport(handler))

    async def fast(_: float) -> None:
        await asyncio.sleep(0)

    scheduler = Scheduler(app_state, graph, settings(), sleep=fast, rng=lambda: 0.0)
    await scheduler.apply_tenants([tenant.model_copy(update={"collectors": ["license"]})])
    await asyncio.wait_for(done.wait(), timeout=5)
    assert app_state.health.runs[(tenant.id, "license")].success is False
    await scheduler.stop()
    await graph.aclose()


def test_cache_persistence_round_trip(tmp_path: Path) -> None:
    cache = SnapshotCache(tmp_path)
    snap = Snapshot(
        "t1", "license", 123.0, (MetricSample("m365_license_total", 5.0, {"sku": "SPB"}),)
    )
    cache.replace(snap)
    cache.replace(Snapshot("t2", "license", 1.0, ()))

    restored = SnapshotCache(tmp_path)
    assert restored.load({("t1", "license")}) == [snap]
    assert restored.get("t1", "license") == snap
    assert restored.get("t2", "license") is None
    assert list(tmp_path.glob("t2__*.json"))  # kept unless asked to delete

    SnapshotCache(tmp_path).load({("t1", "license")}, delete_unknown=True)
    assert not list(tmp_path.glob("t2__*.json"))  # orphaned user data removed
    cache.replace(Snapshot("t2", "license", 1.0, ()))

    cache.drop_tenant("t1")
    assert not list(tmp_path.glob("t1__*.json"))
    assert list(tmp_path.glob("t2__*.json"))


def test_fixture_transport_rejects_auth_on_download_host() -> None:
    """The dev-aid transport fails loudly if anything sends Authorization to the
    pre-authenticated download host."""
    with httpx.Client(transport=fixture_transport(GRAPH_FIXTURES)) as c:
        r = c.get("https://graph.microsoft.com/v1.0/reports/getMailboxUsageDetail(period='D7')")
        assert r.status_code == 302
        location = r.headers["Location"]
        assert c.get(location).status_code == 200
        with pytest.raises(AssertionError, match="auth header leaked"):
            c.get(location, headers={"Authorization": "Bearer x"})
