from __future__ import annotations

from typing import ClassVar

from m365c.collectors.base import (
    BaseCollector,
    CollectorContext,
    MetricSample,
    to_bool,
    to_float,
    to_timestamp,
)
from m365c.collectors.reports import (
    CONCEALED_HELP,
    REPORT_PERMISSIONS,
    concealed_sample,
    live_rows,
    reports_concealed,
)

REPORT = "/reports/getMailboxUsageDetail(period='D7')"

_NUMERIC = (
    ("Storage Used (Byte)", "m365_mailbox_used_bytes"),
    ("Prohibit Send Quota (Byte)", "m365_mailbox_quota_bytes"),
    ("Item Count", "m365_mailbox_items"),
)


class MailboxCollector(BaseCollector):
    name = "mailbox"
    default_interval = 12 * 3600.0
    required_permissions = REPORT_PERMISSIONS
    metric_help: ClassVar[dict[str, str]] = {
        **CONCEALED_HELP,
        "m365_mailbox_used_bytes": "Mailbox storage used",
        "m365_mailbox_quota_bytes": "Mailbox Prohibit Send quota",
        "m365_mailbox_items": "Items in the mailbox",
        "m365_mailbox_last_activity_timestamp": "Unix time of the mailbox's last activity",
        "m365_mailbox_has_archive": "1 if the mailbox has an archive",
    }

    async def collect(self, ctx: CollectorContext) -> list[MetricSample]:
        concealed = await reports_concealed(ctx)
        samples = [concealed_sample(concealed, self.name)]
        if concealed:
            return samples
        rows = await ctx.graph.get_report_csv(REPORT)
        for upn, row in live_rows(rows):
            labels = {"upn": upn}
            for column, metric in _NUMERIC:
                value = to_float(row.get(column))
                if value is not None:
                    samples.append(MetricSample(metric, value, labels))
            last = to_timestamp(row.get("Last Activity Date"))
            if last is not None:
                samples.append(MetricSample("m365_mailbox_last_activity_timestamp", last, labels))
            has_archive = 1.0 if to_bool(row.get("Has Archive")) else 0.0
            samples.append(MetricSample("m365_mailbox_has_archive", has_archive, labels))
        return samples
