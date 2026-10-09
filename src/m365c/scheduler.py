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
from m365c.collectors.base import CollectorContext, MetricSample
from m365c.config import Settings, TenantConfig
from m365c.graph import GraphAuthError, GraphClient
from m365c.health import FailureKind, forget_graph_counters
from m365c.state import AppState

log = structlog.get_logger(__name__)

MAX_INITIAL_JITTER_S = 300.0
# After a failed run, retry sooner than the full interval: 5m, 10m, 20m, ... capped at 1h
# (and never later than the normal interval).
RETRY_BASE_S = 300.0
RETRY_CAP_S = 3600.0

Key = tuple[str, str]


def classify_failure(exc: BaseException) -> FailureKind:
    if isinstance(exc, TokenError):
        return FailureKind.TRANSIENT_TOKEN if exc.transient else FailureKind.TOKEN
    if isinstance(exc, GraphAuthError):
        return FailureKind.UNAUTHORIZED if exc.status == 401 else FailureKind.FORBIDDEN
    return FailureKind.OTHER


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
        state.intervals = {name: self.interval(name) for name in state.collectors}

    # ------------------------------------------------------------------ config

    def interval(self, collector: str) -> float:
        override = self._settings.intervals.get(collector)
        return (
            override if override is not None else self._state.collectors[collector].default_interval
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
        """Make running tasks match ``tenants``; drop cache/health for anything removed."""
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
        ctx = CollectorContext(
            tenant=tenant,
            graph=self._graph.for_tenant(tenant, collector_name),
            log=clog,
            state=self._memory.setdefault(key, {}),
        )
        start = time.perf_counter()
        try:
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
                    tenant.id, collector_name, duration, str(exc), kind
                )
            return RunResult(tenant, collector_name, False, duration, error=str(exc))

        duration = time.perf_counter() - start
        if write:
            self._state.cache.replace(
                Snapshot(tenant.id, collector_name, time.time(), tuple(samples))
            )
            self._state.health.record_success(tenant.id, collector_name, duration, len(samples))
        clog.info("collector_ok", samples=len(samples), duration_s=round(duration, 3))
        return RunResult(tenant, collector_name, True, duration, samples=samples)

    async def _loop(self, key: Key) -> None:
        tenant_id, collector = key
        interval = self.interval(collector)
        await self._sleep(self._rng() * min(interval, MAX_INITIAL_JITTER_S))
        failures = 0
        while True:
            tenant = self._state.tenants.get(tenant_id)
            if tenant is None:
                return
            try:
                ok = (await self.run_once(tenant, collector)).ok
            except asyncio.CancelledError:
                raise
            except Exception:  # run_once handles collector errors; this is belt and braces
                log.exception("scheduler_loop_error", tenant=tenant.name, collector=collector)
                ok = False
            failures = 0 if ok else failures + 1
            await self._sleep(next_delay(interval, failures))

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
