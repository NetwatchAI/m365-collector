"""Per-tenant auth: client secret and certificate methods, lazy secrets, the credential cache
and rotation, and Entra error mapping. azure-identity's real credentials talk to a fake token
endpoint (tests/identity.py)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from m365c.auth import TenantTokenProvider, TokenError, classify_auth_failure
from m365c.config import ResolvedAuth

from .certs import pem_bundle
from .identity import FAKE_SECRET, PARITY_ROLES, FakeIdentity, token_error, token_ok

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"
CLIENT = "33333333-3333-3333-3333-333333333333"


def secret_file(tmp_path: Path, value: str = FAKE_SECRET, name: str = "secret") -> Path:
    path = tmp_path / name
    path.write_text(value + "\n", encoding="utf-8")  # trailing newline, as `echo` writes it
    return path


def secret_auth(path: Path, client_id: str = CLIENT) -> ResolvedAuth:
    return ResolvedAuth("client_secret", client_id, f"file:{path}")


def provider(
    auths: dict[str, ResolvedAuth], identity: FakeIdentity, **kwargs: object
) -> TenantTokenProvider:
    return TenantTokenProvider(
        auths, credential_kwargs={"transport": identity, "retry_total": 0}, **kwargs
    )  # type: ignore[arg-type]


async def test_client_secret_token_and_roles(tmp_path: Path) -> None:
    identity = FakeIdentity()
    tokens = provider({TENANT_A: secret_auth(secret_file(tmp_path))}, identity)
    assert tokens.granted_roles(TENANT_A) is None  # unknown until the first token
    await tokens.get_token(TENANT_A)
    assert tokens.granted_roles(TENANT_A) == frozenset(PARITY_ROLES)
    # The trailing newline is stripped; the secret is sent to the token endpoint only.
    assert identity.secrets_seen == [FAKE_SECRET]
    assert identity.requests[0]["client_id"] == CLIENT
    await tokens.get_token(TENANT_A)
    assert len(identity.requests) == 1  # azure-identity's cache
    await tokens.aclose()


async def test_certificate_method(tmp_path: Path) -> None:
    pem = tmp_path / "cert.pem"
    pem.write_bytes(pem_bundle(rsa_key=True))
    identity = FakeIdentity()
    tokens = provider({TENANT_A: ResolvedAuth("certificate", CLIENT, f"file:{pem}")}, identity)
    await tokens.get_token(TENANT_A)
    assert "client_assertion" in identity.requests[0]  # signed JWT, no secret
    assert [c.subject for c in tokens.certificates()] == ["CN=m365c-test"]
    await tokens.aclose()


@pytest.mark.parametrize(
    ("code", "reason", "fragment"),
    [
        (7000215, "invalid_client_secret", "invalid client secret"),
        (7000222, "secret_expired", "client secret expired"),
        (700016, "app_not_found", "application not found"),
        (90002, "tenant_not_found", "tenant not found"),
        (65001, "consent_missing", "admin consent missing"),
        (50000, "auth_failed", "authentication failed"),
    ],
)
async def test_entra_errors_are_mapped_without_echoing(
    tmp_path: Path, code: int, reason: str, fragment: str
) -> None:
    identity = FakeIdentity(token_error(code))
    tokens = provider({TENANT_A: secret_auth(secret_file(tmp_path))}, identity)
    with pytest.raises(TokenError) as info:
        await tokens.get_token(TENANT_A)
    err = info.value
    assert (err.reason, err.transient) == (reason, False)
    assert fragment in str(err) and f"AADSTS{code}" in str(err)
    # The endpoint echoed the request body (with the secret) in error_description.
    assert FAKE_SECRET not in str(err) and FAKE_SECRET not in repr(err)
    assert "Request:" not in str(err)
    await tokens.aclose()


def test_classify_without_code() -> None:
    assert classify_auth_failure("(invalid_client) bad")[0] == "invalid_client"
    reason, message, transient = classify_auth_failure("503 Service Unavailable")
    assert (reason, transient) == ("transient", True) and "503" not in message


async def test_network_error_is_transient(tmp_path: Path) -> None:
    identity = FakeIdentity()
    identity.network_error = True
    tokens = provider({TENANT_A: secret_auth(secret_file(tmp_path))}, identity)
    with pytest.raises(TokenError) as info:
        await tokens.get_token(TENANT_A)
    assert info.value.transient and info.value.reason == "transient"
    await tokens.aclose()


async def test_broken_secret_only_affects_its_tenant(tmp_path: Path) -> None:
    identity = FakeIdentity()
    tokens = provider(
        {
            TENANT_A: secret_auth(tmp_path / "missing"),
            TENANT_B: secret_auth(secret_file(tmp_path)),
        },
        identity,
    )
    with pytest.raises(TokenError) as info:
        await tokens.get_token(TENANT_A)
    assert info.value.reason == "secret_unavailable" and "file not found" in str(info.value)
    assert await tokens.get_token(TENANT_B)  # unaffected
    # Fixed later: picked up on the next attempt, no restart.
    secret_file(tmp_path, name="missing")
    assert await tokens.get_token(TENANT_A)
    await tokens.aclose()


async def test_env_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("M365C_TEST_SECRET", FAKE_SECRET)
    identity = FakeIdentity()
    auth = ResolvedAuth("client_secret", CLIENT, "env:M365C_TEST_SECRET")
    tokens = provider({TENANT_A: auth}, identity)
    await tokens.get_token(TENANT_A)
    assert identity.secrets_seen == [FAKE_SECRET]
    await tokens.aclose()


async def test_rotated_secret_file_gives_new_credential(tmp_path: Path) -> None:
    path = secret_file(tmp_path)
    identity = FakeIdentity()
    tokens = provider({TENANT_A: secret_auth(path)}, identity, recheck_s=0.0)
    await tokens.get_token(TENANT_A)
    first = tokens._entries[TENANT_A].credential

    path.write_text("rotated-secret-value-0001\n", encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))  # mtime changed
    await tokens.get_token(TENANT_A)
    entry = tokens._entries[TENANT_A]
    assert entry.credential is not first  # new key -> new credential, old one closed
    assert identity.secrets_seen == [FAKE_SECRET, "rotated-secret-value-0001"]
    await tokens.aclose()


async def test_unchanged_file_keeps_credential(tmp_path: Path) -> None:
    identity = FakeIdentity()
    tokens = provider({TENANT_A: secret_auth(secret_file(tmp_path))}, identity, recheck_s=0.0)
    await tokens.get_token(TENANT_A)
    first = tokens._entries[TENANT_A].credential
    await tokens.get_token(TENANT_A)
    assert tokens._entries[TENANT_A].credential is first
    assert len(identity.requests) == 1
    await tokens.aclose()


async def test_reconfigure_closes_changed_tenants_only(tmp_path: Path) -> None:
    identity = FakeIdentity(token_ok())
    a, b = (
        secret_auth(secret_file(tmp_path, name="a")),
        secret_auth(secret_file(tmp_path, name="b")),
    )
    tokens = provider({TENANT_A: a, TENANT_B: b}, identity)
    await tokens.get_token(TENANT_A)
    await tokens.get_token(TENANT_B)
    cred_a = tokens._entries[TENANT_A].credential
    cred_b = tokens._entries[TENANT_B].credential

    new_b = ResolvedAuth("client_secret", "44444444-4444-4444-4444-444444444444", b.secret_ref)
    await tokens.configure({TENANT_A: a, TENANT_B: new_b})
    assert tokens._entries[TENANT_A].credential is cred_a
    assert tokens._entries[TENANT_B].credential is None  # re-resolved on next use
    await tokens.get_token(TENANT_B)
    assert tokens._entries[TENANT_B].credential is not cred_b
    assert identity.requests[-1]["client_id"] == "44444444-4444-4444-4444-444444444444"

    await tokens.configure({TENANT_A: a})  # tenant B removed
    assert TENANT_B not in tokens._entries
    with pytest.raises(TokenError) as info:
        await tokens.get_token(TENANT_B)
    assert info.value.reason == "config"
    await tokens.aclose()


async def test_new_token_updates_roles(tmp_path: Path) -> None:
    roles = ["User.Read.All"]
    identity = FakeIdentity(lambda form: token_ok(roles)(form))
    tokens = provider({TENANT_A: secret_auth(secret_file(tmp_path))}, identity)
    await tokens.get_token(TENANT_A)
    assert tokens.granted_roles(TENANT_A) == {"User.Read.All"}
    # Consent granted later; a new token (here: new credential after reconfigure) shows it.
    roles.append("Reports.Read.All")
    auth = tokens._entries[TENANT_A].auth
    await tokens.configure({})
    await tokens.configure({TENANT_A: auth})
    await tokens.get_token(TENANT_A)
    assert tokens.granted_roles(TENANT_A) == {"User.Read.All", "Reports.Read.All"}
    await tokens.aclose()


async def test_scheduler_reports_secret_unavailable_then_recovers(tmp_path: Path) -> None:
    from m365c.cache import SnapshotCache
    from m365c.collectors import all_collectors
    from m365c.config import HttpConfig, Settings, TenantAuth, TenantConfig
    from m365c.fixtures import fixture_transport
    from m365c.graph import GraphClient
    from m365c.scheduler import Scheduler
    from m365c.state import AppState

    from .conftest import GRAPH_FIXTURES

    missing = tmp_path / "later"
    settings = Settings(app={"client_id": "00000000-0000-0000-0000-000000000000"})
    tenant = TenantConfig(
        id=TENANT_A, name="contoso.onmicrosoft.com", customer="contoso",
        auth=TenantAuth(method="client_secret", client_secret=f"file:{missing}"),
    )  # fmt: skip
    tokens = TenantTokenProvider(credential_kwargs={"transport": FakeIdentity(), "retry_total": 0})
    graph = GraphClient(
        tokens, HttpConfig(max_retries=0), transport=fixture_transport(GRAPH_FIXTURES)
    )
    state = AppState(collectors=all_collectors(), cache=SnapshotCache())
    scheduler = Scheduler(state, graph, settings)
    await scheduler.apply_tenants([tenant])
    try:
        assert not (await scheduler.run_once(tenant, "license")).ok
        assert state.health.auth_ok(TENANT_A) is False
        assert state.health.auth_reason(TENANT_A) == "secret_unavailable"
        secret_file(tmp_path, name="later")
        assert (await scheduler.run_once(tenant, "license")).ok  # retried, no restart
        assert state.health.auth_ok(TENANT_A) is True
        assert state.health.auth_reason(TENANT_A) is None
    finally:
        await scheduler.stop()
        await graph.aclose()
        await tokens.aclose()
