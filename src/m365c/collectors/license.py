from __future__ import annotations

from typing import ClassVar

from m365c.collectors.base import BaseCollector, CollectorContext, MetricSample, Requirement

# subscribedSkus: least privileged LicenseAssignment.Read.All, or Organization.Read.All.
LICENSE_READ: Requirement = ("Organization.Read.All", "LicenseAssignment.Read.All")


class LicenseCollector(BaseCollector):
    name = "license"
    default_interval = 3600.0
    required_permissions = (LICENSE_READ,)
    metric_help: ClassVar[dict[str, str]] = {
        "m365_license_total": "Enabled (prepaid) units of the SKU",
        "m365_license_consumed": "Assigned units of the SKU",
        "m365_license_available": "Enabled minus consumed units (negative if over-assigned)",
        "m365_license_suspended": "Suspended units of the SKU",
        "m365_license_warning": "Units of the SKU in warning state",
    }

    async def collect(self, ctx: CollectorContext) -> list[MetricSample]:
        data = await ctx.graph.get_json("/subscribedSkus")
        samples: list[MetricSample] = []
        for sku in data.get("value", []):
            name = str(sku.get("skuPartNumber") or sku.get("skuId") or "unknown")
            prepaid = sku.get("prepaidUnits") or {}
            total = float(prepaid.get("enabled") or 0)
            consumed = float(sku.get("consumedUnits") or 0)
            labels = {"sku": name}
            samples += [
                MetricSample("m365_license_total", total, labels),
                MetricSample("m365_license_consumed", consumed, labels),
                MetricSample("m365_license_available", total - consumed, labels),
                MetricSample(
                    "m365_license_suspended", float(prepaid.get("suspended") or 0), labels
                ),
                MetricSample("m365_license_warning", float(prepaid.get("warning") or 0), labels),
            ]
        return samples
