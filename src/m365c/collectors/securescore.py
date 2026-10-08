from __future__ import annotations

from typing import ClassVar

from m365c.collectors.base import BaseCollector, CollectorContext, MetricSample


class SecureScoreCollector(BaseCollector):
    name = "securescore"
    default_interval = 24 * 3600.0
    required_permissions = ("SecurityEvents.Read.All",)
    metric_help: ClassVar[dict[str, str]] = {
        "m365_securescore_current": "Microsoft Secure Score, current points",
        "m365_securescore_max": "Microsoft Secure Score, maximum achievable points",
    }

    async def collect(self, ctx: CollectorContext) -> list[MetricSample]:
        data = await ctx.graph.get_json("/security/secureScores", {"$top": "1"})
        scores = data.get("value") or []
        if not scores:
            ctx.log.info("securescore_empty")
            return []
        latest = scores[0]
        samples = []
        if latest.get("currentScore") is not None:
            samples.append(MetricSample("m365_securescore_current", float(latest["currentScore"])))
        if latest.get("maxScore") is not None:
            samples.append(MetricSample("m365_securescore_max", float(latest["maxScore"])))
        return samples
