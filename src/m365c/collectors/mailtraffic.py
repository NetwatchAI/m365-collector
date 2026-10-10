from __future__ import annotations

from typing import ClassVar

from m365c.collectors.base import BaseCollector, CollectorContext, MetricSample, to_float
from m365c.collectors.reports import (
    CONCEALED_HELP,
    REPORT_OPTIONAL,
    REPORT_PERMISSIONS,
    live_rows,
    report_path,
    report_rows,
)

REPORT = "getEmailActivityUserDetail"

# The counts cover the tenant's configured report period, carried in the ``period`` label.
_COLUMNS = (
    ("Send Count", "m365_mail_sent"),
    ("Receive Count", "m365_mail_received"),
    ("Read Count", "m365_mail_read"),
)


class MailTrafficCollector(BaseCollector):
    name = "mailtraffic"
    default_interval = 12 * 3600.0
    required_permissions = REPORT_PERMISSIONS
    optional_permissions = REPORT_OPTIONAL
    metric_help: ClassVar[dict[str, str]] = {
        **CONCEALED_HELP,
        "m365_mail_sent": "Emails sent by the user over the report period (period label)",
        "m365_mail_received": "Emails received by the user over the report period",
        "m365_mail_read": "Emails read by the user over the report period",
    }

    async def collect(self, ctx: CollectorContext) -> list[MetricSample]:
        period = ctx.options.report_period
        samples, rows = await report_rows(ctx, self.name, report_path(REPORT, period))
        for upn, row in live_rows(rows):
            labels = {"upn": upn, "period": period}
            for column, metric in _COLUMNS:
                # Users with no activity have empty or zero counts: keep them as 0 (the
                # Liberty template's fix), so quiet mailboxes are visible too.
                value = to_float(row.get(column))
                samples.append(MetricSample(metric, value if value is not None else 0.0, labels))
        return samples
