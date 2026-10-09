"""Helpers shared by the usage-report collectors (mailbox, mailtraffic).

Concealment is detected two ways and fails *closed*:

1. ``GET /admin/reportSettings`` → ``displayConcealedNames``. Both the documented shape
   ``{"value": {"displayConcealedNames": ...}}`` and the flat shape are accepted; a
   response without the property is an error, not "not concealed".
2. From the data: concealed reports carry hashed IDs instead of UPNs. Rows whose UPN has
   no ``@`` are never exported, and a report where no row has a real UPN is treated as
   concealed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from m365c.collectors.base import CollectorContext, MetricSample, to_bool
from m365c.graph import GraphError

REPORT_PERMISSIONS = ("Reports.Read.All", "ReportSettings.Read.All")
CONCEALED_HELP = {
    "m365_reports_concealed": (
        "1 if the tenant conceals user names in usage reports (per-user report series skipped)"
    ),
}

_WARNED = "concealed_warned"
_HINT = (
    "Usage reports show concealed user names, so per-user report metrics are skipped. In "
    "the Microsoft 365 admin center go to Settings > Org settings > Services > Reports and "
    "clear 'Display concealed user, group, and site names in all reports'."
)


def _concealed_flag(body: dict[str, Any]) -> bool:
    for candidate in (body, body.get("value")):
        if isinstance(candidate, dict) and "displayConcealedNames" in candidate:
            return bool(candidate["displayConcealedNames"])
    raise GraphError(200, "unexpected_response", "reportSettings has no displayConcealedNames")


def warn_concealed(ctx: CollectorContext, source: str) -> None:
    if not ctx.state.get(_WARNED):
        ctx.log.warning("reports_concealed", detected_by=source, hint=_HINT)
        ctx.state[_WARNED] = True


async def reports_concealed(ctx: CollectorContext) -> bool:
    """Check ``displayConcealedNames``; warn (once per tenant and collector) if on."""
    concealed = _concealed_flag(await ctx.graph.get_json("/admin/reportSettings"))
    if concealed:
        warn_concealed(ctx, "reportSettings")
    else:
        ctx.state.pop(_WARNED, None)
    return concealed


def concealed_sample(concealed: bool, report: str) -> MetricSample:
    # ``report`` keeps the mailbox and mailtraffic series distinct.
    return MetricSample("m365_reports_concealed", 1.0 if concealed else 0.0, {"report": report})


def _upn(row: dict[str, str]) -> str:
    return (row.get("User Principal Name") or "").strip()


def looks_concealed(rows: list[dict[str, str]]) -> bool:
    """True if the report has user rows but none with a real (``@``) UPN."""
    upns = [u for row in rows if (u := _upn(row))]
    return bool(upns) and not any("@" in u for u in upns)


def live_rows(rows: list[dict[str, str]]) -> Iterator[tuple[str, dict[str, str]]]:
    """Yield ``(upn, row)`` for rows that are not deleted and carry a real UPN."""
    for row in rows:
        if to_bool(row.get("Is Deleted")):
            continue
        upn = _upn(row)
        if "@" in upn:  # hashed (concealed) identifiers are never exported
            yield upn.lower(), row


async def report_rows(
    ctx: CollectorContext, report: str, path: str
) -> tuple[list[MetricSample], list[dict[str, str]]]:
    """Common prologue: the concealed sample plus the rows to export (empty if concealed)."""
    if await reports_concealed(ctx):
        return [concealed_sample(True, report)], []
    rows = await ctx.graph.get_report_csv(path)
    if looks_concealed(rows):
        warn_concealed(ctx, "report data")
        return [concealed_sample(True, report)], []
    return [concealed_sample(False, report)], rows
