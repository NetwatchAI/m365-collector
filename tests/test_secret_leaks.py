"""A known fake client secret must never appear in logs, exceptions, repr, CLI output or
/metrics: on success, invalid_client, expired secret and network error. The identity endpoint
is faked as hostile: its errors echo the whole token request (secret included)."""

from __future__ import annotations

import logging
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from prometheus_client import CollectorRegistry, generate_latest
from typer.testing import CliRunner

from m365c import cli
from m365c.auth import TenantTokenProvider
from m365c.cache import SnapshotCache
from m365c.collectors import all_collectors
from m365c.config import HttpConfig, Settings, TenantAuth, TenantConfig, resolve_auth
from m365c.exporter import register
from m365c.fixtures import fixture_transport
from m365c.graph import GraphClient
from m365c.logging import configure_logging
from m365c.scheduler import Scheduler
from m365c.state import AppState

from .conftest import GRAPH_FIXTURES, TENANT_ID
from .identity import FAKE_SECRET, FakeIdentity, token_error, token_ok

CLIENT = "33333333-3333-3333-3333-333333333333"
FORMS = (
    FAKE_SECRET,
    urllib.parse.quote(FAKE_SECRET, safe=""),
    urllib.parse.quote_plus(FAKE_SECRET),
)


def _no_token(form: dict[str, str]) -> tuple[int, dict[str, Any]]:
    return 200, {"token_type": "Bearer", "echo": dict(form)}  # no access_token


def _server_error_echo(form: dict[str, str]) -> tuple[int, dict[str, Any]]:
    return 500, {"error": "server_error", "error_description": f"upstream failed: {form}"}


SCENARIOS: dict[str, tuple[Any, str | None]] = {
    "success": (token_ok(), None),
    "invalid_client": (token_error(7000215), "invalid_client_secret"),
    "secret_expired": (token_error(7000222), "secret_expired"),
    "network_error": (None, None),
    "no_access_token": (_no_token, None),
    "server_error_echo": (_server_error_echo, None),
    "invalid_request_echo": (token_error(90014, "invalid_request"), "auth_failed"),
}


def assert_clean(text: str) -> None:
    for form in FORMS:
        assert form not in text


@pytest.fixture
def secret_path(tmp_path: Path) -> Path:
    path = tmp_path / "contoso_client_secret"
    path.write_text(FAKE_SECRET + "\n", encoding="utf-8")
    return path


@pytest.fixture
def debug_logs(capsys: pytest.CaptureFixture[str]) -> Iterator[pytest.CaptureFixture[str]]:
    configure_logging("DEBUG", json=True)
    yield capsys
    logging.getLogger().handlers.clear()


def make_identity(scenario: str) -> FakeIdentity:
    handler, _ = SCENARIOS[scenario]
    identity = FakeIdentity(handler)
    identity.network_error = scenario == "network_error"
    return identity


@pytest.mark.parametrize("scenario", list(SCENARIOS))
async def test_secret_never_leaks_from_service_paths(
    scenario: str, secret_path: Path, debug_logs: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(app={"client_id": "00000000-0000-0000-0000-000000000000"})
    tenant = TenantConfig(
        id=TENANT_ID,
        name="contoso.onmicrosoft.com",
        customer="contoso",
        auth=TenantAuth(
            method="client_secret", client_id=CLIENT, client_secret=f"file:{secret_path}"
        ),
    )
    identity = make_identity(scenario)
    tokens = TenantTokenProvider(credential_kwargs={"transport": identity, "retry_total": 0})
    graph = GraphClient(
        tokens, HttpConfig(max_retries=0), transport=fixture_transport(GRAPH_FIXTURES)
    )
    cache_dir = secret_path.parent / "cache"
    state = AppState(collectors=all_collectors(), cache=SnapshotCache(cache_dir))
    registry = CollectorRegistry()
    register(state, registry)
    scheduler = Scheduler(state, graph, settings)
    await scheduler.apply_tenants([tenant])
    try:
        # Every parity collector, not just license, so all report and user paths are covered.
        results = [
            await scheduler.run_once(tenant, name)
            for name in ("license", "mailbox", "users", "mailtraffic", "onedrive")
        ]
        result = results[0]
    finally:
        await scheduler.stop()
        await graph.aclose()
        await tokens.aclose()

    if scenario != "network_error":
        assert set(identity.secrets_seen) == {FAKE_SECRET}  # the secret really was in play
    metrics = generate_latest(registry).decode()
    expected_reason = SCENARIOS[scenario][1]
    if scenario == "success":
        assert result.ok and "m365_tenant_auth_ok{" in metrics
    else:
        assert not result.ok and result.error
        assert_clean(result.error)
    if expected_reason:
        assert f'm365_tenant_auth_error{{customer="contoso",reason="{expected_reason}"' in metrics
    assert_clean(metrics)
    assert_clean(repr(resolve_auth(settings, tenant)))
    assert_clean(repr(tenant))
    for r in results:
        assert_clean(repr(r))
    for cached in cache_dir.glob("*.json"):  # persisted snapshots (customer data on disk)
        assert_clean(cached.read_text(encoding="utf-8"))
    captured = debug_logs.readouterr()
    assert_clean(captured.out + captured.err)


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_secret_never_leaks_from_cli(
    scenario: str, secret_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "settings.yaml").write_text(
        "app:\n  client_id: 00000000-0000-0000-0000-000000000000\nhttp:\n  max_retries: 0\n",
        encoding="utf-8",
    )
    (cfg / "tenants.yaml").write_text(
        f"tenants:\n  - id: {TENANT_ID}\n    name: contoso.onmicrosoft.com\n"
        "    customer: contoso\n    auth:\n      method: client_secret\n"
        f"      client_id: {CLIENT}\n      client_secret: file:{secret_path.as_posix()}\n",
        encoding="utf-8",
    )
    identity = make_identity(scenario)
    monkeypatch.setattr(
        cli,
        "TenantTokenProvider",
        lambda auths: TenantTokenProvider(
            auths, credential_kwargs={"transport": identity, "retry_total": 0}
        ),
    )
    monkeypatch.setattr(
        cli,
        "_graph",
        lambda settings, tokens, fixtures: GraphClient(
            tokens, settings.http, transport=fixture_transport(GRAPH_FIXTURES)
        ),
    )
    runner = CliRunner()
    for args in (
        ["--log-level", "DEBUG", "-c", str(cfg), "tenant", "verify", "contoso"],
        ["--log-level", "DEBUG", "-c", str(cfg), "run", "--tenant", "contoso", "--dry-run",
         "--collector", "license", "--json"],
        ["-c", str(cfg), "tenant", "list"],
    ):  # fmt: skip
        result = runner.invoke(cli.app, args)
        assert_clean(result.output)
        if result.exception is not None and not isinstance(result.exception, SystemExit):
            assert_clean(repr(result.exception))
        if scenario in ("invalid_client", "secret_expired") and "verify" in args:
            assert result.exit_code == 1
            assert SCENARIOS[scenario][1] in result.output or "AADSTS" in result.output
    logging.getLogger().handlers.clear()


def test_tenant_add_never_echoes_a_secret_in_any_option(cfg_dir_for_add: Path) -> None:
    """A secret pasted into --client-id (easy when copying Zabbix macros side by side) or
    into --client-secret-ref is refused without being echoed."""
    runner = CliRunner()
    for extra in (
        ["--client-id", FAKE_SECRET, "--client-secret-ref", "env:X"],
        ["--client-id", CLIENT, "--client-secret-ref", FAKE_SECRET],
    ):
        result = runner.invoke(
            cli.app,
            ["-c", str(cfg_dir_for_add), "tenant", "add", "--tenant-id", TENANT_ID,
             "--name", "contoso.onmicrosoft.com", "--customer", "contoso",
             "--auth", "client-secret", *extra],
        )  # fmt: skip
        assert result.exit_code == 2
        assert_clean(result.output)
        assert_clean((cfg_dir_for_add / "tenants.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def cfg_dir_for_add(tmp_path: Path) -> Path:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "settings.yaml").write_text(
        "app:\n  client_id: 00000000-0000-0000-0000-000000000000\n", encoding="utf-8"
    )
    (cfg / "tenants.yaml").write_text("tenants: []\n", encoding="utf-8")
    return cfg
