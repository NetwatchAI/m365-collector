from __future__ import annotations

import json
from functools import cache
from importlib.resources import files
from typing import ClassVar

from m365c.collectors.base import BaseCollector, CollectorContext, MetricSample, Requirement

# subscribedSkus: least privileged LicenseAssignment.Read.All, or Organization.Read.All.
LICENSE_READ: Requirement = ("Organization.Read.All", "LicenseAssignment.Read.All")


@cache
def product_names() -> dict[str, str]:
    """skuId -> product display name, from Microsoft's licensing CSV (bundled; regenerate
    with scripts/update_sku_names.py)."""
    data: dict[str, str] = json.loads(
        files("m365c").joinpath("sku_names.json").read_text(encoding="utf-8")
    )
    return data


class LicenseCollector(BaseCollector):
    name = "license"
    default_interval = 3600.0
    required_permissions = (LICENSE_READ,)
    metric_help: ClassVar[dict[str, str]] = {
        "m365_license_total": (
            "Enabled (prepaid) units of the SKU; sku_name is the product display name"
        ),
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
            # Product display name from the bundled map (as m365_user_license); unknown: sku.
            labels = {"sku": name, "sku_name": product_names().get(str(sku.get("skuId")), name)}
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
