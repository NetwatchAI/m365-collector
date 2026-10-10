"""Regression tests for the second review (m365-collector-review-2.md)."""

from __future__ import annotations

import asyncio
import sys
import time
import types
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
import respx
from typer.testing import CliRunner

from m365c import cli
from m365c.auth import (
    TRANSIENT_GRACE_S,
    CertificateInfo,
    CredentialMaterial,
    StaticTokenProvider,
    TenantTokenProvider,
    TokenError,
    roles_from_token,
)
from m365c.cache import SnapshotCache
from m365c.collectors import all_collectors
from m365c.config import (
    ConfigError,
    HttpConfig,
    ResolvedAuth,
    Settings,
    TenantAuth,
    TenantConfig,
    load_all,
)
from m365c.fixtures import fake_jwt, fixture_transport
from m365c.graph import GraphClient
from m365c.health import FailureKind
from m365c.scheduler import Scheduler
from m365c.secrets import SecretBytes, SecretError, load_secret
from m365c.state import AppState

from .conftest import GRAPH_FIXTURES, QUOTA, TENANT_ID, make_ctx
from .identity import FAKE_SECRET, PARITY_ROLES, FakeIdentity
from .test_collectors import find, value
from .test_exporter import scrape

V1 = "https://graph.microsoft.com/v1.0"
CLIENT = "33333333-3333-3333-3333-333333333333"
SETTINGS = Settings(
    app={"client_id": "00000000-0000-0000-0000-000000000000"},
    secrets={"cert": "file:/run/secrets/m365c_cert"},
)


def secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "secret"
    path.write_text(FAKE_SECRET, encoding="utf-8")
    return path


# ------------------------------------------------------------------ H1 / M3: roles


def test_roles_semantics() -> None:
    assert roles_from_token("not-a-jwt") is None  # non-JWT: gating disabled
    assert roles_from_token(fake_jwt([])) == frozenset()
    claims_without_roles = fake_jwt(["x"]).split(".")
    import base64
    import json

    payload = base64.urlsafe_b64encode(json.dumps({"scp": "User.Read"}).encode()).rstrip(b"=")
    delegated = f"{claims_without_roles[0]}.{payload.decode()}."
    assert roles_from_token(delegated) == frozenset()


async def test_token_without_roles_is_an_auth_error(tmp_path: Path) -> None:
    identity = FakeIdentity(
        lambda form: (
            200,
            {"token_type": "Bearer", "expires_in": 3600, "access_token": fake_jwt([])},
        )
    )
    tokens = TenantTokenProvider(
        {TENANT_ID: ResolvedAuth("client_secret", CLIENT, f"file:{secret_file(tmp_path)}")},
        credential_kwargs={"transport": identity, "retry_total": 0},
    )
    with pytest.raises(TokenError) as info:
        await tokens.get_token(TENANT_ID)
    assert info.value.reason == "no_roles" and not info.value.transient
    await tokens.aclose()


async def test_revoked_consent_is_not_silent(tenant: TenantConfig) -> None:
    """Review 2 H1: a token with no roles used to skip every collector with auth_ok 1."""
    graph = GraphClient(
        StaticTokenProvider(fake_jwt([])), HttpConfig(max_retries=0),
        transport=fixture_transport(GRAPH_FIXTURES),
    )  # fmt: skip
    state = AppState(collectors=all_collectors(), cache=SnapshotCache())
    scheduler = Scheduler(state, graph, SETTINGS)
    await scheduler.apply_tenants([tenant])
    try:
        result = await scheduler.run_once(tenant, "mailbox")
    finally:
        await scheduler.stop()
        await graph.aclose()
    assert not result.ok and not result.skipped
    m = scrape(state)
    base = {"customer": "contoso", "tenant": "contoso.onmicrosoft.com"}
    assert m["m365_tenant_auth_ok"] == [(base, 0.0)]
    assert m["m365_tenant_auth_error"] == [({**base, "reason": "no_roles"}, 1.0)]


def test_collector_expected_metric(app_state: AppState, tenant: TenantConfig) -> None:
    m = scrape(app_state)
    expected = {x[0]["collector"]: x[1] for x in m["m365_collector_expected"]}
    assert expected == {
        "license": 1.0, "users": 1.0, "mailbox": 1.0, "onedrive": 1.0, "mailtraffic": 1.0,
        "servicehealth": 0.0, "securescore": 0.0, "appsecrets": 0.0,
    }  # fmt: skip
    explicit = tenant.model_copy(update={"collectors": ["license", "appsecrets"]})
    app_state.tenants = {tenant.id: explicit}
    m = scrape(app_state)
    assert {x[0]["collector"]: x[1] for x in m["m365_collector_expected"]} == {
        "license": 1.0,
        "appsecrets": 1.0,
    }


# ------------------------------------------------------------------ H2: unreadable secret


def test_permission_denied_secret_names_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = secret_file(tmp_path)

    def denied(self: Path) -> bytes:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(SecretError) as info:
        load_secret(f"file:{path}")
    message = str(info.value)
    assert "permission denied" in message and "traverse every directory" in message
    assert "10001" in message


# ------------------------------------------------------------------ L2: skipped rows


@respx.mock
async def test_partially_hashed_report_counts_skipped_rows(
    respx_graph: GraphClient, tenant: TenantConfig
) -> None:
    csv = (
        "User Principal Name,Is Deleted,Send Count,Receive Count,Read Count\n"
        "a@x,False,1,2,3\n5A3F9C0E1B2D4F6A8B7C9D0E1F2A3B4C,False,1,1,1\n"
        "gone@x,True,1,1,1\n"
    )
    download = "https://reports.office.com/data/download/abc"
    respx.get(f"{V1}/admin/reportSettings").mock(
        return_value=httpx.Response(200, json={"displayConcealedNames": False})
    )
    respx.get(url__regex=r".*/reports/getEmailActivityUserDetail.*").mock(
        return_value=httpx.Response(302, headers={"Location": download})
    )
    respx.get(download).mock(return_value=httpx.Response(200, content=csv.encode()))
    state: dict[str, object] = {}
    s = await all_collectors()["mailtraffic"].collect(make_ctx(respx_graph, tenant, state))
    assert value(s, "m365_reports_concealed", report="mailtraffic") == 0
    assert value(s, "m365_report_rows_skipped", report="mailtraffic", reason="no_upn") == 1
    assert state["rows_skipped_warned"] is True


# ------------------------------------------------------------------ L3: GUID echo


def test_bad_guid_is_not_echoed(tmp_path: Path) -> None:
    (tmp_path / "settings.yaml").write_text(f"app:\n  client_id: {FAKE_SECRET}\n", encoding="utf-8")
    (tmp_path / "tenants.yaml").write_text("tenants: []\n", encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_all(tmp_path, list(all_collectors()))
    assert "not a GUID" in str(info.value) and FAKE_SECRET not in str(info.value)


# ------------------------------------------------------------------ L4: configure race


class _CountingCredential:
    created: ClassVar[int] = 0
    closed: ClassVar[int] = 0

    def __init__(self) -> None:
        type(self).created += 1

    async def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        return types.SimpleNamespace(token=fake_jwt(PARITY_ROLES), expires_on=time.time() + 3600)

    async def close(self) -> None:
        type(self).closed += 1


class _SlowMethod:
    name: ClassVar[str] = "client_secret"

    def load(self, auth: ResolvedAuth) -> CredentialMaterial:
        time.sleep(0.2)  # runs in a worker thread while configure() runs on the loop
        return CredentialMaterial(SecretBytes(auth.client_id.encode()))

    def credential(self, tenant_id: str, auth: ResolvedAuth, material: Any, **kw: Any) -> Any:
        return _CountingCredential()


async def test_reconfigure_during_secret_load_leaks_no_credential() -> None:
    _CountingCredential.created = _CountingCredential.closed = 0
    old = ResolvedAuth("client_secret", CLIENT, "env:OLD")
    new = ResolvedAuth("client_secret", "44444444-4444-4444-4444-444444444444", "env:NEW")
    tokens = TenantTokenProvider({TENANT_ID: old}, methods={"client_secret": _SlowMethod()})
    pending = asyncio.create_task(tokens.get_token(TENANT_ID))
    await asyncio.sleep(0.05)  # load in progress for the old entry
    await tokens.configure({TENANT_ID: new})
    await pending
    await tokens.aclose()
    assert _CountingCredential.created == _CountingCredential.closed  # nothing leaked


# ------------------------------------------------------------------ L5: persistent transient


async def test_persistent_identity_errors_become_auth_failure(tmp_path: Path) -> None:
    now = [1000.0]
    identity = FakeIdentity()
    identity.network_error = True
    tokens = TenantTokenProvider(
        {TENANT_ID: ResolvedAuth("client_secret", CLIENT, f"file:{secret_file(tmp_path)}")},
        credential_kwargs={"transport": identity, "retry_total": 0},
        clock=lambda: now[0],
    )
    with pytest.raises(TokenError) as first:
        await tokens.get_token(TENANT_ID)
    assert first.value.transient
    now[0] += TRANSIENT_GRACE_S
    with pytest.raises(TokenError) as later:
        await tokens.get_token(TENANT_ID)
    assert not later.value.transient and later.value.reason == "identity_unreachable"
    identity.network_error = False  # recovered: the timer resets
    assert await tokens.get_token(TENANT_ID)
    assert tokens._entries[TENANT_ID].transient_since is None
    await tokens.aclose()


# ------------------------------------------------------------------ L6: skip kept on token outage


def test_token_outage_keeps_skip_state(app_state: AppState, tenant: TenantConfig) -> None:
    health = app_state.health
    health.record_skipped(tenant.id, "servicehealth", (("ServiceHealth.Read.All",),))
    health.record_failure(tenant.id, "servicehealth", 0.1, "x", FailureKind.TRANSIENT_TOKEN)
    assert (tenant.id, "servicehealth") in health.skipped
    m = scrape(app_state)
    assert not [
        x for x in m.get("m365_collector_success", []) if x[0]["collector"] == "servicehealth"
    ]
    # A definitive token failure still marks the tenant, without un-skipping.
    health.record_failure(tenant.id, "servicehealth", 0.1, "x", FailureKind.TOKEN, "no_roles")
    assert health.auth_ok(tenant.id) is False and (tenant.id, "servicehealth") in health.skipped


# ------------------------------------------------------------------ L7: tenant certificates


def test_tenant_certificate_expiry_exported(app_state: AppState, tenant: TenantConfig) -> None:
    own = tenant.model_copy(
        update={"auth": TenantAuth(method="certificate", cert="vault:secret/c/cert#pem")}
    )
    app_state.tenants = {tenant.id: own}
    info = CertificateInfo("CN=t", "CD" * 20, 0.0, 1_900_000_000.0)
    app_state.tenant_certificates = lambda: {tenant.id: info}
    m = scrape(app_state)
    base = {"customer": "contoso", "tenant": "contoso.onmicrosoft.com"}
    assert m["m365_tenant_cert_expiry_timestamp"] == [
        ({**base, "thumbprint": "CD" * 20}, 1_900_000_000.0)
    ]
    app_state.tenants = {tenant.id: tenant}  # default certificate: not a tenant metric
    assert "m365_tenant_cert_expiry_timestamp" not in scrape(app_state)


# ------------------------------------------------------------------ L11: quota 403s


@respx.mock
async def test_onedrive_quota_403s_do_not_count_as_failures(
    respx_graph: GraphClient, tenant: TenantConfig
) -> None:
    users = {"value": [
        {"id": f"u{i}", "userPrincipalName": f"u{i}@x", "accountEnabled": True,
         "assignedLicenses": [{}]} for i in range(10)
    ]}  # fmt: skip
    respx.get(f"{V1}/users").mock(return_value=httpx.Response(200, json=users))

    def drive(request: httpx.Request) -> httpx.Response:
        if request.url.path.split("/")[-2] in {"u1", "u2", "u3"}:  # 30 % refused
            return httpx.Response(403, json={"error": {"code": "accessDenied", "message": "x"}})
        return httpx.Response(200, json={"quota": {"used": 1, "total": 10}})

    respx.get(url__regex=r".*/users/u\d+/drive.*").mock(side_effect=drive)
    s = await all_collectors()["onedrive"].collect(make_ctx(respx_graph, tenant, options=QUOTA))
    assert len(find(s, "m365_onedrive_used_bytes")) == 7


# ------------------------------------------------------------------ N3: duplicate first


def test_tenant_add_reports_duplicate_before_auth_problems(tmp_path: Path) -> None:
    (tmp_path / "settings.yaml").write_text(
        "app:\n  client_id: 00000000-0000-0000-0000-000000000000\n", encoding="utf-8"
    )
    (tmp_path / "tenants.yaml").write_text(
        f"tenants:\n  - id: {TENANT_ID}\n    name: contoso.onmicrosoft.com\n    customer: contoso\n"
        f"    auth:\n      method: client_secret\n      client_id: {CLIENT}\n"
        "      client_secret: env:X\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        cli.app,
        ["-c", str(tmp_path), "tenant", "add", "--tenant-id", TENANT_ID,
         "--name", "contoso.onmicrosoft.com", "--customer", "contoso"],
    )  # fmt: skip
    assert result.exit_code == 2
    assert "already configured" in result.output and "secrets.cert" not in result.output


# ------------------------------------------------------------------ L9: vault backends


class _FakeHvacClient:
    def __init__(self, url: str, namespace: str | None = None) -> None:
        self.url, self.token, self.logins = url, None, []
        outer = self

        class _AppRole:
            def login(self, role_id: str, secret_id: str) -> None:
                outer.logins.append("approle")

        class _K8s:
            def login(self, role: str, jwt: str, mount_point: str) -> None:
                outer.logins.append(f"k8s:{mount_point}")

        class _KV2:
            def read_secret_version(self, path: str, mount_point: str, **kw: Any) -> Any:
                if path == "missing":
                    raise RuntimeError("404")
                return {"data": {"data": {"value": f"{mount_point}/{path}-secret"}}}

        self.auth = types.SimpleNamespace(approle=_AppRole(), kubernetes=_K8s())
        self.secrets = types.SimpleNamespace(kv=types.SimpleNamespace(v2=_KV2()))
        _FakeHvacClient.last = self

    last: _FakeHvacClient | None = None


@pytest.fixture
def fake_hvac(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("hvac")
    module.Client = _FakeHvacClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hvac", module)
    for var in ("VAULT_ROLE_ID", "VAULT_SECRET_ID", "VAULT_K8S_ROLE", "VAULT_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.example:8200")


def test_vault_approle_kubernetes_and_token(
    fake_hvac: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(SecretError, match="no auth configured"):
        load_secret("vault:secret/m365c/x#value")
    monkeypatch.setenv("VAULT_TOKEN", "dev-token")
    assert load_secret("vault:secret/m365c/x#value").get() == b"secret/m365c/x-secret"
    monkeypatch.setenv("VAULT_ROLE_ID", "r")
    monkeypatch.setenv("VAULT_SECRET_ID", "s")
    load_secret("vault:secret/m365c/x#value")
    assert _FakeHvacClient.last is not None and _FakeHvacClient.last.logins == ["approle"]
    monkeypatch.delenv("VAULT_ROLE_ID")
    jwt = tmp_path / "sa-token"
    jwt.write_text("k8s-jwt", encoding="utf-8")
    monkeypatch.setenv("VAULT_K8S_ROLE", "m365c")
    monkeypatch.setenv("VAULT_K8S_TOKEN_PATH", str(jwt))
    load_secret("vault:secret/m365c/x#value")
    assert _FakeHvacClient.last.logins == ["k8s:kubernetes"]
    with pytest.raises(SecretError, match="read failed"):
        load_secret("vault:secret/missing#value")
    with pytest.raises(SecretError, match="key 'other' not found"):
        load_secret("vault:secret/m365c/x#other")


def test_key_vault_secret_and_pkcs12(monkeypatch: pytest.MonkeyPatch) -> None:
    import base64

    secrets: dict[str, tuple[str, str | None]] = {
        "plain": ("kv-secret-value", None),
        "cert": (base64.b64encode(b"\x30\x82binary").decode(), "application/x-pkcs12"),
    }

    class _Cred:
        def __enter__(self) -> _Cred:
            return self

        def __exit__(self, *a: object) -> None:
            return None

    class _Client:
        def __init__(self, vault_url: str, credential: object) -> None:
            assert vault_url == "https://kv1.vault.azure.net"

        def get_secret(self, name: str) -> Any:
            if name not in secrets:
                raise RuntimeError("not found")
            value, ctype = secrets[name]
            return types.SimpleNamespace(
                value=value, properties=types.SimpleNamespace(content_type=ctype)
            )

    module = types.ModuleType("azure.keyvault.secrets")
    module.SecretClient = _Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "azure.keyvault.secrets", module)
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", _Cred)
    assert load_secret("keyvault:https://kv1.vault.azure.net/plain").get() == b"kv-secret-value"
    assert load_secret("keyvault:https://kv1.vault.azure.net/cert").get() == b"\x30\x82binary"
    with pytest.raises(SecretError, match="read failed"):
        load_secret("keyvault:https://kv1.vault.azure.net/missing")
