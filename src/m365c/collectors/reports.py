"""Helpers shared by the usage-report collectors (mailbox, mailtraffic)."""

from __future__ import annotations

from collections.abc import Iterator

from m365c.collectors.base import CollectorContext, MetricSample, to_bool

REPORT_PERMISSIONS = ("Reports.Read.All", "ReportSettings.Read.All")
CONCEALED_HELP = {
    "m365_reports_concealed": (
        "1 if the tenant conceals user names in usage reports (per-user report series skipped)"
    ),
}

_WARNED = "concealed_warned"


async def reports_concealed(ctx: CollectorContext) -> bool:
    """Check ``displayConcealedNames``; warn (once per tenant and collector) if on."""
    settings = await ctx.graph.get_json("/admin/reportSettings")
    concealed = bool(settings.get("displayConcealedNames", False))
    if concealed:
        if not ctx.state.get(_WARNED):
            ctx.log.warning(
                "reports_concealed",
                hint=(
                    "Usage reports show concealed user names, so per-user report metrics are "
                    "skipped. In the Microsoft 365 admin center go to Settings > Org settings > "
                    "Services > Reports and clear 'Display concealed user, group, and site names "
                    "in all reports'."
                ),
            )
            ctx.state[_WARNED] = True
    else:
        ctx.state.pop(_WARNED, None)
    return concealed


def concealed_sample(concealed: bool, report: str) -> MetricSample:
    # ``report`` keeps the mailbox and mailtraffic series distinct.
    return MetricSample("m365_reports_concealed", 1.0 if concealed else 0.0, {"report": report})


def live_rows(rows: list[dict[str, str]]) -> Iterator[tuple[str, dict[str, str]]]:
    """Yield ``(upn, row)`` for rows that are not deleted and have a UPN."""
    for row in rows:
        if to_bool(row.get("Is Deleted")):
            continue
        upn = (row.get("User Principal Name") or "").strip()
        if upn:
            yield upn.lower(), row
