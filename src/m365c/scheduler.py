"""One asyncio task per ``(tenant, collector)``, with a jittered first run."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from m365c.auth import TokenError
from m365c.cache import Snapshot
from m365c.collectors.base import CollectorContext, MetricSample, Requirement, TenantOptions
from m365c.config import (
    Settings,
    TenantConfig,
    onedrive_source,
    report_period,
    validate_auth,
)
from m365c.graph import GraphAuthError, GraphClient
from m365c.health import FailureKind, forget_graph_counters
from m365c.state import AppState

log = structlog.get_logger(__name__)

MAX_INITIAL_JITTER_S = 300.0
# After a failed run, retry sooner than the full interval: 5m, 10m, 20m, ... capped at 1h
# (and never later than the normal interval).
RETRY_BASE_S = 300.0
RETRY_CAP_S = 3600.0
SKIPPED_RECHECK_S = 3600.0

Key = tuple[str, str]


def classify_failure(exc: BaseException) -> FailureKind:
    if isinstance(exc, TokenError):
        return FailureKind.TRANSIENT_TOKEN if exc.transient else FailureKind.TOKEN
    if isinstance(exc, GraphAuthError):
        return FailureKind.UNAUTHORIZED if exc.status == 401 else FailureKind.FORBIDDEN
    return FailureKind.OTHER


def failure_reason(exc: BaseException) -> str | None:
    """Short label for m365_tenant_auth_error{reason}; None for non-auth failures."""
    if isinstance(exc, TokenError):
        return exc.reason
    if isinstance(exc, GraphAuthError) and exc.status == 401:
        return "unauthorized"
    return None


def next_delay(interval: float, consecutive_failures: int) -> float:
    if consecutive_failures <= 0:
        return interval
    return float(min(interval, RETRY_CAP_S, RETRY_BASE_S * 2 ** (consecutive_failures - 1)))


@dataclass
class RunResult:
    tenant: TenantConfig
    collector: str
    ok: bool
    duration_s: float
    samples: list[MetricSample] = field(default_factory=list)
    error: str | None = None
    # Missing permission groups when the collector was skipped (not run).
    skipped: tuple[Requirement, ...] = ()


class Scheduler:
    def __init__(
        self,
        state: AppState,
        graph: GraphClient,
        settings: Settings,
        *,
        rng: Callable[[], float] = random.random,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._state = state
        self._graph = graph
        self._settings = settings
        self._rng = rng
        self._sleep = sleep
        self._tasks: dict[Key, asyncio.Task[None]] = {}
        self._memory: dict[Key, dict[str, Any]] = {}
        self._skip_warned: dict[Key, tuple[Requirement, ...]] = {}
        state.intervals = {name: self.interval(name) for name in state.collectors}

    # ------------------------------------------------------------------ config

    def interval(self, collector: str) -> float:
        override = self._settings.intervals.get(collector)
        return (
            override if override is not None else self._state.collectors[collector].default_interval
        )

    def options(self, tenant: TenantConfig) -> TenantOptions:
        return TenantOptions(
            report_period=report_period(self._settings, tenant),
            onedrive_source=onedrive_source(self._settings, tenant),
        )

    def desired_keys(self) -> set[Key]:
        names = list(self._state.collectors)
        return {
            (t.id, c) for t in self._state.active_tenants() for c in t.enabled_collectors(names)
        }

    @property
    def task_keys(self) -> set[Key]:
        return set(self._tasks)

    async def apply_tenants(self, tenants: list[TenantConfig]) -> None:
        """Make running tasks match ``tenants``; drop cache/health for anything removed.

        Also hands every active tenant's effective auth to the token provider, which closes
        credentials whose auth block changed (the secret is re-resolved on next use).
        """
        auths = validate_auth(self._settings, tenants)  # raises ConfigError before any change
        await self._graph.tokens.configure(auths)
        self._state.auth = auths
        old = self._state.tenants
        self._state.tenants = {t.id: t for t in tenants}
        new = self._state.tenants

        for tid, before in old.items():
            after = new.get(tid)
            if after is None or not after.enabled:
                self._state.cache.drop_tenant(tid)
                self._state.health.drop_tenant(tid)
            if (
                after is None
                or not after.enabled
                or (after.name, after.customer) != (before.name, before.customer)
            ):
                forget_graph_counters(before.name, before.customer)

        desired = self.desired_keys()
        for key in self.task_keys - desired:
            task = self._tasks.pop(key)
            task.cancel()
            self._memory.pop(key, None)
            self._skip_warned.pop(key, None)
            self._state.cache.drop(*key)
            self._state.health.drop(*key)
        for key in sorted(desired - self.task_keys):
            self._tasks[key] = asyncio.create_task(self._loop(key), name=f"{key[0]}:{key[1]}")
        log.info("schedule_updated", tasks=len(self._tasks), tenants=len(new))

    # ------------------------------------------------------------------ running

    async def run_once(
        self, tenant: TenantConfig, collector_name: str, *, write: bool = True
    ) -> RunResult:
        collector = self._state.collectors[collector_name]
        key = (tenant.id, collector_name)
        clog = log.bind(tenant=tenant.name, customer=tenant.customer, collector=collector_name)
        options = self.options(tenant)
        start = time.perf_counter()
        try:
            # Gate on the roles in the tenant's current token (a new token after new consent
            # is picked up here, without a restart). Unknown roles (not a JWT): no gating.
            roles = await self._graph.tenant_roles(tenant.id)
            if write and roles is not None:
                self._state.health.tenant_roles[tenant.id] = roles
            missing = collector.missing_permissions(options, roles)
            if missing:
                return self._skip(tenant, collector_name, missing, start, clog, write=write)
            ctx = CollectorContext(
                tenant=tenant,
                graph=self._graph.for_tenant(tenant, collector_name),
                log=clog,
                state=self._memory.setdefault(key, {}),
                roles=roles,
                options=options,
                peer_samples=lambda name: self._peer_samples(tenant.id, name),
            )
            samples = await collector.collect(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            duration = time.perf_counter() - start
            kind = classify_failure(exc)
            if kind is FailureKind.OTHER:
                clog.exception("collector_failed", error=str(exc), duration_s=round(duration, 3))
            else:
                clog.error(
                    "collector_failed",
                    failure=kind.value,
                    error=str(exc),
                    duration_s=round(duration, 3),
                )
            if write:
                self._state.health.record_failure(
                    tenant.id, collector_name, duration, str(exc), kind, failure_reason(exc)
                )
            return RunResult(tenant, collector_name, False, duration, error=str(exc))

        duration = time.perf_counter() - start
        if self._skip_warned.pop(key, None):
            clog.info("collector_enabled", hint="required permissions are now granted")
        if write:
            self._state.cache.replace(
                Snapshot(tenant.id, collector_name, time.time(), tuple(samples))
            )
            self._state.health.record_success(tenant.id, collector_name, duration, len(samples))
        clog.info("collector_ok", samples=len(samples), duration_s=round(duration, 3))
        return RunResult(tenant, collector_name, True, duration, samples=samples)

    def _peer_samples(self, tenant_id: str, collector: str) -> tuple[MetricSample, ...]:
        snap = self._state.cache.get(tenant_id, collector)
        return snap.samples if snap is not None else ()

    def _skip(
        self,
        tenant: TenantConfig,
        collector: str,
        missing: tuple[Requirement, ...],
        start: float,
        clog: Any,
        *,
        write: bool,
    ) -> RunResult:
        key = (tenant.id, collector)
        if self._skip_warned.get(key) != missing:  # one warning per tenant and collector
            self._skip_warned[key] = missing
            clog.warning(
                "collector_skipped",
                reason="missing_permission",
                missing=[" or ".join(g) for g in missing],
                hint=f"grant the application permission(s), then: m365c tenant verify "
                f"{tenant.customer}",
            )
        if write:
            self._state.cache.drop(*key)  # no stale data from before the permission went
            self._state.health.record_skipped(tenant.id, collector, missing)
        return RunResult(tenant, collector, True, time.perf_counter() - start, skipped=missing)

    async def _loop(self, key: Key) -> None:
        tenant_id, collector = key
        interval = self.interval(collector)
        await self._sleep(self._rng() * min(interval, MAX_INITIAL_JITTER_S))
        failures = 0
        while True:
            tenant = self._state.tenants.get(tenant_id)
            if tenant is None:
                return
            skipped = False
            try:
                result = await self.run_once(tenant, collector)
                ok, skipped = result.ok, bool(result.skipped)
            except asyncio.CancelledError:
                raise
            except Exception:  # run_once handles collector errors; this is belt and braces
                log.exception("scheduler_loop_error", tenant=tenant.name, collector=collector)
                ok = False
            failures = 0 if ok else failures + 1
            # A skipped collector re-checks the token's roles at least hourly, so newly
            # granted consent starts it without waiting a full (e.g. 24 h) interval.
            delay = min(interval, SKIPPED_RECHECK_S) if skipped else next_delay(interval, failures)
            await self._sleep(delay)

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
