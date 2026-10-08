from __future__ import annotations

import os
from pathlib import Path

import pytest

from m365c.collectors import all_collectors
from m365c.config import (
    ConfigError,
    ConfigWatcher,
    TenantConfig,
    Thresholds,
    load_all,
    load_settings,
    load_tenants,
    load_thresholds,
    parse_duration,
)

from .conftest import CONFIG_FIXTURES

KNOWN = list(all_collectors())


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    for name in ("settings.yaml", "tenants.yaml", "thresholds.yaml"):
        (tmp_path / name).write_bytes((CONFIG_FIXTURES / name).read_bytes())
    return tmp_path


def test_fixture_config_loads(cfg_dir: Path) -> None:
    cfg = load_all(cfg_dir, KNOWN)
    assert cfg.settings.app.client_id == "00000000-0000-0000-0000-000000000000"
    assert cfg.settings.server.port == 9365
    assert cfg.tenants[0].customer == "contoso"


def test_example_configs_are_valid(tmp_path: Path) -> None:
    examples = Path(__file__).parents[1] / "config"
    for name in ("settings", "tenants", "thresholds"):
        (tmp_path / f"{name}.yaml").write_bytes((examples / f"{name}.example.yaml").read_bytes())
    cfg = load_all(tmp_path, KNOWN)
    assert cfg.settings.intervals["mailbox"] == 12 * 3600
    assert len(cfg.tenants) == 2


@pytest.mark.parametrize(
    ("text", "expected"),
    [("30s", 30), ("5m", 300), ("12h", 43200), ("1d", 86400), (90, 90), ("1.5h", 5400)],
)
def test_parse_duration(text: str | int, expected: float) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("bad", ["", "5x", "-1m", 0, True, "m"])
def test_parse_duration_rejects(bad: object) -> None:
    with pytest.raises(ValueError):
        parse_duration(bad)


def test_missing_settings_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"settings.yaml: file not found"):
        load_settings(tmp_path)


def test_invalid_client_id_and_unknown_key(cfg_dir: Path) -> None:
    write(
        cfg_dir / "settings.yaml",
        "app:\n  client_id: not-a-guid\nsecrets:\n  cert: file:/x\nhttp:\n  max_concurency: 3\n",
    )
    with pytest.raises(ConfigError) as info:
        load_settings(cfg_dir)
    message = str(info.value)
    assert "app.client_id" in message and "not a GUID" in message
    assert "http.max_concurency" in message  # typo is reported, not ignored


def test_bad_interval_and_unknown_collector_interval(cfg_dir: Path) -> None:
    write(
        cfg_dir / "settings.yaml",
        "app:\n  client_id: 00000000-0000-0000-0000-000000000000\nsecrets:\n  cert: file:/x\nintervals:\n  mailbox: soon\n",
    )
    with pytest.raises(ConfigError, match=r"intervals.mailbox"):
        load_settings(cfg_dir)
    write(
        cfg_dir / "settings.yaml",
        "app:\n  client_id: 00000000-0000-0000-0000-000000000000\nsecrets:\n  cert: file:/x\nintervals:\n  mailboxes: 1h\n",
    )
    with pytest.raises(ConfigError, match="unknown collectors \\['mailboxes'\\]"):
        load_all(cfg_dir, KNOWN)


def test_invalid_yaml(cfg_dir: Path) -> None:
    write(cfg_dir / "tenants.yaml", "tenants: [\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_tenants(cfg_dir, KNOWN)


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("  - id: nope\n    name: a\n    customer: a\n", "tenants.0.id"),
        (
            "  - id: 11111111-1111-1111-1111-111111111111\n    name: a\n    customer: Not A Slug\n",
            "lowercase slug",
        ),
        (
            "  - id: 11111111-1111-1111-1111-111111111111\n    name: a\n    customer: a\n"
            "  - id: 11111111-1111-1111-1111-111111111111\n    name: b\n    customer: b\n",
            "duplicate tenant id",
        ),
        (
            "  - id: 11111111-1111-1111-1111-111111111111\n    name: a\n    customer: a\n    collectors: [licence]\n",
            "unknown collectors ['licence']",
        ),
    ],
)
def test_tenant_validation_errors(cfg_dir: Path, body: str, fragment: str) -> None:
    write(cfg_dir / "tenants.yaml", "tenants:\n" + body)
    with pytest.raises(ConfigError) as info:
        load_tenants(cfg_dir, KNOWN)
    assert fragment in str(info.value)
    assert "tenants.yaml" in str(info.value)


def test_threshold_validation_error(cfg_dir: Path) -> None:
    write(cfg_dir / "thresholds.yaml", "customers:\n  contoso:\n    storage_used_warn: 5\n")
    with pytest.raises(ConfigError, match=r"customers.contoso.storage_used_warn"):
        load_thresholds(cfg_dir)


def test_threshold_resolution_with_customer_override(cfg_dir: Path) -> None:
    thresholds = load_thresholds(cfg_dir)
    contoso = thresholds.resolve("contoso")
    other = thresholds.resolve("fabrikam")
    assert contoso["storage_used_warn"] == 0.85
    assert other["storage_used_warn"] == 0.90
    assert contoso["license_util_warn"] == other["license_util_warn"] == 0.95
    assert set(contoso) == {
        "license_util_warn", "storage_used_warn", "inactive_days",
        "mail_sent_max_7d", "mail_received_max_7d",
    }  # fmt: skip


def test_thresholds_file_optional(tmp_path: Path) -> None:
    assert load_thresholds(tmp_path).resolve("x")["inactive_days"] == 30


def test_env_overrides_yaml(cfg_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("M365C_HTTP__MAX_CONCURRENCY", "7")
    monkeypatch.setenv("M365C_SERVER__PORT", "9999")
    monkeypatch.setenv("M365C_SECRETS__CERT", "vault:secret/m365c/cert#pem")
    monkeypatch.setenv("M365C_THRESHOLDS_DEFAULTS__INACTIVE_DAYS", "45")
    settings = load_settings(cfg_dir)
    assert settings.http.max_concurrency == 7
    assert settings.http.max_retries == 1  # untouched YAML value survives the merge
    assert settings.server.port == 9999
    assert settings.secrets.cert == "vault:secret/m365c/cert#pem"
    assert load_thresholds(cfg_dir).resolve("contoso")["inactive_days"] == 45


def test_enabled_collectors() -> None:
    t = TenantConfig(
        id="11111111-1111-1111-1111-111111111111",
        name="a",
        customer="a",
        collectors=["users", "license"],
    )
    assert t.enabled_collectors(KNOWN) == ["license", "users"]


async def test_watcher_reloads_and_ignores_invalid(cfg_dir: Path) -> None:
    seen_tenants: list[list[TenantConfig]] = []
    seen_thresholds: list[Thresholds] = []

    async def on_tenants(t: list[TenantConfig]) -> None:
        seen_tenants.append(t)

    async def on_thresholds(t: Thresholds) -> None:
        seen_thresholds.append(t)

    watcher = ConfigWatcher(cfg_dir, KNOWN, on_tenants, on_thresholds, interval=1)
    await watcher.check_once()
    assert not seen_tenants and not seen_thresholds

    def bump(path: Path, text: str) -> None:
        stat = path.stat()
        write(path, text)
        os.utime(path, (stat.st_atime, stat.st_mtime + 10))

    bump(
        cfg_dir / "tenants.yaml",
        "tenants:\n  - id: 22222222-2222-2222-2222-222222222222\n    name: fabrikam.onmicrosoft.com\n    customer: fabrikam\n",
    )
    await watcher.check_once()
    assert [t.customer for t in seen_tenants[-1]] == ["fabrikam"]

    bump(cfg_dir / "tenants.yaml", "tenants: [oops\n")
    await watcher.check_once()
    assert len(seen_tenants) == 1  # invalid file ignored, last good config stays

    bump(cfg_dir / "thresholds.yaml", "defaults:\n  inactive_days: 60\n")
    await watcher.check_once()
    assert seen_thresholds[-1].resolve("x")["inactive_days"] == 60
