"""Helpers shared by the usage-report collectors (mailbox, mailtraffic, onedrive report).

Reports are v1.0 CSVs for the tenant's configured period (``D7``/``D30``/``D90``/``D180``).

Concealment is detected two ways and fails *closed*:

1. ``GET /admin/reportSettings`` → ``displayConcealedNames``, when the token holds
   ``ReportSettings.Read.All`` (or the roles are unknown). Both the documented shape
   ``{"value": {"displayConcealedNames": ...}}`` and the flat shape are accepted; a response
   without the property is an error, not "not concealed". A 403 (permission missing) falls
   back to the heuristic and never fails the run.
2. From the data (heuristic): the UPN column has no ``@`` anywhere and most values look like
   32-character hex hashes. Rows whose UPN has no ``@`` are never exported in any case.

``m365_reports_concealed{report, source="setting"|"heuristic"}`` says which one decided.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from m365c.collectors.base import CollectorContext, MetricSample, Requirement, to_bool
from m365c.graph import GraphAuthError, GraphError

REPORT_PERMISSIONS: tuple[Requirement, ...] = (("Reports.Read.All",),)
REPORT_SETTINGS: Requirement = ("ReportSettings.Read.All",)
REPORT_OPTIONAL: tuple[tuple[Requirement, str], ...] = (
    (
        REPORT_SETTINGS,
        "reads the 'concealed names' setting directly (otherwise inferred from report data)",
    ),
)
CONCEALED_HELP = {
    "m365_reports_concealed": (
        "1 if the tenant conceals user names in usage reports (per-user report series skipped)"
    ),
}
UPN = "User Principal Name"

_WARNED = "concealed_warned"
_FORBIDDEN_WARNED = "report_settings_forbidden_warned"
_HEX32 = re.compile(r"^[0-9A-Fa-f]{32}$")
_HINT = (
    "Usage reports show concealed user names, so per-user report metrics are skipped. In "
    "the Microsoft 365 admin center go to Settings > Org settings > Services > Reports and "
    "clear 'Display concealed user, group, and site names in all reports'."
)


def report_path(function: str, period: str) -> str:
    return f"/reports/{function}(period='{period}')"


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


def concealed_sample(concealed: bool, report: str, source: str) -> MetricSample:
    # ``report`` keeps the per-collector series distinct.
    return MetricSample(
        "m365_reports_concealed", 1.0 if concealed else 0.0, {"report": report, "source": source}
    )


def _upn(row: dict[str, str], column: str) -> str:
    return (row.get(column) or "").strip()


def looks_concealed(rows: list[dict[str, str]], column: str = UPN) -> bool:
    """True if no row has a real (``@``) UPN and at least half look like 32-hex-digit
    hashes."""
    upns = [u for row in rows if (u := _upn(row, column))]
    if not upns or any("@" in u for u in upns):
        return False
    return sum(1 for u in upns if _HEX32.match(u)) * 2 >= len(upns)


def live_rows(
    rows: list[dict[str, str]], column: str = UPN
) -> Iterator[tuple[str, dict[str, str]]]:
    """Yield ``(upn, row)`` for rows that are not deleted and carry a real UPN."""
    for row in rows:
        if to_bool(row.get("Is Deleted")):
            continue
        upn = _upn(row, column)
        if "@" in upn:  # hashed (concealed) identifiers are never exported
            yield upn.lower(), row


def deleted_count(rows: list[dict[str, str]]) -> int:
    return sum(1 for row in rows if to_bool(row.get("Is Deleted")))


async def report_rows(
    ctx: CollectorContext, report: str, path: str, column: str = UPN
) -> tuple[list[MetricSample], list[dict[str, str]]]:
    """Common prologue: the concealed sample plus the rows to export (empty if concealed)."""
    source = "heuristic"
    if ctx.granted(REPORT_SETTINGS):
        try:
            if await reports_concealed(ctx):
                return [concealed_sample(True, report, "setting")], []
            source = "setting"
        except GraphAuthError as exc:
            if exc.status != 403:
                raise
            if not ctx.state.get(_FORBIDDEN_WARNED):
                ctx.log.info(
                    "report_settings_unavailable",
                    error=exc.code,
                    hint="ReportSettings.Read.All not granted; concealment inferred from data",
                )
                ctx.state[_FORBIDDEN_WARNED] = True
    rows = await ctx.graph.get_report_csv(path)
    if looks_concealed(rows, column):
        warn_concealed(ctx, "report data")
        return [concealed_sample(True, report, "heuristic")], []
    return [concealed_sample(False, report, source)], rows
