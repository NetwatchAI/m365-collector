from __future__ import annotations

from typing import ClassVar

from m365c.collectors.base import BaseCollector, CollectorContext, MetricSample


def normalize_status(status: str) -> str:
    """The serviceHealthStatus enum is camelCase, but the docs' example (and possibly live
    data) uses PascalCase. Export camelCase so the alert regex sees one spelling."""
    return status[:1].lower() + status[1:] if status else "unknown"


class ServiceHealthCollector(BaseCollector):
    name = "servicehealth"
    default_interval = 300.0
    required_permissions = (("ServiceHealth.Read.All",),)
    metric_help: ClassVar[dict[str, str]] = {
        "m365_service_status": "1 for the service's current health status",
    }

    async def collect(self, ctx: CollectorContext) -> list[MetricSample]:
        overviews = await ctx.graph.get_all(
            "/admin/serviceAnnouncement/healthOverviews", {"$select": "service,status"}
        )
        return [
            MetricSample(
                "m365_service_status",
                1.0,
                {
                    "service": str(o["service"]),
                    "status": normalize_status(str(o.get("status") or "")),
                },
            )
            for o in overviews
            if o.get("service")
        ]
