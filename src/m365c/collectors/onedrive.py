"""OneDrive usage per user, from one of two sources (``onedrive.source``, per tenant):

- ``report`` (default): ``getOneDriveUsageAccountDetail(period=...)``, one CSV per tenant,
  ``Reports.Read.All`` only (what the legacy Zabbix template used). Daily data.
- ``quota``: live ``GET /users/{id}/drive?$select=quota`` per licensed, enabled user;
  needs ``User.Read.All`` and ``Files.Read.All``. Adds recycle-bin bytes.
- ``auto``: ``quota`` when the token holds both roles, otherwise ``report``.

Permission gating applies to the effective source.

Quota source design choice: this collector fetches its own minimal paged user list instead
of depending on the ``users`` collector's run. That keeps every collector independent
(own interval, own failure state, works with ``m365c run --collector onedrive``) at the
cost of one extra paged ``/users`` listing per run.

The per-user drive requests run through a worker pool no larger than the tenant's
request concurrency, so a large tenant queues its own work instead of flooding the
shared HTTP client. Per-user failures are tolerated: 404 means OneDrive is not
provisioned; other errors are counted, and the run only fails if every drive was
refused (a permission problem) or more than ``MAX_ERROR_RATIO`` of users failed.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from m365c.collectors.base import (
    BaseCollector,
    CollectorContext,
    MetricSample,
    Requirement,
    TenantOptions,
    satisfied,
    to_float,
    to_timestamp,
)
from m365c.collectors.reports import (
    CONCEALED_HELP,
    REPORT_PERMISSIONS,
    live_rows,
    report_path,
    report_rows,
)
from m365c.collectors.users import USER_READ, is_agent_user
from m365c.graph import GraphAuthError, GraphError, GraphNotFoundError

MAX_ERROR_RATIO = 0.10
REPORT = "getOneDriveUsageAccountDetail"
OWNER = "Owner Principal Name"
FILES_READ: Requirement = ("Files.Read.All",)
QUOTA_PERMISSIONS: tuple[Requirement, ...] = (USER_READ, FILES_READ)

_REPORT_NUMERIC = (
    ("Storage Used (Byte)", "m365_onedrive_used_bytes"),
    ("Storage Allocated (Byte)", "m365_onedrive_total_bytes"),
    ("File Count", "m365_onedrive_file_count"),
)


def effective_source(configured: str, roles: frozenset[str] | None) -> str:
    if configured != "auto":
        return configured
    if roles is not None and all(satisfied(roles, group) for group in QUOTA_PERMISSIONS):
        return "quota"
    return "report"


class OneDriveCollector(BaseCollector):
    name = "onedrive"
    default_interval = 6 * 3600.0
    required_permissions = REPORT_PERMISSIONS  # default source; see requirements()
    optional_permissions = (
        (
            FILES_READ,
            "onedrive.source quota/auto: live quota per user via /drive (with User.Read.All), "
            "incl. recycle-bin bytes",
        ),
    )
    metric_help: ClassVar[dict[str, str]] = {
        **CONCEALED_HELP,
        "m365_onedrive_info": "OneDrive owner metadata from the usage report; always 1",
        "m365_onedrive_used_bytes": "OneDrive storage used",
        "m365_onedrive_total_bytes": "OneDrive storage quota (report: Storage Allocated)",
        "m365_onedrive_deleted_bytes": "OneDrive storage used by the recycle bin (quota source)",
        "m365_onedrive_file_count": "Files in the OneDrive (report source)",
        "m365_onedrive_last_activity_timestamp": (
            "Unix time of the OneDrive's last activity (report source; absent if never)"
        ),
    }

    def requirements(
        self, options: TenantOptions, roles: frozenset[str] | None
    ) -> tuple[Requirement, ...]:
        if effective_source(options.onedrive_source, roles) == "quota":
            return QUOTA_PERMISSIONS
        return REPORT_PERMISSIONS

    async def collect(self, ctx: CollectorContext) -> list[MetricSample]:
        if effective_source(ctx.options.onedrive_source, ctx.roles) == "quota":
            return await self._collect_quota(ctx)
        return await self._collect_report(ctx)

    async def _collect_report(self, ctx: CollectorContext) -> list[MetricSample]:
        path = report_path(REPORT, ctx.options.report_period)
        samples, rows = await report_rows(ctx, self.name, path, OWNER)
        for upn, row in live_rows(rows, OWNER):  # deleted OneDrives are skipped
            labels = {"upn": upn}
            samples.append(
                MetricSample(
                    "m365_onedrive_info",
                    1.0,
                    {"upn": upn, "display_name": (row.get("Owner Display Name") or "").strip()},
                )
            )
            for column, metric in _REPORT_NUMERIC:
                value = to_float(row.get(column))
                if value is not None:
                    samples.append(MetricSample(metric, value, labels))
            last = to_timestamp(row.get("Last Activity Date"))
            if last is not None:
                samples.append(MetricSample("m365_onedrive_last_activity_timestamp", last, labels))
        return samples

    async def _collect_quota(self, ctx: CollectorContext) -> list[MetricSample]:
        users = await ctx.graph.get_all(
            "/users",
            {"$select": "id,userPrincipalName,accountEnabled,assignedLicenses", "$top": "999"},
        )
        targets = [
            u
            for u in users
            if u.get("accountEnabled")
            and u.get("assignedLicenses")
            and u.get("userPrincipalName")
            and not is_agent_user(u)
        ]

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for user in targets:
            queue.put_nowait(user)
        quotas: dict[str, dict[str, Any]] = {}
        denied: list[GraphAuthError] = []
        errors: list[GraphError] = []
        not_provisioned = 0

        async def worker() -> None:
            nonlocal not_provisioned
            while True:
                try:
                    user = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    drive = await ctx.graph.get_json(
                        f"/users/{user['id']}/drive", {"$select": "quota"}
                    )
                except GraphNotFoundError:
                    not_provisioned += 1
                    continue
                except GraphAuthError as exc:
                    denied.append(exc)
                    continue
                except GraphError as exc:
                    errors.append(exc)
                    continue
                quota = drive.get("quota")
                if isinstance(quota, dict):
                    quotas[str(user["userPrincipalName"]).lower()] = quota

        workers = max(1, min(ctx.graph.concurrency, len(targets)))
        await asyncio.gather(*(worker() for _ in range(workers)))

        if targets and len(denied) == len(targets):
            raise denied[0]  # every drive refused: permission problem, not a per-user quirk
        # Per-drive 403s (e.g. a OneDrive the app may not open) are expected quirks, not
        # failures: they are logged but only errors count toward the failure ratio.
        if targets and len(errors) / len(targets) > MAX_ERROR_RATIO:
            first = errors[0]
            raise GraphError(
                first.status,
                first.code,
                f"{len(errors)} of {len(targets)} drives failed (first: {first.message})",
            )
        if denied or errors:
            ctx.log.warning(
                "onedrive_partial_failure",
                users=len(targets),
                denied=len(denied),
                errors=len(errors),
                first_error=(errors or denied)[0].code,
            )
        if not_provisioned:
            ctx.log.debug("onedrive_not_provisioned", users=not_provisioned)

        samples: list[MetricSample] = []
        for upn, quota in quotas.items():
            labels = {"upn": upn}
            for key, metric in (
                ("used", "m365_onedrive_used_bytes"),
                ("total", "m365_onedrive_total_bytes"),
                ("deleted", "m365_onedrive_deleted_bytes"),
            ):
                if quota.get(key) is not None:
                    samples.append(MetricSample(metric, float(quota[key]), labels))
        return samples
