from __future__ import annotations

import json
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
    known = set(BUILTIN_HELP) | {"m365_graph_requests_total", "m365_graph_throttled_total"}
    for c in all_collectors().values():
        known |= set(c.metric_help)
    import re

    for rule in _rules():
        for name in re.findall(r"\bm365_[a-z0-9_]+", str(rule["expr"])):
            assert name in known, f"{rule['alert']} uses unknown metric {name}"


@pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool not installed")
def test_promtool_check_rules() -> None:
    subprocess.run(["promtool", "check", "rules", str(RULES)], check=True)


def test_dashboard_json_and_variables() -> None:
    dash = json.loads(
        (DEPLOY / "grafana" / "dashboards" / "m365-overview.json").read_text(encoding="utf-8")
    )
    assert {v["name"] for v in dash["templating"]["list"]} == {"customer", "tenant"}
    titles = {p["title"] for p in dash["panels"]}
    for expected in ("Licence utilisation per SKU", "Top 10 mailboxes by % used", "Top 10 OneDrives by % used",
                     "Disabled users with licences", "Service health", "Secure score", "Collectors"):  # fmt: skip
        assert expected in titles


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
