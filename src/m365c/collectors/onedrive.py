"""OneDrive quota per licensed, enabled user.

Design choice: this collector fetches its own minimal paged user list instead of
depending on the ``users`` collector's run. That keeps every collector independent
(own interval, own failure state, works with ``m365c run --collector onedrive``) at the
cost of one extra paged ``/users`` listing per run.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from m365c.collectors.base import BaseCollector, CollectorContext, MetricSample
from m365c.collectors.users import is_agent_user
from m365c.graph import GraphAuthError, GraphNotFoundError


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

        async def quota(user: dict[str, Any]) -> dict[str, Any] | None:
            try:
                drive = await ctx.graph.get_json(f"/users/{user['id']}/drive", {"$select": "quota"})
            except GraphNotFoundError:
                return None  # OneDrive not provisioned for this user
            q = drive.get("quota")
            return q if isinstance(q, dict) else None

        results = await asyncio.gather(*(quota(u) for u in targets), return_exceptions=True)

        samples: list[MetricSample] = []
        denied: list[GraphAuthError] = []
        not_provisioned = 0
        for user, result in zip(targets, results, strict=True):
            if isinstance(result, GraphAuthError):
                denied.append(result)
                continue
            if isinstance(result, BaseException):
                raise result
            if result is None:
                not_provisioned += 1
                continue
            labels = {"upn": str(user["userPrincipalName"]).lower()}
            for key, metric in (
                ("used", "m365_onedrive_used_bytes"),
                ("total", "m365_onedrive_total_bytes"),
                ("deleted", "m365_onedrive_deleted_bytes"),
            ):
                if result.get(key) is not None:
                    samples.append(MetricSample(metric, float(result[key]), labels))

        if denied and len(denied) == len(targets):
            raise denied[0]  # every drive refused: permission problem, not a per-user quirk
        if denied:
            ctx.log.warning("onedrive_access_denied", users=len(denied), error=denied[0].code)
        if not_provisioned:
            ctx.log.debug("onedrive_not_provisioned", users=not_provisioned)
        return samples
