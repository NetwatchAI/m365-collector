from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import httpx

from m365c.cache import Snapshot, SnapshotCache
from m365c.collectors.base import MetricSample
from m365c.config import Settings, TenantConfig
from m365c.fixtures import fixture_transport
from m365c.scheduler import Scheduler
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
    assert not health.success and not health.auth_failed
    assert health.last_success_ts == last_success
    assert app_state.health.auth_ok(tenant.id) is True
    await good.aclose()
    await broken.aclose()


async def test_auth_failure_sets_auth_not_ok(app_state: AppState, tenant: TenantConfig) -> None:
    denied = make_fixture_graph(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                403, json={"error": {"code": "Authorization_RequestDenied", "message": "x"}}
            )
        )
    )
    scheduler = Scheduler(app_state, denied, settings())
    result = await scheduler.run_once(tenant, "license")
    assert not result.ok
    assert app_state.health.auth_ok(tenant.id) is False
    await denied.aclose()


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
    assert restored.load({("t1", "license")}) == 1
    assert restored.get("t1", "license") == snap
    assert restored.get("t2", "license") is None

    cache.drop_tenant("t1")
    assert not list(tmp_path.glob("t1__*.json"))
    assert list(tmp_path.glob("t2__*.json"))


def test_fixture_transport_never_leaks_auth() -> None:
    # sanity check for the dev-aid transport itself
    transport = fixture_transport(GRAPH_FIXTURES)
    with httpx.Client(transport=transport) as c:  # type: ignore[arg-type]
        r = c.get("https://graph.microsoft.com/v1.0/reports/getMailboxUsageDetail(period='D7')")
        assert r.status_code == 302
