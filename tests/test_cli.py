from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from m365c.cli import app
from m365c.collectors import permissions

from .conftest import CONFIG_FIXTURES, GRAPH_FIXTURES

runner = CliRunner()


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    target = tmp_path / "config"
    shutil.copytree(CONFIG_FIXTURES, target)
    return target


def invoke(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args))
    return result.exit_code, result.output


def test_permissions() -> None:
    code, output = invoke("permissions")
    assert code == 0
    assert output.split() == permissions()


def test_run_dry_run_against_fixtures_json(cfg_dir: Path) -> None:
    code, output = invoke(
        "-c", str(cfg_dir), "run", "--tenant", "contoso", "--dry-run", "--json",
        "--fixtures", str(GRAPH_FIXTURES),
    )  # fmt: skip
    assert code == 0, output
    results = json.loads(output)
    assert {r["collector"] for r in results} == {
        "license", "users", "mailbox", "onedrive", "mailtraffic",
        "servicehealth", "securescore", "appsecrets",
    }  # fmt: skip
    assert all(r["ok"] for r in results)
    lic = next(r for r in results if r["collector"] == "license")
    assert {"name": "m365_license_total", "value": 25.0, "labels": {"sku": "SPB"}} in lic["samples"]


def test_run_table_single_collector(cfg_dir: Path) -> None:
    code, output = invoke(
        "-c", str(cfg_dir), "run", "--tenant", "11111111-1111-1111-1111-111111111111",
        "--collector", "securescore", "--dry-run", "--fixtures", str(GRAPH_FIXTURES),
    )  # fmt: skip
    assert code == 0, output
    assert "m365_securescore_current" in output and "42.5" in output
    assert "m365_license_total" not in output


def test_run_errors(cfg_dir: Path) -> None:
    code, output = invoke(
        "-c", str(cfg_dir), "run", "--tenant", "nobody", "--fixtures", str(GRAPH_FIXTURES)
    )
    assert code == 2 and "no tenant matches" in output
    code, output = invoke(
        "-c",
        str(cfg_dir),
        "run",
        "--tenant",
        "contoso",
        "--collector",
        "bogus",
        "--fixtures",
        str(GRAPH_FIXTURES),
    )
    assert code == 2 and "unknown collectors" in output


def test_run_without_fixtures_reports_missing_cert(cfg_dir: Path) -> None:
    code, output = invoke("-c", str(cfg_dir), "run", "--tenant", "contoso", "--dry-run")
    assert code == 2
    assert "file not found" in output


def test_tenant_list(cfg_dir: Path) -> None:
    code, output = invoke("-c", str(cfg_dir), "tenant", "list")
    assert code == 0
    assert "contoso.onmicrosoft.com" in output


def test_tenant_add(cfg_dir: Path) -> None:
    code, output = invoke(
        "-c", str(cfg_dir), "tenant", "add",
        "--tenant-id", "22222222-2222-2222-2222-222222222222",
        "--name", "fabrikam.onmicrosoft.com", "--customer", "fabrikam",
    )  # fmt: skip
    assert code == 0, output
    assert (
        "https://login.microsoftonline.com/22222222-2222-2222-2222-222222222222/adminconsent"
        "?client_id=00000000-0000-0000-0000-000000000000"
    ) in output.replace("\n", "")
    data = yaml.safe_load((cfg_dir / "tenants.yaml").read_text(encoding="utf-8"))
    assert [t["customer"] for t in data["tenants"]] == ["contoso", "fabrikam"]

    code, output = invoke(
        "-c", str(cfg_dir), "tenant", "add",
        "--tenant-id", "22222222-2222-2222-2222-222222222222",
        "--name", "other.onmicrosoft.com", "--customer", "other",
    )  # fmt: skip
    assert code == 2 and "already configured" in output


def test_tenant_add_creates_file_and_validates(cfg_dir: Path) -> None:
    (cfg_dir / "tenants.yaml").unlink()
    code, output = invoke(
        "-c", str(cfg_dir), "tenant", "add", "--tenant-id", "nope", "--name", "x", "--customer", "x"
    )
    assert code == 2 and "GUID" in output
    code, _ = invoke(
        "-c", str(cfg_dir), "tenant", "add",
        "--tenant-id", "33333333-3333-3333-3333-333333333333", "--name", "x.onmicrosoft.com", "--customer", "x",
    )  # fmt: skip
    assert code == 0
    data = yaml.safe_load((cfg_dir / "tenants.yaml").read_text(encoding="utf-8"))
    assert data["tenants"][0]["id"] == "33333333-3333-3333-3333-333333333333"


def test_tenant_verify_with_fixtures(cfg_dir: Path) -> None:
    code, output = invoke(
        "-c", str(cfg_dir), "tenant", "verify", "contoso", "--fixtures", str(GRAPH_FIXTURES)
    )
    assert code == 0, output
    for check in ("token", "permissions", "reports", "signin"):
        assert check in output
    assert "❌" not in output


def test_tenant_verify_reports_concealed(cfg_dir: Path, tmp_path: Path) -> None:
    graph = tmp_path / "graph"
    shutil.copytree(GRAPH_FIXTURES, graph)
    (graph / "admin_reportSettings.json").write_text(
        '{"displayConcealedNames": true}', encoding="utf-8"
    )
    code, output = invoke(
        "-c", str(cfg_dir), "tenant", "verify", "contoso", "--fixtures", str(graph)
    )
    assert code == 1
    assert "❌" in output and "concealed" in output


def test_tenant_add_twice_from_empty_dir(cfg_dir: Path) -> None:
    """Review H2: the second add used to corrupt tenants.yaml."""
    (cfg_dir / "tenants.yaml").unlink()
    for i, slug in ((3, "x"), (4, "y"), (5, "z")):
        code, output = invoke(
            "-c", str(cfg_dir), "tenant", "add", "--tenant-id", f"{i}" * 8 + "-" + "-".join([f"{i}" * 4] * 3) + "-" + f"{i}" * 12,
            "--name", f"{slug}.onmicrosoft.com", "--customer", slug,
        )  # fmt: skip
        assert code == 0, output
    code, output = invoke("-c", str(cfg_dir), "tenant", "list")
    assert code == 0, output
    data = yaml.safe_load((cfg_dir / "tenants.yaml").read_text(encoding="utf-8"))
    assert [t["customer"] for t in data["tenants"]] == ["x", "y", "z"]
    assert not list(cfg_dir.glob(".tenants-*"))  # temp file cleaned up


def test_tenant_add_never_writes_an_invalid_file(cfg_dir: Path) -> None:
    before = (cfg_dir / "tenants.yaml").read_bytes()
    code, output = invoke(
        "-c", str(cfg_dir), "tenant", "add", "--tenant-id", "44444444-4444-4444-4444-444444444444",
        "--name", "contoso.onmicrosoft.com", "--customer", "dup",
    )  # fmt: skip
    assert code == 2 and "already configured" in output
    assert (cfg_dir / "tenants.yaml").read_bytes() == before


def test_verify_with_bad_certificate_is_a_clean_error(cfg_dir: Path, tmp_path: Path) -> None:
    """Review H3: a garbage certificate used to crash verify with a traceback."""
    cert = tmp_path / "bad.pem"
    cert.write_bytes(b"garbage")
    settings = (cfg_dir / "settings.yaml").read_text(encoding="utf-8")
    settings = settings.replace("file:/nonexistent/m365c_cert", f"file:{cert.as_posix()}")
    (cfg_dir / "settings.yaml").write_text(settings, encoding="utf-8")
    code, output = invoke("-c", str(cfg_dir), "tenant", "verify", "contoso")
    assert code == 2, output
    assert "certificate unusable" in output and "Traceback" not in output
