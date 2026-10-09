from __future__ import annotations

from typing import ClassVar

from m365c.collectors.base import BaseCollector, CollectorContext, MetricSample, to_float
from m365c.collectors.reports import (
    CONCEALED_HELP,
    REPORT_PERMISSIONS,
    live_rows,
    report_rows,
)

REPORT = "/reports/getEmailActivityUserDetail(period='D7')"

_COLUMNS = (
    ("Send Count", "m365_mail_sent_7d"),
    ("Receive Count", "m365_mail_received_7d"),
    ("Read Count", "m365_mail_read_7d"),
)


class MailTrafficCollector(BaseCollector):
    name = "mailtraffic"
    default_interval = 12 * 3600.0
    required_permissions = REPORT_PERMISSIONS
    metric_help: ClassVar[dict[str, str]] = {
        **CONCEALED_HELP,
        "m365_mail_sent_7d": "Emails sent by the user over the last 7 days",
        "m365_mail_received_7d": "Emails received by the user over the last 7 days",
        "m365_mail_read_7d": "Emails read by the user over the last 7 days",
    }

    async def collect(self, ctx: CollectorContext) -> list[MetricSample]:
        samples, rows = await report_rows(ctx, self.name, REPORT)
        for upn, row in live_rows(rows):
            labels = {"upn": upn}
            for column, metric in _COLUMNS:
                value = to_float(row.get(column))
                if value is not None:
                    samples.append(MetricSample(metric, value, labels))
        return samples
