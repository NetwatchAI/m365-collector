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


def table(
    title,
    targets,
    x,
    y,
    w,
    h,
    organize=None,
    unit=None,
    sort=None,
    overrides=None,
    extra=None,
    description=None,
):
    transformations = [{"id": "merge", "options": {}}] if len(targets) > 1 else []
    transformations += extra or []
    if organize:
        transformations.append({"id": "organize", "options": organize})
    if sort:
        transformations.append({"id": "sortBy", "options": {"sort": [sort]}})
    defaults = {"custom": {"align": "auto", "filterable": True}}
    if unit:
        defaults["unit"] = unit
    panel = {
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
    if description:
        panel["description"] = description
    panels.append(panel)


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

# ---------------------------------------------------------------- template parity
# The four widgets of the legacy Zabbix dashboards. Thresholds come from m365_threshold
# (thresholds.yaml), so no data is filtered at collection time.
GIB = 1073741824


def unit_override(name, unit):
    return {
        "matcher": {"id": "byName", "options": name},
        "properties": [{"id": "unit", "value": unit}],
    }


row("Template parity", y)
y += 1
table(
    "Disabled users with licenses – ordered by last activity",
    [
        target(
            f"m365_user_license{{{SEL}}} * on(customer, tenant, upn) group_left(display_name, department)"
            f' m365_user_info{{{SEL},enabled="false",licensed="true"}}',
            instant=True,
            fmt="table",
            ref="A",
        ),
        target(
            f"(m365_user_last_activity_timestamp{{{SEL}}}"
            f" or on(customer, tenant, upn) m365_mailbox_last_activity_timestamp{{{SEL}}}) * 1000"
            f' and on(customer, tenant, upn) m365_user_info{{{SEL},enabled="false",licensed="true"}}',
            instant=True,
            fmt="table",
            ref="B",
        ),
    ],
    0,
    y,
    12,
    10,
    extra=[
        {
            "id": "groupBy",
            "options": {
                "fields": {
                    "customer": {"aggregations": [], "operation": "groupby"},
                    "tenant": {"aggregations": [], "operation": "groupby"},
                    "upn": {"aggregations": [], "operation": "groupby"},
                    "display_name": {"aggregations": [], "operation": "groupby"},
                    "department": {"aggregations": [], "operation": "groupby"},
                    "sku": {"aggregations": ["uniqueValues"], "operation": "aggregate"},
                    "Value #B": {"aggregations": ["max"], "operation": "aggregate"},
                }
            },
        }
    ],
    organize={
        **hide("Value #A"),
        "renameByName": {
            "upn": "UPN",
            "display_name": "display name",
            "sku (uniqueValues)": "licences",
            "Value #B (max)": "last activity",
        },
    },
    sort={"field": "last activity", "desc": False},
    overrides=[unit_override("last activity", "dateTimeAsIso")],
    description="Last activity = last successful sign-in, or the mailbox report's last activity "
    "when sign-in data is unavailable (no AuditLog.Read.All / Entra ID P1).",
)
MAILBOX_BIG = (
    f"m365_mailbox_used_bytes{{{SEL}}} >= on(customer, tenant) group_left()"
    f' (m365_threshold{{{SEL},check="mailbox_size_gb_min"}} * {GIB})'
)
table(
    "Mailbox activity by size",
    [
        target(MAILBOX_BIG, instant=True, fmt="table", ref="A"),
        target(
            f"m365_mailbox_last_activity_timestamp{{{SEL}}} * 1000 and on(customer, tenant, upn) ({MAILBOX_BIG})",
            instant=True,
            fmt="table",
            ref="B",
        ),
        target(
            f"m365_mailbox_info{{{SEL}}} and on(customer, tenant, upn) ({MAILBOX_BIG})",
            instant=True,
            fmt="table",
            ref="C",
        ),
    ],
    12,
    y,
    12,
    10,
    organize={
        **hide("Value #C"),
        "renameByName": {
            "Value #A": "used",
            "Value #B": "last activity",
            "upn": "UPN",
            "display_name": "display name",
            "has_archive": "archive",
        },
    },
    sort={"field": "used", "desc": True},
    overrides=[unit_override("used", "bytes"), unit_override("last activity", "dateTimeAsIso")],
    description="Mailboxes at or above mailbox_size_gb_min (thresholds.yaml).",
)
y += 10
MAIL_HIGH = (
    f"(m365_mail_sent{{{SEL}}} > on(customer, tenant) group_left()"
    f' m365_threshold{{{SEL},check="mail_sent_max"}})'
    f" or on(customer, tenant, upn, period)"
    f" (m365_mail_received{{{SEL}}} > on(customer, tenant) group_left()"
    f' m365_threshold{{{SEL},check="mail_received_max"}})'
)
table(
    "Emails sent/received – last N days (above thresholds)",
    [
        target(
            f"m365_mail_sent{{{SEL}}} and on(customer, tenant, upn, period) ({MAIL_HIGH})",
            instant=True,
            fmt="table",
            ref="A",
        ),
        target(
            f"m365_mail_received{{{SEL}}} and on(customer, tenant, upn, period) ({MAIL_HIGH})",
            instant=True,
            fmt="table",
            ref="B",
        ),
    ],
    0,
    y,
    12,
    10,
    organize={
        **hide(),
        "renameByName": {
            "Value #A": "sent",
            "Value #B": "received",
            "upn": "UPN",
            "period": "report period",
        },
    },
    sort={"field": "sent", "desc": True},
    description="Users above mail_sent_max or mail_received_max over the report period "
    "(reports.period: D7, D30, D90 or D180).",
)
ONEDRIVE_BIG = (
    f"m365_onedrive_used_bytes{{{SEL}}} >= on(customer, tenant) group_left()"
    f' (m365_threshold{{{SEL},check="onedrive_size_gb_min"}} * {GIB})'
)
table(
    "OneDrive activity by size (GB)",
    [
        target(ONEDRIVE_BIG, instant=True, fmt="table", ref="A"),
        target(
            f"m365_onedrive_total_bytes{{{SEL}}} and on(customer, tenant, upn) ({ONEDRIVE_BIG})",
            instant=True,
            fmt="table",
            ref="B",
        ),
        target(
            f"m365_onedrive_used_bytes{{{SEL}}} / (m365_onedrive_total_bytes{{{SEL}}} > 0)"
            f" and on(customer, tenant, upn) ({ONEDRIVE_BIG})",
            instant=True,
            fmt="table",
            ref="C",
        ),
        target(
            f"m365_onedrive_file_count{{{SEL}}} and on(customer, tenant, upn) ({ONEDRIVE_BIG})",
            instant=True,
            fmt="table",
            ref="D",
        ),
        target(
            f"m365_onedrive_last_activity_timestamp{{{SEL}}} * 1000 and on(customer, tenant, upn) ({ONEDRIVE_BIG})",
            instant=True,
            fmt="table",
            ref="E",
        ),
        target(
            f"m365_onedrive_info{{{SEL}}} and on(customer, tenant, upn) ({ONEDRIVE_BIG})",
            instant=True,
            fmt="table",
            ref="F",
        ),
    ],
    12,
    y,
    12,
    10,
    organize={
        **hide("Value #F"),
        "renameByName": {
            "Value #A": "used",
            "Value #B": "allocated",
            "Value #C": "% used",
            "Value #D": "files",
            "Value #E": "last activity",
            "upn": "UPN",
            "display_name": "display name",
        },
    },
    sort={"field": "used", "desc": True},
    overrides=[
        unit_override("used", "bytes"),
        unit_override("allocated", "bytes"),
        unit_override("% used", "percentunit"),
        unit_override("last activity", "dateTimeAsIso"),
    ],
    description="OneDrives at or above onedrive_size_gb_min. File count and last activity come "
    "from the usage report (onedrive.source: report, the default).",
)
y += 10
# The "M365 mailbox inactivity audit" template: report Last Activity Date older than
# inactive_days, plus mailboxes with no activity at all (the template counted a null date as
# inactive). Uses the mailbox report only, so it works without Entra ID P1.
MAILBOX_STALE = (
    f"(time() - m365_mailbox_last_activity_timestamp{{{SEL}}}) / 86400"
    f' > on(customer, tenant) group_left() m365_threshold{{{SEL},check="inactive_days"}}'
)
MAILBOX_NEVER = (
    f"m365_mailbox_info{{{SEL}}} unless on(customer, tenant, upn)"
    f" m365_mailbox_last_activity_timestamp{{{SEL}}}"
)
MAILBOX_INACTIVE = f"({MAILBOX_STALE}) or on(customer, tenant, upn) ({MAILBOX_NEVER})"
table(
    "Mailboxes inactive longer than inactive_days (mailbox report)",
    [
        target(MAILBOX_STALE, instant=True, fmt="table", ref="A"),
        target(
            f"m365_mailbox_used_bytes{{{SEL}}} and on(customer, tenant, upn) ({MAILBOX_INACTIVE})",
            instant=True,
            fmt="table",
            ref="B",
        ),
        target(
            f"m365_mailbox_info{{{SEL}}} and on(customer, tenant, upn) ({MAILBOX_INACTIVE})",
            instant=True,
            fmt="table",
            ref="C",
        ),
    ],
    0,
    y,
    18,
    10,
    organize={
        **hide("Value #C"),
        "renameByName": {
            "Value #A": "days inactive",
            "Value #B": "used",
            "upn": "UPN",
            "display_name": "display name",
            "has_archive": "archive",
        },
    },
    sort={"field": "days inactive", "desc": True},
    overrides=[
        unit_override("used", "bytes"),
        {
            "matcher": {"id": "byName", "options": "days inactive"},
            "properties": [
                {"id": "decimals", "value": 0},
                {
                    "id": "mappings",
                    "value": [
                        {
                            "type": "special",
                            "options": {"match": "null", "result": {"text": "never"}},
                        }
                    ],
                },
            ],
        },
    ],
    description="Mailboxes whose report Last Activity Date is older than inactive_days "
    "(thresholds.yaml), or that have no activity at all ('never'). Works without sign-in data.",
)
stat(
    "Deleted mailboxes in the report",
    f"sum by (tenant) (m365_mailbox_deleted{{{SEL}}})",
    18,
    y,
    w=6,
    h=10,
    legend="{{tenant}}",
    thresholds=BLUE,
)
y += 10


def lic(metric, match=""):
    return f"m365_license_{metric}{{{SEL}{match}}}"


row("Licences", y)
y += 1
bargauge(
    "Licence utilisation per SKU",
    f"{lic('consumed')} / ({lic('total')} > 0)",
    "{{tenant}} · {{sku}}",
    0,
    y,
    12,
    10,
)
table(
    "Licences",
    [
        target(lic("total"), instant=True, fmt="table", ref="A"),
        target(lic("consumed"), instant=True, fmt="table", ref="B"),
        target(lic("available"), instant=True, fmt="table", ref="C"),
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


# The Zabbix "M365 Available License Count" widget (available.licenses[] = total - used per
# discovered SKU). Same queries as "Licences", plus: series from collectors without the
# sku_name label (before the product-name map) show sku instead, and SKUs without prepaid
# units (free/viral, e.g. FLOW_FREE) are hidden. Zabbix listed those too.
def lic_paid(metric):
    unnamed, named = lic(metric, ',sku_name=""'), lic(metric, ',sku_name!=""')
    either = f'(label_replace({unnamed}, "sku_name", "$1", "sku", "(.*)") or {named})'
    return f"{either} and on(customer, tenant, sku) ({lic('total')} > 0)"


# One card per tenant and SKU, like the Zabbix widget; sort() orders the cards fewest first
# (Grafana keeps the order of an instant query). SKUs with exactly 0 available are left out;
# over-assigned (negative) ones stay. Total and consumed: the Licences table.
available_panel = {
    "type": "stat",
    "title": "M365 Available License Count",
    "id": 0,  # set after the panels below, so their IDs stay stable
    "datasource": DS,
    # Tall, so the auto grid gets large cards: Grafana sizes the text to fit each card (fixed
    # sizes overflowed and overlapped once the cards wrapped into several rows).
    "gridPos": {"h": 16, "w": 24, "x": 0, "y": y},
    "targets": [
        target(f"sort(({lic_paid('available')}) != 0)", "{{tenant}} · {{sku_name}}", instant=True)
    ],
    "options": {
        "reduceOptions": {"calcs": ["lastNotNull"], "values": False, "fields": ""},
        "colorMode": "background",
        "graphMode": "none",
        "textMode": "value_and_name",
        "justifyMode": "center",
        "orientation": "auto",
        "wideLayout": True,
        "minVizWidth": 150,
        "minVizHeight": 100,
    },
    "fieldConfig": {
        "defaults": {
            "unit": "none",
            "decimals": 0,
            "thresholds": {
                "mode": "absolute",
                "steps": [
                    {"color": "red", "value": None},
                    {"color": "orange", "value": 1},
                    {"color": "green", "value": 6},
                ],
            },
        },
        "overrides": [],
    },
    "description": "Available = enabled (prepaid) minus consumed units, one card per tenant and "
    "SKU, fewest first; negative means over-assigned. Colours: red ≤ 0, orange 1–5, green > 5. "
    "Hidden: SKUs with exactly 0 available, and SKUs with no enabled units (free/viral). "
    "Total and consumed: the Licences table.",
}
panels.append(available_panel)
y += 16

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
            f"(((time() - m365_user_created_timestamp{{{SEL}}}) / 86400)"
            f' and on(customer, tenant, upn) m365_user_info{{{SEL},enabled="true",licensed="true"}}'
            f" unless on(customer, tenant, upn) m365_user_last_signin_timestamp{{{SEL}}}"
            f' and on(customer, tenant) (m365_tenant_feature{{{SEL},feature="signin_activity"}} == 1))'
            # one row per assigned licence (m365_user_license is 1, so the value stays the age);
            # groupBy below folds them back into one row per user. Series from collectors
            # without the sku_name label (before the product-name map) show sku instead.
            f" * on(customer, tenant, upn) group_right()"
            f' (label_replace(m365_user_license{{{SEL},sku_name=""}}, "sku_name", "$1", "sku", "(.*)")'
            f' or m365_user_license{{{SEL},sku_name!=""}})',
            instant=True,
            fmt="table",
        )
    ],
    0,
    y,
    24,
    8,
    extra=[
        {
            "id": "groupBy",
            "options": {
                "fields": {
                    "customer": {"aggregations": [], "operation": "groupby"},
                    "tenant": {"aggregations": [], "operation": "groupby"},
                    "upn": {"aggregations": [], "operation": "groupby"},
                    "sku_name": {"aggregations": ["uniqueValues"], "operation": "aggregate"},
                    "Value": {"aggregations": ["max"], "operation": "aggregate"},
                }
            },
        }
    ],
    organize={
        **hide(),
        "renameByName": {
            "sku_name (uniqueValues)": "license type",
            "Value (max)": "account age (days)",
        },
    },
    sort={"field": "account age (days)", "desc": True},
)
y += 8

row("Mail", y)
y += 1
bargauge(
    "Top 10 senders (report period)",
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
    "Top 10 receivers (report period)",
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
table(
    "Collectors skipped for a missing Graph permission",
    [target(f"m365_collector_skipped{{{SEL}}} == 1", instant=True, fmt="table")],
    0,
    y,
    14,
    8,
    organize={**hide("Value"), "renameByName": {"permission": "missing (any of, separated by |)"}},
    description="Grant the permission in the client's app registration (admin consent), then: "
    "m365c tenant verify <customer>. Expected collectors that are skipped raise "
    "M365CollectorSkipped.",
)
table(
    "Graph permissions in the tenant's token",
    [target(f"m365_tenant_permission{{{SEL}}}", instant=True, fmt="table")],
    14,
    y,
    10,
    8,
    organize={**hide(), "renameByName": {"Value": "granted"}},
    sort={"field": "granted", "desc": False},
    overrides=[
        {
            "matcher": {"id": "byName", "options": "granted"},
            "properties": [
                {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                {
                    "id": "mappings",
                    "value": [
                        {
                            "type": "value",
                            "options": {
                                "1": {"text": "yes", "color": "green"},
                                "0": {"text": "no", "color": "transparent"},
                            },
                        }
                    ],
                },
            ],
        }
    ],
    description="Roles m365c uses, and broader roles that also satisfy them (e.g. "
    "Directory.Read.All), from the tenant's current token (decoded locally).",
)
y += 8
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

available_panel["id"] = nid()


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
