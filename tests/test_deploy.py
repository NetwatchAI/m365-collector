from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from m365c.collectors import all_collectors
from m365c.exporter import BUILTIN_HELP

DEPLOY = Path(__file__).parents[1] / "deploy"
RULES = DEPLOY / "prometheus" / "rules" / "m365.yml"

REQUIRED_ALERTS = {
    "M365LicenseNearlyExhausted", "M365LicenseExhausted", "M365MailboxNearlyFull",
    "M365OneDriveNearlyFull", "M365DisabledUserLicensed", "M365UserInactive",
    "M365HighMailTraffic", "M365ServiceDegraded", "M365AppCredentialExpiring",
    "M365CollectorFailing", "M365CollectorStale", "M365TenantAuthFailed", "M365ReportsConcealed",
    "M365CollectorDown", "M365PermissionMissing", "M365CertificateExpiring",
    "M365ClientSecretExpiring", "M365CollectorSkipped", "M365PermissionRevoked",
    "M365TenantCertificateExpiring",
}  # fmt: skip


def _rules() -> list[dict[str, object]]:
    data = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    return [r for g in data["groups"] for r in g["rules"]]


def test_rules_cover_spec_and_have_summaries() -> None:
    rules = _rules()
    assert {r["alert"] for r in rules} >= REQUIRED_ALERTS
    for rule in rules:
        annotations = rule["annotations"]
        assert isinstance(annotations, dict) and "summary" in annotations, rule["alert"]
        assert "customer" in str(annotations["summary"]) or "customer" in rule.get("labels", {})


def test_rules_reference_only_known_metrics() -> None:
    known = _known_metrics()
    for rule in _rules():
        for name in re.findall(r"\bm365_[a-z0-9_]+", str(rule["expr"])):
            assert name in known, f"{rule['alert']} uses unknown metric {name}"


PROMETHEUS_IMAGE = "prom/prometheus:v3.5.0"
ALERTMANAGER_IMAGE = "prom/alertmanager:v0.28.1"


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


def _run_tool(tool: str, image: str, args: list[str], mount: Path, workdir: str = "/w") -> str:
    """Run promtool/amtool locally if installed, else in its official image via Docker.

    Without either, the test is skipped locally but FAILS when CI is set, so an invalid
    rules file can never pass CI silently (review C1/M11).
    """
    if shutil.which(tool):
        local = [a.replace(workdir, str(mount)) for a in args]
        proc = subprocess.run([tool, *local], capture_output=True, text=True, check=False)
    elif _docker_ok():
        proc = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{mount.as_posix()}:{workdir}:ro", "-w", workdir,
             "--entrypoint", tool, image, *args],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
    elif os.environ.get("CI"):
        pytest.fail(f"{tool} not available (install it or Docker); required in CI")
    else:
        pytest.skip(f"{tool} not installed and Docker not available")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def test_promtool_check_rules() -> None:
    out = _run_tool("promtool", PROMETHEUS_IMAGE, ["check", "rules", "/w/rules/m365.yml"],
                    DEPLOY / "prometheus")  # fmt: skip
    assert "SUCCESS" in out


def test_promtool_check_config() -> None:
    # prometheus.yml points rule_files at /etc/prometheus/rules; mount it there.
    out = _run_tool("promtool", PROMETHEUS_IMAGE, ["check", "config", "/etc/prometheus/prometheus.yml"],
                    DEPLOY / "prometheus", workdir="/etc/prometheus")  # fmt: skip
    assert "SUCCESS" in out


def test_promtool_rule_unit_tests() -> None:
    """Each alert fires (and does not fire) as intended: deploy/prometheus/tests."""
    out = _run_tool("promtool", PROMETHEUS_IMAGE, ["test", "rules", "/w/tests/m365_test.yml"],
                    DEPLOY / "prometheus")  # fmt: skip
    assert "SUCCESS" in out


def test_amtool_check_config() -> None:
    out = _run_tool("amtool", ALERTMANAGER_IMAGE, ["check-config", "/w/alertmanager.yml"],
                    DEPLOY / "alertmanager")  # fmt: skip
    assert "SUCCESS" in out


def test_every_alert_has_a_unit_test() -> None:
    tests = yaml.safe_load(
        (DEPLOY / "prometheus" / "tests" / "m365_test.yml").read_text(encoding="utf-8")
    )
    tested = {t["alertname"] for group in tests["tests"] for t in group.get("alert_rule_test", [])}
    assert {str(r["alert"]) for r in _rules()} <= tested


def test_dashboard_json_and_variables() -> None:
    dash = json.loads(
        (DEPLOY / "grafana" / "dashboards" / "m365-overview.json").read_text(encoding="utf-8")
    )
    assert {v["name"] for v in dash["templating"]["list"]} == {"customer", "tenant"}
    titles = {p["title"] for p in dash["panels"]}
    for expected in ("Licence utilisation per SKU", "Top 10 mailboxes by % used", "Top 10 OneDrives by % used",
                     "Disabled users with licences", "Service health", "Secure score", "Collectors"):  # fmt: skip
        assert expected in titles


def test_dashboard_has_template_parity_row() -> None:
    """The four legacy Zabbix widgets, as tables filtered by the exported thresholds."""
    dash = json.loads(
        (DEPLOY / "grafana" / "dashboards" / "m365-overview.json").read_text(encoding="utf-8")
    )
    titles = [p["title"] for p in dash["panels"]]
    assert "Template parity" in titles
    parity = {
        "Disabled users with licenses – ordered by last activity": "m365_user_last_activity_timestamp",
        "Mailbox activity by size": 'check="mailbox_size_gb_min"',
        "Emails sent/received – last N days (above thresholds)": 'check="mail_sent_max"',
        "OneDrive activity by size (GB)": 'check="onedrive_size_gb_min"',
    }
    for title, needle in parity.items():
        panel = next(p for p in dash["panels"] if p["title"] == title)
        assert panel["type"] == "table"
        exprs = " ".join(t["expr"] for t in panel["targets"])
        assert needle in exprs and "$customer" in exprs and "$tenant" in exprs


def _known_metrics() -> set[str]:
    known = set(BUILTIN_HELP) | {"m365_graph_requests_total", "m365_graph_throttled_total"}
    for c in all_collectors().values():
        known |= set(c.metric_help)
    return known


def test_dashboard_has_available_license_count() -> None:
    """The Zabbix "M365 Available License Count" widget: per tenant and SKU, fewest first."""
    dash = json.loads(
        (DEPLOY / "grafana" / "dashboards" / "m365-overview.json").read_text(encoding="utf-8")
    )
    panels = dash["panels"]
    assert len({p["id"] for p in panels}) == len(panels)
    titles = [p["title"] for p in panels]
    idx = titles.index("M365 Available License Count")
    assert titles.index("Licences") < idx < titles.index("Storage")  # in the Licences row
    panel = panels[idx]
    assert panel["type"] == "stat"  # one card per tenant and SKU, like the Zabbix widget
    (target,) = panel["targets"]
    expr = target["expr"]
    assert expr.startswith("sort(")  # fewest available first
    assert "m365_license_available" in expr and "$customer" in expr and "$tenant" in expr
    assert "(m365_license_total" in expr and "> 0)" in expr  # free SKUs hidden
    assert expr.endswith(") != 0)")  # SKUs with 0 available hidden, negatives kept
    assert "text" not in panel["options"]  # auto-sized: fixed sizes overflowed the cards
    assert target["legendFormat"] == "{{tenant}} · {{sku_name}}"
    for name in re.findall(r"m365_[a-z0-9_]+", expr):
        assert name in _known_metrics(), name
    defaults = panel["fieldConfig"]["defaults"]
    assert defaults["unit"] == "none" and defaults["decimals"] == 0
    steps = [(s["color"], s["value"]) for s in defaults["thresholds"]["steps"]]
    assert steps == [("red", None), ("orange", 1), ("green", 6)]
    # Laid out below the other Licences panels, without overlapping anything.
    boxes = [p["gridPos"] for p in panels]
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            assert not (
                a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
                and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]
            ), (a, b)  # fmt: skip


def test_compose_has_no_inline_secrets() -> None:
    compose = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
    assert set(compose["services"]) >= {
        "collector",
        "prometheus",
        "alertmanager",
        "grafana",
        "loki",
        "alloy",
    }
    grafana_env = compose["services"]["grafana"]["environment"]
    assert "GF_SECURITY_ADMIN_PASSWORD__FILE" in grafana_env
    assert "GF_SECURITY_ADMIN_PASSWORD" not in grafana_env
    for secret in compose["secrets"].values():
        assert set(secret) == {"file"}
    am = (DEPLOY / "alertmanager" / "alertmanager.yml").read_text(encoding="utf-8")
    assert "url_file" in am and "smtp_auth_password_file" in am


def test_dashboard_queries_parse(tmp_path: Path) -> None:
    """Every Prometheus query in the dashboard is valid PromQL: they are written into a
    throwaway rule file (one recording rule each) and checked with promtool."""
    dash = json.loads(
        (DEPLOY / "grafana" / "dashboards" / "m365-overview.json").read_text(encoding="utf-8")
    )
    exprs = [
        t["expr"].replace("$customer", ".*").replace("$tenant", ".*")
        for panel in dash["panels"]
        for t in panel.get("targets", [])
        if t.get("datasource", {}).get("type") == "prometheus"
    ]
    assert len(exprs) > 15
    rules = {"groups": [{"name": "dashboard", "rules": [
        {"record": f"dashboard:query_{i}", "expr": e} for i, e in enumerate(exprs)
    ]}]}  # fmt: skip
    (tmp_path / "dashboard.yml").write_text(yaml.safe_dump(rules), encoding="utf-8")
    out = _run_tool("promtool", PROMETHEUS_IMAGE, ["check", "rules", "/w/dashboard.yml"], tmp_path)
    assert "SUCCESS" in out


def test_docker_proxy_is_isolated() -> None:
    """Review 2 L12: only Alloy may reach the Docker API proxy."""
    compose = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert services["docker-proxy"]["networks"] == ["docker-proxy"]
    assert "docker-proxy" in services["alloy"]["networks"]
    others = [n for n, svc in services.items() if n not in ("docker-proxy", "alloy")]
    for name in others:
        assert "docker-proxy" not in (services[name].get("networks") or []), name
    assert compose["networks"]["docker-proxy"]["internal"] is True
