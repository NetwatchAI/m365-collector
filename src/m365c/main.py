"""Service entry point: load config, start the scheduler and the HTTP server."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from pathlib import Path

import structlog
from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from m365c import __version__
from m365c.auth import CertificateTokenProvider
from m365c.cache import SnapshotCache
from m365c.collectors import all_collectors
from m365c.config import ConfigWatcher, LoadedConfig, Thresholds, load_all
from m365c.exporter import register
from m365c.graph import GraphClient
from m365c.health import REGISTRY
from m365c.scheduler import Scheduler
from m365c.secrets import load_secret
from m365c.state import AppState

log = structlog.get_logger(__name__)


def build_app(state: AppState) -> web.Application:
    async def metrics(_: web.Request) -> web.Response:
        body = generate_latest(REGISTRY)
        return web.Response(body=body, headers={"Content-Type": CONTENT_TYPE_LATEST})

    async def healthz(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def readyz(_: web.Request) -> web.Response:
        ready = state.config_loaded and state.health.attempted_any
        return web.json_response(
            {
                "status": "ready" if ready else "starting",
                "config_loaded": state.config_loaded,
                "collector_cycle_attempted": state.health.attempted_any,
            },
            status=200 if ready else 503,
        )

    app = web.Application()
    app.router.add_get("/metrics", metrics)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)
    return app


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows event loops
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


async def serve(config_dir: Path) -> None:
    collectors = all_collectors()
    cfg: LoadedConfig = load_all(config_dir, list(collectors))
    settings = cfg.settings

    certificate = load_secret(settings.secrets.cert)
    password = (
        load_secret(settings.secrets.cert_password) if settings.secrets.cert_password else None
    )
    tokens = CertificateTokenProvider(settings.app.client_id, certificate, password)
    graph = GraphClient(tokens, settings.http)

    state = AppState(collectors=collectors, cache=SnapshotCache(settings.cache.persist_dir))
    state.thresholds = cfg.thresholds
    register(state, REGISTRY)
    scheduler = Scheduler(state, graph, settings)

    valid = {
        (t.id, c) for t in cfg.tenants if t.enabled for c in t.enabled_collectors(list(collectors))
    }
    restored = state.cache.load(valid)
    await scheduler.apply_tenants(cfg.tenants)
    state.config_loaded = True

    async def on_thresholds(thresholds: Thresholds) -> None:
        state.thresholds = thresholds

    watcher = ConfigWatcher(
        config_dir,
        list(collectors),
        on_tenants=scheduler.apply_tenants,
        on_thresholds=on_thresholds,
        interval=settings.reload_interval,
    )
    watch_task = asyncio.create_task(watcher.run(), name="config-watcher")

    runner = web.AppRunner(build_app(state), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.server.host, settings.server.port)
    await site.start()
    log.info(
        "started",
        version=__version__,
        host=settings.server.host,
        port=settings.server.port,
        tenants=len(state.active_tenants()),
        tasks=len(scheduler.task_keys),
        restored_snapshots=restored,
    )

    stop = asyncio.Event()
    _install_signal_handlers(stop)
    try:
        await stop.wait()
    finally:
        log.info("stopping")
        watch_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watch_task
        await scheduler.stop()
        await runner.cleanup()
        await graph.aclose()
        await tokens.aclose()
        log.info("stopped")
