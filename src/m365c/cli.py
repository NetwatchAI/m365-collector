"""``m365c`` command line."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from m365c.auth import (
    CertificateError,
    CertificateTokenProvider,
    TokenError,
    TokenProvider,
    decode_jwt_claims,
    load_app_certificate,
)
from m365c.cache import SnapshotCache
from m365c.collectors import all_collectors, permissions
from m365c.config import (
    TENANTS_FILE,
    ConfigError,
    Settings,
    TenantConfig,
    load_all,
    load_settings,
    load_tenants,
    write_tenants,
)
from m365c.graph import GraphAuthError, GraphClient, GraphError
from m365c.logging import configure_logging
from m365c.scheduler import RunResult, Scheduler
from m365c.secrets import SecretError
from m365c.state import AppState

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Microsoft 365 collector")
tenant_app = typer.Typer(no_args_is_help=True, help="Manage client tenants")
app.add_typer(tenant_app, name="tenant")

err = Console(stderr=True)


class _Out:
    """stdout console created per use, so piped/captured output isn't cut to 80 columns."""

    def __getattr__(self, name: str) -> Any:
        width = None if sys.stdout.isatty() else 240
        return getattr(Console(soft_wrap=True, width=width), name)


out = _Out()

OK, FAIL, SKIP = "✅", "❌", "➖"
CONSENT_URL = "https://login.microsoftonline.com/{tenant}/adminconsent?client_id={client_id}"
REPORT_COLLECTORS = {"mailbox", "mailtraffic"}

FixturesOpt = Annotated[
    Path | None,
    typer.Option(
        "--fixtures",
        hidden=True,
        help="Serve Graph from fixture files instead of the network (development aid).",
    ),
]


@dataclass
class Ctx:
    config_dir: Path
    log_level: str


def _ctx(ctx: typer.Context) -> Ctx:
    obj = ctx.obj
    assert isinstance(obj, Ctx)
    return obj


def _fail(message: str, code: int = 2) -> typer.Exit:
    err.print(f"[red]error:[/red] {message}", highlight=False)
    return typer.Exit(code)


@app.callback()
def main(
    ctx: typer.Context,
    config_dir: Annotated[
        Path, typer.Option("--config-dir", "-c", envvar="M365C_CONFIG_DIR", help="Config directory")
    ] = Path("config"),
    log_level: Annotated[str, typer.Option(envvar="M365C_LOG_LEVEL", help="Log level")] = "INFO",
) -> None:
    # Windows consoles default to cp1252, which cannot print the status symbols.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper) and (stream.encoding or "").lower() != "utf-8":
            stream.reconfigure(encoding="utf-8", errors="replace")
    ctx.obj = Ctx(config_dir=config_dir, log_level=log_level)


# --------------------------------------------------------------------------- helpers


def _token_provider(settings: Settings, fixtures: Path | None) -> TokenProvider:
    if fixtures is not None:
        from m365c.fixtures import fixture_token_provider

        return fixture_token_provider(permissions())
    cert, password, _ = load_app_certificate(settings.secrets.cert, settings.secrets.cert_password)
    return CertificateTokenProvider(settings.app.client_id, cert, password)


def _graph(settings: Settings, tokens: TokenProvider, fixtures: Path | None) -> GraphClient:
    transport = None
    if fixtures is not None:
        from m365c.fixtures import fixture_transport

        transport = fixture_transport(fixtures)
    return GraphClient(tokens, settings.http, transport=transport)


def _match(tenants: list[TenantConfig], selector: str) -> list[TenantConfig]:
    key = selector.strip().lower()
    return [t for t in tenants if key in (t.id.lower(), t.name.lower(), t.customer.lower())]


# --------------------------------------------------------------------------- serve


@app.command()
def serve(ctx: typer.Context, fixtures: FixturesOpt = None) -> None:
    """Run the long-running collector service."""
    from m365c.main import serve as run_service

    c = _ctx(ctx)
    configure_logging(c.log_level, json=True)
    kwargs: dict[str, Any] = {}
    if fixtures is not None:  # offline demo / end-to-end test of the deploy stack
        from m365c.fixtures import fixture_token_provider, fixture_transport

        kwargs = {
            "tokens": fixture_token_provider(permissions()),
            "transport": fixture_transport(fixtures),
        }
    try:
        asyncio.run(run_service(c.config_dir, **kwargs))
    except (ConfigError, SecretError, CertificateError) as exc:
        raise _fail(str(exc)) from None
    except KeyboardInterrupt:
        pass


# --------------------------------------------------------------------------- permissions


@app.command("permissions")
def permissions_cmd() -> None:
    """Print the Graph application permissions the app registration needs."""
    for perm in permissions():
        out.print(perm, highlight=False)


# --------------------------------------------------------------------------- tenant


@tenant_app.command("list")
def tenant_list(ctx: typer.Context) -> None:
    """List configured tenants."""
    c = _ctx(ctx)
    try:
        tenants = load_tenants(c.config_dir, list(all_collectors()))
    except ConfigError as exc:
        raise _fail(str(exc)) from None
    table = Table("customer", "tenant", "tenant id", "collectors", "enabled")
    for t in tenants:
        collectors = t.collectors if isinstance(t.collectors, str) else ", ".join(t.collectors)
        table.add_row(t.customer, t.name, t.id, collectors, OK if t.enabled else FAIL)
    out.print(table)


@tenant_app.command("add")
def tenant_add(
    ctx: typer.Context,
    tenant_id: Annotated[str, typer.Option("--tenant-id", help="Client tenant GUID")],
    name: Annotated[
        str, typer.Option("--name", help="Tenant domain, e.g. contoso.onmicrosoft.com")
    ],
    customer: Annotated[str, typer.Option("--customer", help="Customer slug")],
) -> None:
    """Append a tenant to tenants.yaml and print the admin-consent URL."""
    c = _ctx(ctx)
    try:
        settings = load_settings(c.config_dir)
        new = TenantConfig(id=tenant_id, name=name, customer=customer)
    except ConfigError as exc:
        raise _fail(str(exc)) from None
    except ValidationError as exc:
        raise _fail("; ".join(f"{e['loc'][0]}: {e['msg']}" for e in exc.errors())) from None

    known = list(all_collectors())
    existing: list[TenantConfig] = []
    if (c.config_dir / TENANTS_FILE).exists():
        try:
            existing = load_tenants(c.config_dir, known)
        except ConfigError as exc:
            raise _fail(str(exc)) from None
    for t in existing:
        if t.id == new.id or t.name == new.name:
            raise _fail(f"tenant already configured: {t.name} ({t.id}), customer {t.customer}")
    try:
        path = write_tenants(c.config_dir, [*existing, new], known)
    except ConfigError as exc:
        raise _fail(str(exc)) from None

    out.print(f"Added {new.name} (customer {new.customer}) to {path}", highlight=False)
    out.print("Send this admin-consent URL to a Global Administrator of the client tenant:")
    out.print(CONSENT_URL.format(tenant=new.id, client_id=settings.app.client_id), highlight=False)
    out.print(f"Then run: m365c tenant verify {new.customer}", highlight=False)


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


async def _verify_tenant(
    tenant: TenantConfig, tokens: TokenProvider, graph: GraphClient
) -> list[Check]:
    enabled = tenant.enabled_collectors(list(all_collectors()))
    checks: list[Check] = []
    try:
        token = await tokens.get_token(tenant.id)
    except TokenError as exc:
        checks.append(Check("token", FAIL, str(exc)))
        skip = "skipped: no token"
        return [*checks, *(Check(n, SKIP, skip) for n in ("permissions", "reports", "signin"))]
    checks.append(Check("token", OK, "token acquired"))

    try:
        roles = set(decode_jwt_claims(token).get("roles") or [])
    except ValueError:
        roles = set()
    missing = sorted(set(permissions(enabled)) - roles)
    checks.append(
        Check("permissions", FAIL, "missing: " + ", ".join(missing))
        if missing
        else Check("permissions", OK, f"{len(permissions(enabled))} required roles granted")
    )

    tg = graph.for_tenant(tenant)
    if REPORT_COLLECTORS & set(enabled):
        try:
            concealed = bool(
                (await tg.get_json("/admin/reportSettings")).get("displayConcealedNames")
            )
            checks.append(
                Check(
                    "reports",
                    FAIL if concealed else OK,
                    "names concealed: turn off 'Display concealed user, group, and site names' "
                    "in admin center > Settings > Org settings > Reports"
                    if concealed
                    else "report names not concealed",
                )
            )
        except GraphError as exc:
            checks.append(Check("reports", FAIL, f"{exc.status} {exc.code}"))
    else:
        checks.append(Check("reports", SKIP, "no report collectors enabled"))

    if "users" in enabled:
        try:
            await tg.get_json("/users", {"$select": "id,signInActivity", "$top": "1"})
            checks.append(Check("signin", OK, "signInActivity available"))
        except GraphAuthError as exc:
            checks.append(
                Check("signin", FAIL, f"{exc.code}: needs AuditLog.Read.All and Entra ID P1")
            )
        except GraphError as exc:
            checks.append(Check("signin", FAIL, f"{exc.status} {exc.code}"))
    else:
        checks.append(Check("signin", SKIP, "users collector not enabled"))
    return checks


@tenant_app.command("verify")
def tenant_verify(
    ctx: typer.Context,
    selector: Annotated[str, typer.Argument(help="Customer slug, tenant name or tenant id")],
    fixtures: FixturesOpt = None,
) -> None:
    """Check token, granted permissions, report concealment and signInActivity."""
    c = _ctx(ctx)
    configure_logging(c.log_level if c.log_level.upper() != "INFO" else "WARNING", json=False)
    try:
        cfg = load_all(c.config_dir, list(all_collectors()))
        tokens = _token_provider(cfg.settings, fixtures)
    except (ConfigError, SecretError, CertificateError) as exc:
        raise _fail(str(exc)) from None
    tenants = _match(cfg.tenants, selector)
    if not tenants:
        raise _fail(f"no tenant matches {selector!r}")

    async def run() -> list[tuple[TenantConfig, list[Check]]]:
        graph = _graph(cfg.settings, tokens, fixtures)
        try:
            return [(t, await _verify_tenant(t, tokens, graph)) for t in tenants]
        finally:
            await graph.aclose()
            await tokens.aclose()

    results = asyncio.run(run())
    table = Table("tenant", "check", "", "detail")
    failed = False
    for tenant, checks in results:
        for check in checks:
            failed |= check.status == FAIL
            table.add_row(tenant.name, check.name, check.status, check.detail)
    out.print(table)
    if failed:
        raise typer.Exit(1)


# --------------------------------------------------------------------------- run


@app.command()
def run(
    ctx: typer.Context,
    tenant: Annotated[str, typer.Option("--tenant", help="Customer slug, tenant name or id")],
    collector: Annotated[
        list[str] | None, typer.Option("--collector", help="Collector name (repeatable)")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Do not write cache/persistence")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of a table")] = False,
    fixtures: FixturesOpt = None,
) -> None:
    """Run collectors once for a tenant and print the samples."""
    c = _ctx(ctx)
    configure_logging(c.log_level if c.log_level.upper() != "INFO" else "WARNING", json=False)
    collectors = all_collectors()
    try:
        cfg = load_all(c.config_dir, list(collectors))
        tokens = _token_provider(cfg.settings, fixtures)
    except (ConfigError, SecretError, CertificateError) as exc:
        raise _fail(str(exc)) from None

    unknown = sorted(set(collector or []) - set(collectors))
    if unknown:
        raise _fail(f"unknown collectors {unknown}; known: {sorted(collectors)}")
    tenants = _match(cfg.tenants, tenant)
    if not tenants:
        raise _fail(f"no tenant matches {tenant!r}")

    cache = SnapshotCache(None if dry_run else cfg.settings.cache.persist_dir)
    state = AppState(collectors=collectors, cache=cache, thresholds=cfg.thresholds)
    state.tenants = {t.id: t for t in tenants}

    async def go() -> list[RunResult]:
        graph = _graph(cfg.settings, tokens, fixtures)
        scheduler = Scheduler(state, graph, cfg.settings)
        try:
            results = []
            for t in tenants:
                for name in t.enabled_collectors(list(collectors)):
                    if collector and name not in collector:
                        continue
                    results.append(await scheduler.run_once(t, name, write=not dry_run))
            return results
        finally:
            await graph.aclose()
            await tokens.aclose()

    results = asyncio.run(go())
    if as_json:
        payload: list[dict[str, Any]] = [
            {
                "tenant": r.tenant.name,
                "customer": r.tenant.customer,
                "collector": r.collector,
                "ok": r.ok,
                "error": r.error,
                "duration_s": round(r.duration_s, 3),
                "samples": [s.to_json() for s in r.samples],
            }
            for r in results
        ]
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        table = Table("tenant", "collector", "metric", "labels", "value")
        for r in results:
            if not r.ok:
                table.add_row(r.tenant.name, r.collector, f"{FAIL} {r.error}", "", "")
            for s in r.samples:
                labels = ",".join(f'{k}="{v}"' for k, v in s.labels.items())
                table.add_row(r.tenant.name, r.collector, s.name, labels, f"{s.value:g}")
        out.print(table)
    if not all(r.ok for r in results):
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
