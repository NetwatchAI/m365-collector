from __future__ import annotations

from typing import ClassVar

from m365c.collectors.base import (
    BaseCollector,
    CollectorContext,
    MetricSample,
    bool_label,
    to_bool,
    to_float,
    to_timestamp,
)
from m365c.collectors.reports import (
    CONCEALED_HELP,
    REPORT_OPTIONAL,
    REPORT_PERMISSIONS,
    deleted_count,
    live_rows,
    report_path,
    report_rows,
)

REPORT = "getMailboxUsageDetail"

_NUMERIC = (
    ("Storage Used (Byte)", "m365_mailbox_used_bytes"),
    ("Prohibit Send Quota (Byte)", "m365_mailbox_quota_bytes"),
    ("Item Count", "m365_mailbox_items"),
)


class MailboxCollector(BaseCollector):
    name = "mailbox"
    default_interval = 12 * 3600.0
    required_permissions = REPORT_PERMISSIONS
    optional_permissions = REPORT_OPTIONAL
    metric_help: ClassVar[dict[str, str]] = {
        **CONCEALED_HELP,
        "m365_mailbox_info": "Mailbox metadata from the usage report; always 1",
        "m365_mailbox_used_bytes": "Mailbox storage used",
        "m365_mailbox_quota_bytes": "Mailbox Prohibit Send quota",
        "m365_mailbox_items": "Items in the mailbox",
        "m365_mailbox_last_activity_timestamp": "Unix time of the mailbox's last activity",
        "m365_mailbox_has_archive": "1 if the mailbox has an archive",
        "m365_mailbox_deleted": "Deleted mailboxes listed in the usage report",
    }

    async def collect(self, ctx: CollectorContext) -> list[MetricSample]:
        path = report_path(REPORT, ctx.options.report_period)
        samples, rows = await report_rows(ctx, self.name, path)
        if rows:
            samples.append(MetricSample("m365_mailbox_deleted", float(deleted_count(rows))))
        for upn, row in live_rows(rows):
            labels = {"upn": upn}
            has_archive = to_bool(row.get("Has Archive"))
            samples.append(
                MetricSample(
                    "m365_mailbox_info",
                    1.0,
                    {
                        "upn": upn,
                        "display_name": (row.get("Display Name") or "").strip(),
                        "has_archive": bool_label(has_archive),
                    },
                )
            )
            for column, metric in _NUMERIC:
                value = to_float(row.get(column))
                if value is not None:
                    samples.append(MetricSample(metric, value, labels))
            last = to_timestamp(row.get("Last Activity Date"))
            if last is not None:
                samples.append(MetricSample("m365_mailbox_last_activity_timestamp", last, labels))
            samples.append(
                MetricSample("m365_mailbox_has_archive", 1.0 if has_archive else 0.0, labels)
            )
        return samples
