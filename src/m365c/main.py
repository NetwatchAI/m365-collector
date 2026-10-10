"""Service entry point: load config, start the scheduler and the HTTP server."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from pathlib import Path

import httpx
import structlog
from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

from m365c import __version__
from m365c.auth import (
    CertificateInfo,
    TenantTokenProvider,
    TokenProvider,
    load_app_certificate,
)
from m365c.cache import SnapshotCache
from m365c.collectors import all_collectors
from m365c.config import (
    ConfigWatcher,
    Thresholds,
    hot_reload_env_overrides,
    load_all,
    uses_default_certificate,
)
from m365c.exporter import register
from m365c.graph import GraphClient
from m365c.health import REGISTRY
from m365c.scheduler import Scheduler
from m365c.state import AppState

log = structlog.get_logger(__name__)

CERT_WARN_DAYS = 30


def build_app(state: AppState, registry: CollectorRegistry = REGISTRY) -> web.Application:
    async def metrics(_: web.Request) -> web.Response:
        # Rendering a large cache takes seconds: do it off the event loop so Graph I/O and
        # /healthz are not stalled during a scrape.
        body = await asyncio.to_thread(generate_latest, registry)
        return web.Response(body=body, headers={"Content-Type": CONTENT_TYPE_LATEST})

    async def healthz(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def readyz(_: web.Request) -> web.Response:
        # With no tenants configured there is nothing to wait for.
        ready = state.config_loaded and (state.health.attempted_any or not state.active_tenants())
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


def _log_certificate(info: CertificateInfo) -> None:
    days_left = (info.not_after - time.time()) / 86400
    fields = {
        "subject": info.subject,
        "thumbprint": info.thumbprint,
        "days_left": round(days_left, 1),
    }
    if days_left <= 0:
        log.error("certificate_expired", **fields, hint="every token request will fail")
    elif days_left <= CERT_WARN_DAYS:
        log.warning("certificate_expiring", **fields)
    else:
        log.info("certificate_loaded", **fields)


async def serve(
    config_dir: Path,
    *,
    tokens: TokenProvider | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    registry: CollectorRegistry = REGISTRY,
    stop: asyncio.Event | None = None,
) -> None:
    """Run until SIGTERM/SIGINT (or ``stop`` is set).

    ``tokens``/``transport``/``registry``/``stop`` exist for tests; in production each
    tenant's configured auth method is used and the real network is called.
    """
    collectors = all_collectors()
    cfg = load_all(config_dir, list(collectors))
    settings = cfg.settings
    for var in hot_reload_env_overrides():
        log.warning(
            "env_overrides_hot_reload_file",
            variable=var,
            hint="edits to the corresponding YAML file have no effect while this is set",
        )

    state = AppState(collectors=collectors, cache=SnapshotCache(settings.cache.persist_dir))
    state.thresholds = cfg.thresholds
    if tokens is None:
        if settings.secrets.cert is not None and (
            settings.app.method == "certificate" or uses_default_certificate(settings, cfg.tenants)
        ):
            # The shared default certificate is checked now, as before: a broken one must
            # fail startup, not every tenant's token request later. Raises CertificateError /
            # SecretError. Per-tenant secrets (client secrets, own certificates) load lazily,
            # so one tenant's broken reference cannot stop the others.
            _, _, info = load_app_certificate(settings.secrets.cert, settings.secrets.cert_password)
            _log_certificate(info)
            state.certificate = info
        tokens = TenantTokenProvider()

    async with contextlib.AsyncExitStack() as stack:
        # Callbacks run in reverse order: this one runs last.
        stack.callback(log.info, "stopped")
        stack.push_async_callback(tokens.aclose)
        graph = GraphClient(tokens, settings.http, transport=transport)
        stack.push_async_callback(graph.aclose)

        exporter = register(state, registry)
        stack.callback(registry.unregister, exporter)
        scheduler = Scheduler(state, graph, settings)
        stack.push_async_callback(scheduler.stop)

        valid = {
            (t.id, c)
            for t in cfg.tenants
            if t.enabled
            for c in t.enabled_collectors(list(collectors))
        }
        restored = state.cache.load(valid, delete_unknown=True)
        for snap in restored:
            state.health.seed_restored(
                snap.tenant_id, snap.collector, snap.collected_at, len(snap.samples)
            )
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
            settings=settings,
        )
        watch_task = asyncio.create_task(watcher.run(), name="config-watcher")

        async def stop_watcher() -> None:
            watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watch_task

        stack.push_async_callback(stop_watcher)

        runner = web.AppRunner(build_app(state, registry), access_log=None)
        await runner.setup()
        stack.push_async_callback(runner.cleanup)
        site = web.TCPSite(runner, settings.server.host, settings.server.port)
        await site.start()
        log.info(
            "started",
            version=__version__,
            host=settings.server.host,
            port=settings.server.port,
            tenants=len(state.active_tenants()),
            tasks=len(scheduler.task_keys),
            restored_snapshots=len(restored),
        )

        stop = stop or asyncio.Event()
        _install_signal_handlers(stop)
        await stop.wait()
        log.info("stopping")
