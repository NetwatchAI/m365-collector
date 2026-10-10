"""Per-tenant auth configuration: methods, defaults, secret references, backwards compat."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from m365c.collectors import all_collectors
from m365c.config import (
    ConfigError,
    ConfigWatcher,
    ResolvedAuth,
    TenantConfig,
    Thresholds,
    load_all,
    onedrive_source,
    report_period,
    resolve_auth,
    uses_default_certificate,
    write_tenants,
)

KNOWN = list(all_collectors())
# The example files as they were on main before per-tenant auth: they must keep working.
LEGACY_EXAMPLES = Path(__file__).parent / "fixtures" / "legacy-config"
EXAMPLES = Path(__file__).parents[1] / "config"
CLIENT = "33333333-3333-3333-3333-333333333333"
SETTINGS_CERT = """app:
  client_id: 00000000-0000-0000-0000-000000000000
secrets:
  cert: file:/run/secrets/m365c_cert
"""
SETTINGS_NO_CERT = "app:\n  client_id: 00000000-0000-0000-0000-000000000000\n"
SECRET_TENANT = f"""tenants:
  - id: 11111111-1111-1111-1111-111111111111
    name: contoso.onmicrosoft.com
    customer: contoso
    auth:
      method: client_secret
      client_id: {CLIENT}
      client_secret: file:/run/secrets/contoso_client_secret
      client_secret_expires: 2027-03-01
    collectors: all
"""


def write_cfg(d: Path, settings: str, tenants: str) -> Path:
    d.mkdir(exist_ok=True)
    (d / "settings.yaml").write_text(settings, encoding="utf-8")
    (d / "tenants.yaml").write_text(tenants, encoding="utf-8")
    return d


def test_examples_load_unchanged_and_behave_as_before(tmp_path: Path) -> None:
    """Existing settings.yaml / tenants.yaml (no method, no tenant auth) keep working."""
    for name in ("settings", "tenants", "thresholds"):
        shutil.copy(LEGACY_EXAMPLES / f"{name}.example.yaml", tmp_path / f"{name}.yaml")
    cfg = load_all(tmp_path, KNOWN)
    assert cfg.settings.app.method == "certificate"
    for tenant in cfg.tenants:
        assert tenant.auth is None
        auth = resolve_auth(cfg.settings, tenant)
        assert auth == ResolvedAuth(
            "certificate", "00000000-0000-0000-0000-000000000000", "file:/run/secrets/m365c_cert"
        )
        assert report_period(cfg.settings, tenant) == "D7"
    assert uses_default_certificate(cfg.settings, cfg.tenants)
    # Legacy threshold keys still load (exported under the new period-neutral names).
    assert cfg.thresholds.resolve("contoso")["mail_sent_max"] == 800


def test_client_secret_tenant_overrides_default(tmp_path: Path) -> None:
    cfg = load_all(write_cfg(tmp_path, SETTINGS_CERT, SECRET_TENANT), KNOWN)
    auth = resolve_auth(cfg.settings, cfg.tenants[0])
    assert auth == ResolvedAuth(
        "client_secret", CLIENT, "file:/run/secrets/contoso_client_secret", None, date(2027, 3, 1)
    )
    assert not uses_default_certificate(cfg.settings, cfg.tenants)


def test_default_certificate_optional_without_certificate_tenants(tmp_path: Path) -> None:
    cfg = load_all(write_cfg(tmp_path, SETTINGS_NO_CERT, SECRET_TENANT), KNOWN)
    assert cfg.settings.secrets.cert is None


def test_certificate_tenant_without_certificate_is_rejected(tmp_path: Path) -> None:
    tenants = (
        "tenants:\n  - id: 11111111-1111-1111-1111-111111111111\n"
        "    name: contoso.onmicrosoft.com\n    customer: contoso\n"
    )
    with pytest.raises(
        ConfigError, match=r"uses certificate auth but settings.yaml has no secrets.cert"
    ):
        load_all(write_cfg(tmp_path, SETTINGS_NO_CERT, tenants), KNOWN)


def test_client_secret_as_default_method(tmp_path: Path) -> None:
    settings = (
        "app:\n  method: client_secret\n  client_id: 00000000-0000-0000-0000-000000000000\n"
        "  client_secret: env:M365C_CLIENT_SECRET\n"
    )
    tenants = (
        "tenants:\n  - id: 11111111-1111-1111-1111-111111111111\n"
        "    name: contoso.onmicrosoft.com\n    customer: contoso\n"
    )
    cfg = load_all(write_cfg(tmp_path, settings, tenants), KNOWN)
    assert resolve_auth(cfg.settings, cfg.tenants[0]).method == "client_secret"


def test_tenant_own_certificate(tmp_path: Path) -> None:
    tenants = (
        "tenants:\n  - id: 11111111-1111-1111-1111-111111111111\n"
        "    name: contoso.onmicrosoft.com\n    customer: contoso\n"
        "    auth:\n      method: certificate\n      cert: vault:secret/contoso/cert#pem\n"
    )
    cfg = load_all(write_cfg(tmp_path, SETTINGS_NO_CERT, tenants), KNOWN)
    auth = resolve_auth(cfg.settings, cfg.tenants[0])
    assert (auth.method, auth.secret_ref) == ("certificate", "vault:secret/contoso/cert#pem")


LITERAL = "Zq8~Th1s.Is-A-Pasted_Secret"


@pytest.mark.parametrize(
    ("settings", "tenants"),
    [
        (SETTINGS_CERT, SECRET_TENANT.replace("file:/run/secrets/contoso_client_secret", LITERAL)),
        (SETTINGS_CERT.replace("file:/run/secrets/m365c_cert", LITERAL), SECRET_TENANT),
        (
            "app:\n  method: client_secret\n  client_id: 00000000-0000-0000-0000-000000000000\n"
            f"  client_secret: {LITERAL}\n",
            SECRET_TENANT,
        ),
    ],
)
def test_inline_secrets_rejected_without_echo(tmp_path: Path, settings: str, tenants: str) -> None:
    with pytest.raises(ConfigError) as info:
        load_all(write_cfg(tmp_path, settings, tenants), KNOWN)
    message = str(info.value)
    assert "not a secret reference" in message and "Inline secret values are not allowed" in message
    assert LITERAL not in message and "Zq8" not in message


@pytest.mark.parametrize(
    ("auth", "fragment"),
    [
        ("method: client_secret", "needs client_secret"),
        ("method: certificate\n      client_secret: env:X", "only valid with method client_secret"),
        ("method: client_secret\n      client_secret: env:X\n      cert: env:Y", "only valid with method certificate"),
        ("method: workload_identity", "auth.method"),
    ],
)  # fmt: skip
def test_tenant_auth_validation(tmp_path: Path, auth: str, fragment: str) -> None:
    tenants = (
        "tenants:\n  - id: 11111111-1111-1111-1111-111111111111\n"
        "    name: contoso.onmicrosoft.com\n    customer: contoso\n"
        f"    auth:\n      {auth}\n"
    )
    with pytest.raises(ConfigError, match=fragment):
        load_all(write_cfg(tmp_path, SETTINGS_CERT, tenants), KNOWN)


def test_report_period_and_onedrive_source(tmp_path: Path) -> None:
    settings = SETTINGS_CERT + "reports:\n  period: D30\nonedrive:\n  source: auto\n"
    tenants = (
        SECRET_TENANT + "    reports:\n      period: D90\n    onedrive:\n      source: quota\n"
    )
    tenants += (
        "  - id: 22222222-2222-2222-2222-222222222222\n"
        "    name: fabrikam.onmicrosoft.com\n    customer: fabrikam\n"
    )
    cfg = load_all(write_cfg(tmp_path, settings, tenants), KNOWN)
    contoso, fabrikam = cfg.tenants
    assert report_period(cfg.settings, contoso) == "D90"
    assert report_period(cfg.settings, fabrikam) == "D30"
    assert onedrive_source(cfg.settings, contoso) == "quota"
    assert onedrive_source(cfg.settings, fabrikam) == "auto"
    with pytest.raises(ConfigError, match=r"reports.period"):
        load_all(write_cfg(tmp_path, SETTINGS_CERT + "reports:\n  period: D14\n", tenants), KNOWN)


def test_write_tenants_keeps_auth_block(tmp_path: Path) -> None:
    cfg = load_all(write_cfg(tmp_path, SETTINGS_CERT, SECRET_TENANT), KNOWN)
    path = write_tenants(tmp_path, cfg.tenants, KNOWN)
    text = path.read_text(encoding="utf-8")
    assert "client_secret: file:/run/secrets/contoso_client_secret" in text
    assert "client_secret_expires: '2027-03-01'" in text
    assert "reports:" not in text  # unset options are not written as nulls
    assert load_all(tmp_path, KNOWN).tenants == cfg.tenants


async def test_hot_reload_rejects_unresolvable_auth(tmp_path: Path) -> None:
    cfg = load_all(write_cfg(tmp_path, SETTINGS_NO_CERT, SECRET_TENANT), KNOWN)
    applied: list[list[TenantConfig]] = []

    async def on_tenants(tenants: list[TenantConfig]) -> None:
        applied.append(tenants)

    async def on_thresholds(_: Thresholds) -> None:
        return None

    watcher = ConfigWatcher(tmp_path, KNOWN, on_tenants, on_thresholds, 1, settings=cfg.settings)
    # A tenant switched to certificate auth, but there is no certificate configured.
    (tmp_path / "tenants.yaml").write_text(
        SECRET_TENANT.replace(
            "method: client_secret\n      client_id: 33333333-3333-3333-3333-333333333333\n"
            "      client_secret: file:/run/secrets/contoso_client_secret\n"
            "      client_secret_expires: 2027-03-01\n",
            "method: certificate\n",
        ),
        encoding="utf-8",
    )
    await watcher.check_once()
    await watcher.check_once()
    assert applied == []  # last good configuration stays


def test_current_examples_show_the_parity_settings(tmp_path: Path) -> None:
    for name in ("settings", "tenants", "thresholds"):
        shutil.copy(EXAMPLES / f"{name}.example.yaml", tmp_path / f"{name}.yaml")
    cfg = load_all(tmp_path, KNOWN)
    assert cfg.settings.reports.period == "D7" and cfg.settings.onedrive.source == "report"
    contoso = cfg.tenants[0]
    assert resolve_auth(cfg.settings, contoso).method == "client_secret"
    values = cfg.thresholds.resolve("contoso")
    assert values["mailbox_size_gb_min"] == 10 and values["onedrive_size_gb_min"] == 1
    assert values["mail_sent_max"] == 800
