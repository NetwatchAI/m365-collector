"""Thin async Microsoft Graph client: JSON, paging, report CSVs, retries, throttling."""

from __future__ import annotations

import asyncio
import csv
import io
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import structlog

from m365c import __version__
from m365c.auth import TokenProvider
from m365c.config import HttpConfig, TenantConfig
from m365c.health import count_request, count_throttled

log = structlog.get_logger(__name__)

GRAPH_V1 = "https://graph.microsoft.com/v1.0"
GRAPH_BETA = "https://graph.microsoft.com/beta"

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
THROTTLE_STATUSES = frozenset({429, 503})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 60.0
RETRY_AFTER_CAP_S = 600.0
MAX_PAGES = 10_000


class GraphError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"Graph {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class GraphAuthError(GraphError):
    """401/403: consent revoked, permission missing or licence-gated feature."""


class GraphNotFoundError(GraphError):
    """404."""


def _error_from(resp: httpx.Response, *, auth: bool = True) -> GraphError:
    code, message = "unknown", resp.reason_phrase
    try:
        body = resp.json()
        err = body.get("error", {}) if isinstance(body, dict) else {}
        code = str(err.get("code") or code)
        message = str(err.get("message") or message)
    except ValueError:
        pass
    status = resp.status_code
    if not auth:
        # A pre-authenticated download URL that expired is not a tenant auth problem.
        return GraphError(status, code, message)
    if status in (401, 403):
        return GraphAuthError(status, code, message)
    if status == 404:
        return GraphNotFoundError(status, code, message)
    return GraphError(status, code, message)


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - (now or datetime.now(UTC))).total_seconds()
    return max(0.0, min(seconds, RETRY_AFTER_CAP_S))


def backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter: base*2^n plus up to the same again, capped."""
    step = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2**attempt)
    return float(min(BACKOFF_CAP_S, step + random.uniform(0, step)))


def parse_csv(content: bytes) -> list[dict[str, str]]:
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    return [dict(row) for row in reader]


class GraphClient:
    """Shared HTTP client. Use :meth:`for_tenant` to get a tenant-scoped view."""

    def __init__(
        self,
        tokens: TokenProvider,
        http: HttpConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._tokens = tokens
        self._max_retries = http.max_retries
        self._per_tenant = http.per_tenant_concurrency
        self._global_sem = asyncio.Semaphore(http.max_concurrency)
        self._tenant_sems: dict[str, asyncio.Semaphore] = {}
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            timeout=http.timeout_s,
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": f"m365c/{__version__}"},
        )

    @property
    def tokens(self) -> TokenProvider:
        return self._tokens

    async def tenant_roles(self, tenant_id: str) -> frozenset[str] | None:
        """Graph application roles in the tenant's token (fetching one if needed); None if
        the token is not a JWT. Raises TokenError like any Graph call would."""
        await self._tokens.get_token(tenant_id)
        return self._tokens.granted_roles(tenant_id)

    def for_tenant(self, tenant: TenantConfig, collector: str | None = None) -> TenantGraph:
        return TenantGraph(self, tenant, collector)

    def _tenant_sem(self, tenant_id: str) -> asyncio.Semaphore:
        sem = self._tenant_sems.get(tenant_id)
        if sem is None:
            sem = self._tenant_sems[tenant_id] = asyncio.Semaphore(self._per_tenant)
        return sem

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def per_tenant_concurrency(self) -> int:
        return self._per_tenant

    async def send(
        self,
        tenant: TenantConfig,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        auth: bool = True,
        collector: str | None = None,
    ) -> httpx.Response:
        """Send with retries. Returns any non-retryable response; raises on 4xx/5xx.

        ``auth=False`` is used for pre-authenticated report downloads: no Authorization
        header is sent, and the URL path (which *is* the download credential) is never
        logged.
        """
        ctx = {"tenant": tenant.name, "customer": tenant.customer, "collector": collector}
        target = httpx.URL(url)
        log_path = target.path if auth else "<report-download>"
        attempt = 0
        while True:
            delay: float
            resp: httpx.Response | None = None
            req_headers = dict(headers or {})
            if auth:
                # Outside the semaphores: a slow token endpoint must not hold Graph slots.
                req_headers["Authorization"] = f"Bearer {await self._tokens.get_token(tenant.id)}"
            # Per-tenant slot first: a tenant's backlog then waits on its own semaphore
            # without holding global slots, so it cannot starve other tenants.
            async with self._tenant_sem(tenant.id), self._global_sem:
                start = time.perf_counter()
                try:
                    resp = await self._client.request(
                        method, url, params=params, headers=req_headers
                    )
                except httpx.TransportError as exc:
                    count_request(tenant.name, tenant.customer, "error")
                    log.debug(
                        "graph_request",
                        **ctx,
                        method=method,
                        host=target.host,
                        path=log_path,
                        status="error",
                        error=type(exc).__name__,
                    )
                    if attempt >= self._max_retries:
                        raise GraphError(0, "transport_error", type(exc).__name__) from exc
                    delay = backoff_delay(attempt)
            if resp is not None:
                duration_ms = round((time.perf_counter() - start) * 1000, 1)
                status = resp.status_code
                count_request(tenant.name, tenant.customer, str(status))
                log.debug(
                    "graph_request",
                    **ctx,
                    method=method,
                    host=target.host,
                    path=log_path,
                    status=status,
                    duration_ms=duration_ms,
                )
                if status not in RETRY_STATUSES:
                    if status >= 400:
                        raise _error_from(resp, auth=auth)
                    return resp
                if status in THROTTLE_STATUSES:
                    count_throttled(tenant.name, tenant.customer)
                if attempt >= self._max_retries:
                    raise _error_from(resp, auth=auth)
                retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                delay = retry_after if retry_after is not None else backoff_delay(attempt)
                log.info(
                    "graph_retry",
                    **ctx,
                    path=log_path,
                    status=status,
                    attempt=attempt + 1,
                    delay_s=round(delay, 2),
                )
            attempt += 1
            await self._sleep(delay)


class TenantGraph:
    def __init__(
        self, client: GraphClient, tenant: TenantConfig, collector: str | None = None
    ) -> None:
        self._client = client
        self.tenant = tenant
        self.collector = collector

    @property
    def concurrency(self) -> int:
        """How many requests this tenant may have in flight at once."""
        return self._client.per_tenant_concurrency

    @staticmethod
    def _url(path: str, beta: bool) -> str:
        if path.startswith("https://"):
            return path
        return f"{GRAPH_BETA if beta else GRAPH_V1}/{path.lstrip('/')}"

    async def get_json(
        self,
        path: str,
        params: Mapping[str, str] | None = None,
        *,
        beta: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        resp = await self._client.send(
            self.tenant,
            "GET",
            self._url(path, beta),
            params=params,
            headers=headers,
            collector=self.collector,
        )
        data = resp.json()
        if not isinstance(data, dict):
            raise GraphError(resp.status_code, "invalid_response", "expected a JSON object")
        return data

    async def get_all(
        self,
        path: str,
        params: Mapping[str, str] | None = None,
        *,
        beta: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """GET a collection, following ``@odata.nextLink`` until exhausted."""
        items: list[dict[str, Any]] = []
        url: str | None = self._url(path, beta)
        page_params = params
        for _ in range(MAX_PAGES):
            if url is None:
                return items
            page = await self.get_json(url, page_params, headers=headers)
            items.extend(page.get("value", []))
            url = page.get("@odata.nextLink")
            page_params = None  # nextLink already carries the query
        raise GraphError(0, "too_many_pages", f"more than {MAX_PAGES} pages for {path}")

    async def get_report_csv(self, path: str, *, beta: bool = False) -> list[dict[str, str]]:
        """Fetch a usage report. Graph answers 302 to a pre-authenticated download URL,
        which is fetched without our Authorization header."""
        resp = await self._client.send(
            self.tenant, "GET", self._url(path, beta), collector=self.collector
        )
        if resp.status_code in REDIRECT_STATUSES:
            location = resp.headers.get("Location")
            if not location:
                raise GraphError(resp.status_code, "invalid_redirect", "redirect without Location")
            target = str(resp.request.url.join(location))
            resp = await self._client.send(
                self.tenant, "GET", target, auth=False, collector=self.collector
            )
        return parse_csv(resp.content)
