import json
import sys

DS = {"type": "prometheus", "uid": "prometheus"}
SEL = 'customer=~"$customer",tenant=~"$tenant"'
panels = []
pid = 0


def nid():
    global pid
    pid += 1
    return pid


def target(expr, legend="", instant=False, fmt="time_series", ref="A"):
    t = {"datasource": DS, "expr": expr, "refId": ref, "legendFormat": legend}
    if instant:
        t.update({"instant": True, "range": False, "format": fmt})
    return t


def row(title, y):
    panels.append(
        {
            "type": "row",
            "title": title,
            "id": nid(),
            "collapsed": False,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "panels": [],
        }
    )


def stat(title, expr, x, y, w=6, h=4, unit="short", thresholds=None, legend=""):
    panels.append(
        {
            "type": "stat",
            "title": title,
            "id": nid(),
            "datasource": DS,
            "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "targets": [target(expr, legend, instant=True)],
            "options": {
                "reduceOptions": {"calcs": ["lastNotNull"], "values": False, "fields": ""},
                "colorMode": "background",
                "graphMode": "none",
                "textMode": "value_and_name",
            },
            "fieldConfig": {
                "defaults": {
                    "unit": unit,
                    "thresholds": thresholds
                    or {"mode": "absolute", "steps": [{"color": "green", "value": None}]},
                },
                "overrides": [],
            },
        }
    )


def table(title, targets, x, y, w, h, organize=None, unit=None, sort=None, overrides=None):
    transformations = [{"id": "merge", "options": {}}] if len(targets) > 1 else []
    if organize:
        transformations.append({"id": "organize", "options": organize})
    if sort:
        transformations.append({"id": "sortBy", "options": {"sort": [sort]}})
    defaults = {"custom": {"align": "auto", "filterable": True}}
    if unit:
        defaults["unit"] = unit
    panels.append(
        {
            "type": "table",
            "title": title,
            "id": nid(),
            "datasource": DS,
            "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "targets": targets,
            "transformations": transformations,
            "options": {"showHeader": True, "cellHeight": "sm"},
            "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        }
    )


PCT_STEPS = {
    "mode": "absolute",
    "steps": [
        {"color": "green", "value": None},
        {"color": "orange", "value": 0.85},
        {"color": "red", "value": 0.95},
    ],
}
BLUE = {"mode": "absolute", "steps": [{"color": "blue", "value": None}]}


def bargauge(title, expr, legend, x, y, w, h, unit="percentunit", maxv=1, thresholds=None):
    defaults = {"unit": unit, "min": 0, "thresholds": thresholds or PCT_STEPS}
    if maxv is not None:
        defaults["max"] = maxv
    panels.append(
        {
            "type": "bargauge",
            "title": title,
            "id": nid(),
            "datasource": DS,
            "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "targets": [target(expr, legend, instant=True)],
            "options": {
                "orientation": "horizontal",
                "displayMode": "gradient",
                "showUnfilled": True,
                "reduceOptions": {"calcs": ["lastNotNull"], "values": False, "fields": ""},
            },
            "fieldConfig": {"defaults": defaults, "overrides": []},
        }
    )


def hide(*cols):
    return {"excludeByName": {c: True for c in ("Time", "__name__", "job", "instance", *cols)}}


y = 0
row("Overview", y)
y += 1
stat(
    "Tenant auth OK",
    f"min by (tenant) (m365_tenant_auth_ok{{{SEL}}})",
    0,
    y,
    legend="{{tenant}}",
    thresholds={
        "mode": "absolute",
        "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}],
    },
)
stat(
    "Disabled users with licences",
    f"sum(m365_users_disabled_licensed{{{SEL}}})",
    6,
    y,
    thresholds={
        "mode": "absolute",
        "steps": [{"color": "green", "value": None}, {"color": "orange", "value": 1}],
    },
)
stat(
    "Secure score",
    f"m365_securescore_current{{{SEL}}} / m365_securescore_max{{{SEL}}}",
    12,
    y,
    unit="percentunit",
    legend="{{tenant}}",
    thresholds={
        "mode": "absolute",
        "steps": [
            {"color": "red", "value": None},
            {"color": "orange", "value": 0.4},
            {"color": "green", "value": 0.7},
        ],
    },
)
stat(
    "Failing collectors",
    f"count(m365_collector_success{{{SEL}}} == 0) or vector(0)",
    18,
    y,
    thresholds={
        "mode": "absolute",
        "steps": [{"color": "green", "value": None}, {"color": "red", "value": 1}],
    },
)
y += 4

row("Licences", y)
y += 1
bargauge(
    "Licence utilisation per SKU",
    f"m365_license_consumed{{{SEL}}} / (m365_license_total{{{SEL}}} > 0)",
    "{{tenant}} · {{sku}}",
    0,
    y,
    12,
    10,
)
table(
    "Licences",
    [
        target(f"m365_license_total{{{SEL}}}", instant=True, fmt="table", ref="A"),
        target(f"m365_license_consumed{{{SEL}}}", instant=True, fmt="table", ref="B"),
        target(f"m365_license_available{{{SEL}}}", instant=True, fmt="table", ref="C"),
    ],
    12,
    y,
    12,
    10,
    organize={
        **hide(),
        "renameByName": {"Value #A": "total", "Value #B": "consumed", "Value #C": "available"},
    },
    sort={"field": "available", "desc": False},
)
y += 10

row("Storage", y)
y += 1
bargauge(
    "Top 10 mailboxes by % used",
    f"topk(10, m365_mailbox_used_bytes{{{SEL}}} / (m365_mailbox_quota_bytes{{{SEL}}} > 0))",
    "{{upn}}",
    0,
    y,
    12,
    10,
)
bargauge(
    "Top 10 OneDrives by % used",
    f"topk(10, m365_onedrive_used_bytes{{{SEL}}} / (m365_onedrive_total_bytes{{{SEL}}} > 0))",
    "{{upn}}",
    12,
    y,
    12,
    10,
)
y += 10

row("Users", y)
y += 1
table(
    "Disabled users with licences",
    [target(f'm365_user_info{{{SEL},enabled="false",licensed="true"}}', instant=True, fmt="table")],
    0,
    y,
    12,
    10,
    organize=hide("Value", "enabled", "licensed"),
)
table(
    "Inactive licensed users (days since last sign-in)",
    [
        target(
            f"((time() - m365_user_last_signin_timestamp{{{SEL}}}) / 86400)"
            f' > on(customer, tenant) group_left() m365_threshold{{{SEL},check="inactive_days"}}'
            f' and on(customer, tenant, upn) m365_user_info{{{SEL},enabled="true",licensed="true"}}',
            instant=True,
            fmt="table",
        )
    ],
    12,
    y,
    12,
    10,
    organize={**hide(), "renameByName": {"Value": "days"}},
    sort={"field": "days", "desc": True},
)
y += 10
table(
    "Licensed users who never signed in (days since account creation)",
    [
        target(
            f"((time() - m365_user_created_timestamp{{{SEL}}}) / 86400)"
            f' and on(customer, tenant, upn) m365_user_info{{{SEL},enabled="true",licensed="true"}}'
            f" unless on(customer, tenant, upn) m365_user_last_signin_timestamp{{{SEL}}}"
            f' and on(customer, tenant) (m365_tenant_feature{{{SEL},feature="signin_activity"}} == 1)',
            instant=True,
            fmt="table",
        )
    ],
    0,
    y,
    24,
    8,
    organize={**hide(), "renameByName": {"Value": "account age (days)"}},
    sort={"field": "account age (days)", "desc": True},
)
y += 8

row("Mail", y)
y += 1
bargauge(
    "Top 10 senders (7d)",
    f"topk(10, m365_mail_sent{{{SEL}}})",
    "{{upn}}",
    0,
    y,
    12,
    10,
    unit="short",
    maxv=None,
    thresholds=BLUE,
)
bargauge(
    "Top 10 receivers (7d)",
    f"topk(10, m365_mail_received{{{SEL}}})",
    "{{upn}}",
    12,
    y,
    12,
    10,
    unit="short",
    maxv=None,
    thresholds=BLUE,
)
y += 10

row("Tenant health", y)
y += 1
table(
    "Service health",
    [target(f"m365_service_status{{{SEL}}} == 1", instant=True, fmt="table")],
    0,
    y,
    8,
    10,
    organize=hide("Value"),
    overrides=[
        {
            "matcher": {"id": "byName", "options": "status"},
            "properties": [
                {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                {
                    "id": "mappings",
                    "value": [
                        {
                            "type": "value",
                            "options": {"serviceOperational": {"color": "green", "index": 0}},
                        },
                        {
                            "type": "regex",
                            "options": {"pattern": ".*", "result": {"color": "orange", "index": 1}},
                        },
                    ],
                },
            ],
        }
    ],
)
panels.append(
    {
        "type": "timeseries",
        "title": "Secure score",
        "id": nid(),
        "datasource": DS,
        "gridPos": {"h": 10, "w": 8, "x": 8, "y": y},
        "targets": [
            target(f"m365_securescore_current{{{SEL}}}", "{{tenant}} current", ref="A"),
            target(f"m365_securescore_max{{{SEL}}}", "{{tenant}} max", ref="B"),
        ],
        "fieldConfig": {"defaults": {"unit": "short"}, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
    }
)
table(
    "App credentials expiring within 60 days (negative = expired)",
    [
        target(
            f"(m365_app_credential_expiry_timestamp{{{SEL}}} - time()) / 86400 < 60",
            instant=True,
            fmt="table",
        )
    ],
    16,
    y,
    8,
    10,
    organize={**hide(), "renameByName": {"Value": "days left"}},
    sort={"field": "days left", "desc": False},
)
y += 10

row("Collector health", y)
y += 1
table(
    "Collectors",
    [
        target(f"m365_collector_success{{{SEL}}}", instant=True, fmt="table", ref="A"),
        target(
            f"time() - m365_collector_last_success_timestamp{{{SEL}}}",
            instant=True,
            fmt="table",
            ref="B",
        ),
        target(f"m365_collector_duration_seconds{{{SEL}}}", instant=True, fmt="table", ref="C"),
        target(f"m365_collector_samples{{{SEL}}}", instant=True, fmt="table", ref="D"),
        target(f"m365_collector_permission_ok{{{SEL}}}", instant=True, fmt="table", ref="E"),
    ],
    0,
    y,
    14,
    10,
    organize={
        **hide(),
        "renameByName": {
            "Value #A": "ok",
            "Value #B": "since last success",
            "Value #C": "duration",
            "Value #D": "samples",
            "Value #E": "permission",
        },
    },
    overrides=[
        {
            "matcher": {"id": "byName", "options": "since last success"},
            "properties": [{"id": "unit", "value": "s"}],
        },
        {
            "matcher": {"id": "byName", "options": "duration"},
            "properties": [{"id": "unit", "value": "s"}],
        },
        {
            "matcher": {"id": "byName", "options": "permission"},
            "properties": [
                {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                {
                    "id": "mappings",
                    "value": [
                        {
                            "type": "value",
                            "options": {
                                "1": {"text": "OK", "color": "green"},
                                "0": {"text": "403", "color": "red"},
                            },
                        }
                    ],
                },
            ],
        },
        {
            "matcher": {"id": "byName", "options": "ok"},
            "properties": [
                {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                {
                    "id": "mappings",
                    "value": [
                        {
                            "type": "value",
                            "options": {
                                "1": {"text": "OK", "color": "green"},
                                "0": {"text": "FAIL", "color": "red"},
                            },
                        }
                    ],
                },
            ],
        },
    ],
)
panels.append(
    {
        "type": "timeseries",
        "title": "Graph requests / throttling",
        "id": nid(),
        "datasource": DS,
        "gridPos": {"h": 10, "w": 10, "x": 14, "y": y},
        "targets": [
            target(
                f"sum by (status) (rate(m365_graph_requests_total{{{SEL}}}[5m]))",
                "{{status}}",
                ref="A",
            ),
            target(f"sum(rate(m365_graph_throttled_total{{{SEL}}}[5m]))", "throttled", ref="B"),
        ],
        "fieldConfig": {"defaults": {"unit": "reqps"}, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
    }
)
y += 10
LOKI = {"type": "loki", "uid": "loki"}
panels.append(
    {
        "type": "logs",
        "title": "Collector logs (warnings and errors)",
        "id": nid(),
        "datasource": LOKI,
        "gridPos": {"h": 10, "w": 24, "x": 0, "y": y},
        "targets": [
            {
                "datasource": LOKI,
                "refId": "A",
                "expr": '{service="collector", level=~"warning|error|critical", customer=~"$customer"}',
            }
        ],
        "options": {
            "showTime": True,
            "wrapLogMessage": True,
            "sortOrder": "Descending",
            "enableLogDetails": True,
        },
    }
)


def var(name, query, label):
    return {
        "name": name,
        "label": label,
        "type": "query",
        "datasource": DS,
        "query": {"query": query, "refId": f"{name}-var"},
        "definition": query,
        "refresh": 2,
        "includeAll": True,
        "multi": True,
        "allValue": ".*",
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
        "sort": 1,
    }


dashboard = {
    "uid": "m365-overview",
    "title": "Microsoft 365 overview",
    "tags": ["m365", "netwatch"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 1,
    "editable": False,
    "refresh": "5m",
    "time": {"from": "now-7d", "to": "now"},
    "templating": {
        "list": [
            var("customer", "label_values(m365_threshold, customer)", "Customer"),
            var("tenant", 'label_values(m365_threshold{customer=~"$customer"}, tenant)', "Tenant"),
        ]
    },
    "annotations": {"list": []},
    "panels": panels,
}
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(dashboard, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
print(len(panels), "panels")
