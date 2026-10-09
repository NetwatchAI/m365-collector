"""OneDrive quota per licensed, enabled user.

Design choice: this collector fetches its own minimal paged user list instead of
depending on the ``users`` collector's run. That keeps every collector independent
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

from m365c.collectors.base import BaseCollector, CollectorContext, MetricSample
from m365c.collectors.users import is_agent_user
from m365c.graph import GraphAuthError, GraphError, GraphNotFoundError

MAX_ERROR_RATIO = 0.10


class OneDriveCollector(BaseCollector):
    name = "onedrive"
    default_interval = 6 * 3600.0
    required_permissions = ("User.Read.All", "Files.Read.All")
    metric_help: ClassVar[dict[str, str]] = {
        "m365_onedrive_used_bytes": "OneDrive storage used",
        "m365_onedrive_total_bytes": "OneDrive storage quota",
        "m365_onedrive_deleted_bytes": "OneDrive storage used by the recycle bin",
    }

    async def collect(self, ctx: CollectorContext) -> list[MetricSample]:
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
        failed = len(denied) + len(errors)
        if targets and failed / len(targets) > MAX_ERROR_RATIO:
            first = (errors or denied)[0]
            raise GraphError(
                first.status,
                first.code,
                f"{failed} of {len(targets)} drives failed (first: {first.message})",
            )
        if failed:
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
