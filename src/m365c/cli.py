"""``m365c`` command line."""

from __future__ import annotations

import asyncio
import enum
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
    TenantTokenProvider,
    TokenError,
    TokenProvider,
    load_app_certificate,
)
from m365c.cache import SnapshotCache
from m365c.collectors import (
    PARITY_COLLECTORS,
    PARITY_PERMISSIONS,
    TenantOptions,
    all_collectors,
    format_requirement,
    permissions,
)
from m365c.collectors.onedrive import effective_source
from m365c.collectors.reports import REPORT_SETTINGS
from m365c.collectors.users import SIGNIN_READ
from m365c.config import (
    TENANTS_FILE,
    ConfigError,
    Settings,
    TenantAuth,
    TenantConfig,
    load_all,
    load_settings,
    load_tenants,
    onedrive_source,
    report_period,
    resolve_auth,
    uses_default_certificate,
    validate_auth,
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
REPORT_COLLECTORS = {"mailbox", "mailtraffic", "onedrive"}

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


def _token_provider(
    settings: Settings, tenants: list[TenantConfig], fixtures: Path | None
) -> TokenProvider:
    if fixtures is not None:
        from m365c.fixtures import fixture_token_provider

        return fixture_token_provider(permissions())
    if settings.secrets.cert is not None and uses_default_certificate(settings, tenants):
        # Fail fast on a broken shared certificate, as the service does at startup.
        load_app_certificate(settings.secrets.cert, settings.secrets.cert_password)
    return TenantTokenProvider(validate_auth(settings, tenants))


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
def permissions_cmd(
    parity: Annotated[
        bool, typer.Option("--parity", help="Only the minimal set for Zabbix template parity")
    ] = False,
    flat: Annotated[
        bool, typer.Option("--all", help="Only a flat list of every role any collector uses")
    ] = False,
) -> None:
    """Graph application permissions: required and optional per collector, and the minimal
    set for template parity (what the legacy client app registrations already have)."""
    if flat:
        for perm in permissions():
            out.print(perm, highlight=False)
        return
    parity_lines = [format_requirement(g) for g in PARITY_PERMISSIONS]
    if parity:
        for line in parity_lines:
            out.print(line, highlight=False)
        return
    table = Table("collector", "required (every line; 'or' = any one)", "optional: adds", "parity")
    for name, c in all_collectors().items():
        table.add_row(
            name,
            "\n".join(format_requirement(g) for g in c.required_permissions),
            "\n".join(f"{format_requirement(g)}: {what}" for g, what in c.optional_permissions),
            OK if name in PARITY_COLLECTORS else SKIP,
        )
    out.print(table)
    out.print("Minimal set for Zabbix template parity (license, users, mailbox, mailtraffic,")
    out.print("onedrive with source: report):")
    for line in parity_lines:
        out.print(f"  {line}", highlight=False)


# --------------------------------------------------------------------------- tenant


@tenant_app.command("list")
def tenant_list(ctx: typer.Context) -> None:
    """List configured tenants."""
    c = _ctx(ctx)
    try:
        tenants = load_tenants(c.config_dir, list(all_collectors()))
    except ConfigError as exc:
        raise _fail(str(exc)) from None
    table = Table("customer", "tenant", "tenant id", "auth", "collectors", "enabled")
    for t in tenants:
        collectors = t.collectors if isinstance(t.collectors, str) else ", ".join(t.collectors)
        auth = t.auth.method if t.auth is not None else "default"
        table.add_row(t.customer, t.name, t.id, auth, collectors, OK if t.enabled else FAIL)
    out.print(table)


class AuthChoice(enum.StrEnum):
    client_secret = "client-secret"
    certificate = "certificate"


def _tenant_auth(
    auth: AuthChoice | None,
    client_id: str | None,
    client_secret_ref: str | None,
    client_secret_expires: str | None,
    cert_ref: str | None,
    cert_password_ref: str | None,
) -> TenantAuth | None:
    """Build the tenant's auth block from the options; references only, never values."""
    secret_opts = client_secret_ref or client_secret_expires
    cert_opts = cert_ref or cert_password_ref
    if auth is None:
        if client_id or secret_opts or cert_opts:
            raise _fail("--client-id/--client-secret-ref/--cert-ref need --auth")
        return None  # use the settings.yaml default
    if auth is AuthChoice.client_secret:
        if not client_id or not client_secret_ref:
            raise _fail("--auth client-secret needs --client-id and --client-secret-ref")
        if cert_opts:
            raise _fail("--cert-ref/--cert-password-ref are only valid with --auth certificate")
    elif secret_opts:
        raise _fail("--client-secret-* options are only valid with --auth client-secret")
    try:
        return TenantAuth(
            method="client_secret" if auth is AuthChoice.client_secret else "certificate",
            client_id=client_id,
            client_secret=client_secret_ref,
            client_secret_expires=client_secret_expires,
            cert=cert_ref,
            cert_password=cert_password_ref,
        )
    except ValidationError as exc:
        # hide_input_in_errors: messages never contain the (possibly literal) value.
        raise _fail(
            "; ".join(f"{'.'.join(map(str, e['loc'])) or 'auth'}: {e['msg']}" for e in exc.errors())
        ) from None


@tenant_app.command("add")
def tenant_add(
    ctx: typer.Context,
    tenant_id: Annotated[str, typer.Option("--tenant-id", help="Client tenant GUID")],
    name: Annotated[
        str, typer.Option("--name", help="Tenant domain, e.g. contoso.onmicrosoft.com")
    ],
    customer: Annotated[str, typer.Option("--customer", help="Customer slug")],
    auth: Annotated[
        AuthChoice | None,
        typer.Option("--auth", help="Tenant's own auth; default: settings.yaml app:"),
    ] = None,
    client_id: Annotated[
        str | None, typer.Option("--client-id", help="Application (client) ID of the app")
    ] = None,
    client_secret_ref: Annotated[
        str | None,
        typer.Option(
            "--client-secret-ref",
            help="Secret REFERENCE (file:/path, env:VAR, vault:..., keyvault:...); "
            "literal secrets are refused",
        ),
    ] = None,
    client_secret_expires: Annotated[
        str | None,
        typer.Option("--client-secret-expires", help="Secret expiry date, YYYY-MM-DD"),
    ] = None,
    cert_ref: Annotated[
        str | None, typer.Option("--cert-ref", help="Certificate reference (default: secrets.cert)")
    ] = None,
    cert_password_ref: Annotated[
        str | None, typer.Option("--cert-password-ref", help="Certificate password reference")
    ] = None,
) -> None:
    """Append a tenant to tenants.yaml and print the admin-consent URL."""
    c = _ctx(ctx)
    tenant_auth = _tenant_auth(
        auth, client_id, client_secret_ref, client_secret_expires, cert_ref, cert_password_ref
    )
    try:
        settings = load_settings(c.config_dir)
        new = TenantConfig(id=tenant_id, name=name, customer=customer, auth=tenant_auth)
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
        resolved = resolve_auth(settings, new)
    except ConfigError as exc:
        raise _fail(str(exc)) from None
    try:
        path = write_tenants(c.config_dir, [*existing, new], known)
    except ConfigError as exc:
        raise _fail(str(exc)) from None

    out.print(
        f"Added {new.name} (customer {new.customer}, auth {resolved.method}) to {path}",
        highlight=False,
    )
    if resolved.method == "client_secret":
        out.print(
            "The client's app registration needs admin consent for its Graph permissions "
            "(already granted for apps used by the Zabbix templates). If not, send:"
        )
    else:
        out.print("Send this admin-consent URL to a Global Administrator of the client tenant:")
    out.print(CONSENT_URL.format(tenant=new.id, client_id=resolved.client_id), highlight=False)
    out.print(f"Then run: m365c tenant verify {new.customer}", highlight=False)


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


async def _verify_tenant(
    tenant: TenantConfig, tokens: TokenProvider, graph: GraphClient, settings: Settings
) -> list[Check]:
    collectors = all_collectors()
    configured = tenant.enabled_collectors(list(collectors))
    explicit = tenant.collectors != "all"
    checks: list[Check] = []
    try:
        auth = resolve_auth(settings, tenant)
        checks.append(Check("auth", OK, f"{auth.method}, client {auth.client_id}"))
    except ConfigError as exc:
        return [Check("auth", FAIL, str(exc))]
    try:
        await tokens.get_token(tenant.id)
    except TokenError as exc:
        checks.append(Check("token", FAIL, f"{exc} [reason: {exc.reason}]"))
        skip = "skipped: no token"
        return [*checks, *(Check(n, SKIP, skip) for n in ("permissions", "reports", "signin"))]
    checks.append(Check("token", OK, "token acquired"))

    # Roles come from the token's roles claim, decoded locally; the token is never shown.
    roles = tokens.granted_roles(tenant.id)
    if roles is None:
        checks.append(Check("permissions", SKIP, "token has no roles claim: collectors not gated"))
    else:
        relevant = sorted(roles & set(permissions()))
        checks.append(Check("permissions", OK, "granted: " + (", ".join(relevant) or "none")))

    options = TenantOptions(report_period(settings, tenant), onedrive_source(settings, tenant))
    running: set[str] = set()
    for name in configured:
        collector = collectors[name]
        missing = collector.missing_permissions(options, roles)
        if missing:
            # Listed explicitly but cannot run: that needs fixing. Under `all` it is expected.
            status = FAIL if explicit else SKIP
            detail = "skipped: missing " + "; ".join(format_requirement(g) for g in missing)
        else:
            running.add(name)
            status, detail = OK, "runs"
            if name == "onedrive":
                detail += f" (source {effective_source(options.onedrive_source, roles)})"
        extras = [
            f"{format_requirement(g)} would add {what}"
            for g, what in collector.optional_permissions
            if roles is not None and not roles & set(g)
        ]
        if extras:
            detail += "; optional: " + "; ".join(extras)
        checks.append(Check(f"collector {name}", status, detail))

    tg = graph.for_tenant(tenant)
    if not REPORT_COLLECTORS & running:
        checks.append(Check("reports", SKIP, "no report collectors running"))
    elif roles is not None and not roles & set(REPORT_SETTINGS):
        checks.append(
            Check(
                "reports",
                SKIP,
                "ReportSettings.Read.All not granted: concealment is inferred from report data",
            )
        )
    else:
        try:
            body = await tg.get_json("/admin/reportSettings")
            value = body.get("value")
            flags = value if isinstance(value, dict) else body
            concealed = bool(flags.get("displayConcealedNames"))
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
        except GraphAuthError as exc:
            checks.append(
                Check("reports", SKIP, f"{exc.code}: concealment is inferred from report data")
            )
        except GraphError as exc:
            checks.append(Check("reports", FAIL, f"{exc.status} {exc.code}"))

    if "users" not in running:
        checks.append(Check("signin", SKIP, "users collector not running"))
    elif roles is not None and not roles & set(SIGNIN_READ):
        checks.append(
            Check(
                "signin",
                SKIP,
                "AuditLog.Read.All not granted: no last sign-in; last activity falls back to "
                "the mailbox report",
            )
        )
    else:
        try:
            await tg.get_json("/users", {"$select": "id,signInActivity", "$top": "1"})
            checks.append(Check("signin", OK, "signInActivity available"))
        except GraphAuthError as exc:
            checks.append(
                Check("signin", FAIL, f"{exc.code}: needs AuditLog.Read.All and Entra ID P1")
            )
        except GraphError as exc:
            checks.append(Check("signin", FAIL, f"{exc.status} {exc.code}"))
    return checks


@tenant_app.command("verify")
def tenant_verify(
    ctx: typer.Context,
    selector: Annotated[str, typer.Argument(help="Customer slug, tenant name or tenant id")],
    fixtures: FixturesOpt = None,
) -> None:
    """Check auth, token, which collectors run or are skipped (and why), report
    concealment and signInActivity."""
    c = _ctx(ctx)
    configure_logging(c.log_level if c.log_level.upper() != "INFO" else "WARNING", json=False)
    try:
        cfg = load_all(c.config_dir, list(all_collectors()))
        tokens = _token_provider(cfg.settings, cfg.tenants, fixtures)
    except (ConfigError, SecretError, CertificateError) as exc:
        raise _fail(str(exc)) from None
    tenants = _match(cfg.tenants, selector)
    if not tenants:
        raise _fail(f"no tenant matches {selector!r}")

    async def run() -> list[tuple[TenantConfig, list[Check]]]:
        graph = _graph(cfg.settings, tokens, fixtures)
        try:
            return [(t, await _verify_tenant(t, tokens, graph, cfg.settings)) for t in tenants]
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
        tokens = _token_provider(cfg.settings, cfg.tenants, fixtures)
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
                "skipped_missing": [format_requirement(g) for g in r.skipped],
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
            if r.skipped:
                missing = "; ".join(format_requirement(g) for g in r.skipped)
                table.add_row(r.tenant.name, r.collector, f"{SKIP} skipped: missing {missing}")
            for s in r.samples:
                labels = ",".join(f'{k}="{v}"' for k, v in s.labels.items())
                table.add_row(r.tenant.name, r.collector, s.name, labels, f"{s.value:g}")
        out.print(table)
    if not all(r.ok for r in results):
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
