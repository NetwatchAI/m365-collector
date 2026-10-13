from __future__ import annotations

import json
from functools import cache
from importlib.resources import files
from typing import Any, ClassVar

from m365c.collectors.base import (
    BaseCollector,
    CollectorContext,
    MetricSample,
    Requirement,
    bool_label,
    to_timestamp,
)
from m365c.collectors.license import LICENSE_READ
from m365c.graph import GraphAuthError, GraphError

BASE_SELECT = (
    "id",
    "userPrincipalName",
    "displayName",
    "department",
    "accountEnabled",
    "assignedLicenses",
    "createdDateTime",
)
USER_READ: Requirement = ("User.Read.All",)
SIGNIN_READ: Requirement = ("AuditLog.Read.All",)
AGENT_USER_TYPE = "#microsoft.graph.agentUser"
AGENT_FILTER = "not isof('microsoft.graph.agentUser')"
# lastSignInDateTime / lastNonInteractiveSignInDateTime record *attempts*, successful or
# not, so a dormant account under password spray would look active. Only the successful
# timestamp says the account was really used.
SIGNIN_FIELD = "lastSuccessfulSignInDateTime"


@cache
def product_names() -> dict[str, str]:
    """skuId -> product display name, from Microsoft's licensing CSV (bundled; regenerate
    with scripts/update_sku_names.py)."""
    data: dict[str, str] = json.loads(
        files("m365c").joinpath("sku_names.json").read_text(encoding="utf-8")
    )
    return data


def is_agent_user(user: dict[str, Any]) -> bool:
    return user.get("@odata.type") == AGENT_USER_TYPE


async def fetch_users(
    ctx: CollectorContext, select: tuple[str, ...], *, server_filter: bool
) -> list[dict[str, Any]]:
    params = {"$select": ",".join(select), "$top": "999"}
    headers: dict[str, str] | None = None
    if server_filter:
        # isof() is an advanced query: needs ConsistencyLevel: eventual and $count=true.
        params["$filter"] = AGENT_FILTER
        params["$count"] = "true"
        headers = {"ConsistencyLevel": "eventual"}
    users = await ctx.graph.get_all("/users", params, headers=headers)
    # Client-side guard as well, for tenants/API versions where the filter isn't accepted.
    return [u for u in users if not is_agent_user(u)]


async def fetch_users_with_fallback(
    ctx: CollectorContext, select: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Try the agent-user filter first; drop it on 400 (remembered per tenant)."""
    if ctx.state.get("server_filter", True):
        try:
            return await fetch_users(ctx, select, server_filter=True)
        except GraphError as exc:
            if exc.status != 400 or isinstance(exc, GraphAuthError):
                raise
            ctx.state["server_filter"] = False
            ctx.log.info("agent_user_filter_unsupported", error=exc.code)
    return await fetch_users(ctx, select, server_filter=False)


class UsersCollector(BaseCollector):
    name = "users"
    default_interval = 6 * 3600.0
    required_permissions = (USER_READ,)
    optional_permissions = (
        (SIGNIN_READ, "last successful sign-in (signInActivity; also needs Entra ID P1)"),
        (LICENSE_READ, "SKU names (skuPartNumber) on m365_user_license instead of SKU IDs"),
    )
    metric_help: ClassVar[dict[str, str]] = {
        "m365_user_info": "User metadata; always 1",
        "m365_user_last_signin_timestamp": (
            "Unix time of the user's most recent successful sign-in (absent if never)"
        ),
        "m365_user_created_timestamp": "Unix time the user account was created",
        "m365_users_total": "Number of users by enabled/licensed state",
        "m365_users_disabled_licensed": "Disabled users that still hold licences",
        "m365_tenant_feature": "1 if an optional tenant feature is available to the collector",
        "m365_user_license": (
            "1 per licence (SKU) assigned to the user; sku_name is the product display name"
        ),
        "m365_user_last_activity_timestamp": (
            "Unix time of the user's last activity: last successful sign-in, or else the "
            "mailbox report's Last Activity Date"
        ),
    }

    async def _sku_names(self, ctx: CollectorContext) -> dict[str, str]:
        """skuId -> skuPartNumber, fetched once per run; empty if not permitted."""
        if not ctx.granted(LICENSE_READ):
            return {}
        try:
            data = await ctx.graph.get_json("/subscribedSkus", {"$select": "skuId,skuPartNumber"})
        except GraphError as exc:  # any failure: fall back to sku=sku_id, don't fail the run
            if not ctx.state.get("sku_warned"):
                ctx.log.warning(
                    "sku_names_unavailable",
                    error=exc.code,
                    hint="needs Organization.Read.All or LicenseAssignment.Read.All; "
                    "m365_user_license uses sku=sku_id",
                )
                ctx.state["sku_warned"] = True
            return {}
        ctx.state.pop("sku_warned", None)
        return {
            str(s["skuId"]).lower(): str(s.get("skuPartNumber") or s["skuId"])
            for s in data.get("value") or []
            if isinstance(s, dict) and s.get("skuId")
        }

    async def _fetch(self, ctx: CollectorContext) -> tuple[list[dict[str, Any]], bool]:
        """Users, and whether signInActivity was available."""
        if not ctx.granted(SIGNIN_READ):
            # Not in the token: don't provoke a 403 every run.
            return await fetch_users_with_fallback(ctx, BASE_SELECT), False
        try:
            return await fetch_users_with_fallback(ctx, (*BASE_SELECT, "signInActivity")), True
        except GraphAuthError as exc:
            # signInActivity needs AuditLog.Read.All and Entra ID P1. Retry without it; if
            # this also fails the error is a real permission problem and propagates.
            users = await fetch_users_with_fallback(ctx, BASE_SELECT)
            if not ctx.state.get("signin_warned"):
                ctx.log.warning(
                    "signin_activity_unavailable",
                    error=exc.code,
                    hint="needs AuditLog.Read.All consent and Entra ID P1 in the client tenant",
                )
                ctx.state["signin_warned"] = True
            return users, False

    async def collect(self, ctx: CollectorContext) -> list[MetricSample]:
        users, signin_available = await self._fetch(ctx)
        if signin_available:
            ctx.state.pop("signin_warned", None)
        sku_names = await self._sku_names(ctx)
        # Fallback for last activity: the mailbox report's Last Activity Date, from the
        # mailbox collector's latest snapshot for this tenant (if it has run).
        mailbox_activity = {
            s.labels["upn"]: s.value
            for s in ctx.peer_samples("mailbox")
            if s.name == "m365_mailbox_last_activity_timestamp" and "upn" in s.labels
        }

        samples: list[MetricSample] = [
            MetricSample(
                "m365_tenant_feature",
                1.0 if signin_available else 0.0,
                {"feature": "signin_activity"},
            )
        ]
        counts = {(e, li): 0 for e in (True, False) for li in (True, False)}
        for user in users:
            upn = str(user.get("userPrincipalName") or "").lower()
            if not upn:
                continue
            enabled = bool(user.get("accountEnabled"))
            licenses = [
                str(lic["skuId"]).lower()
                for lic in user.get("assignedLicenses") or []
                if isinstance(lic, dict) and lic.get("skuId")
            ]
            licensed = bool(licenses)
            counts[(enabled, licensed)] += 1
            samples.append(
                MetricSample(
                    "m365_user_info",
                    1.0,
                    {
                        "upn": upn,
                        "display_name": str(user.get("displayName") or ""),
                        "department": str(user.get("department") or ""),
                        "enabled": bool_label(enabled),
                        "licensed": bool_label(licensed),
                    },
                )
            )
            for sku_id in dict.fromkeys(licenses):
                sku = sku_names.get(sku_id, sku_id)
                samples.append(
                    MetricSample(
                        "m365_user_license",
                        1.0,
                        {
                            "upn": upn,
                            "sku": sku,
                            "sku_id": sku_id,
                            "sku_name": product_names().get(sku_id, sku),
                        },
                    )
                )
            created = to_timestamp(user.get("createdDateTime"))
            if created is not None:
                samples.append(MetricSample("m365_user_created_timestamp", created, {"upn": upn}))
            activity = (user.get("signInActivity") if signin_available else None) or {}
            last = to_timestamp(activity.get(SIGNIN_FIELD))
            if last is not None:
                samples.append(MetricSample("m365_user_last_signin_timestamp", last, {"upn": upn}))
            last_activity = last if last is not None else mailbox_activity.get(upn)
            if last_activity is not None:
                samples.append(
                    MetricSample("m365_user_last_activity_timestamp", last_activity, {"upn": upn})
                )

        for (enabled, licensed), n in counts.items():
            samples.append(
                MetricSample(
                    "m365_users_total",
                    float(n),
                    {"enabled": bool_label(enabled), "licensed": bool_label(licensed)},
                )
            )
        samples.append(MetricSample("m365_users_disabled_licensed", float(counts[(False, True)])))
        return samples
