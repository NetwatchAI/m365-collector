from __future__ import annotations

from typing import Any, ClassVar

from m365c.collectors.base import (
    BaseCollector,
    CollectorContext,
    MetricSample,
    bool_label,
    to_timestamp,
)
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
AGENT_USER_TYPE = "#microsoft.graph.agentUser"
AGENT_FILTER = "not isof('microsoft.graph.agentUser')"
# lastSignInDateTime / lastNonInteractiveSignInDateTime record *attempts*, successful or
# not, so a dormant account under password spray would look active. Only the successful
# timestamp says the account was really used.
SIGNIN_FIELD = "lastSuccessfulSignInDateTime"


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
    required_permissions = ("User.Read.All", "AuditLog.Read.All")
    metric_help: ClassVar[dict[str, str]] = {
        "m365_user_info": "User metadata; always 1",
        "m365_user_last_signin_timestamp": (
            "Unix time of the user's most recent successful sign-in (absent if never)"
        ),
        "m365_user_created_timestamp": "Unix time the user account was created",
        "m365_users_total": "Number of users by enabled/licensed state",
        "m365_users_disabled_licensed": "Disabled users that still hold licences",
        "m365_tenant_feature": "1 if an optional tenant feature is available to the collector",
    }

    async def collect(self, ctx: CollectorContext) -> list[MetricSample]:
        signin_available = True
        try:
            users = await fetch_users_with_fallback(ctx, (*BASE_SELECT, "signInActivity"))
        except GraphAuthError as exc:
            # signInActivity needs AuditLog.Read.All and Entra ID P1. Retry without it; if
            # this also fails the error is a real permission problem and propagates.
            signin_available = False
            users = await fetch_users_with_fallback(ctx, BASE_SELECT)
            if not ctx.state.get("signin_warned"):
                ctx.log.warning(
                    "signin_activity_unavailable",
                    error=exc.code,
                    hint="needs AuditLog.Read.All consent and Entra ID P1 in the client tenant",
                )
                ctx.state["signin_warned"] = True
        if signin_available:
            ctx.state.pop("signin_warned", None)

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
            licensed = bool(user.get("assignedLicenses"))
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
            created = to_timestamp(user.get("createdDateTime"))
            if created is not None:
                samples.append(MetricSample("m365_user_created_timestamp", created, {"upn": upn}))
            activity = user.get("signInActivity") or {}
            last = to_timestamp(activity.get(SIGNIN_FIELD))
            if last is not None:
                samples.append(MetricSample("m365_user_last_signin_timestamp", last, {"upn": upn}))

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
