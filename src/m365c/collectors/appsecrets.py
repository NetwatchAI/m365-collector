from __future__ import annotations

from typing import ClassVar

from m365c.collectors.base import BaseCollector, CollectorContext, MetricSample, to_timestamp


class AppSecretsCollector(BaseCollector):
    name = "appsecrets"
    default_interval = 24 * 3600.0
    required_permissions = ("Application.Read.All",)
    metric_help: ClassVar[dict[str, str]] = {
        "m365_app_credential_expiry_timestamp": (
            "Unix time when an app registration's client secret or certificate expires"
        ),
    }

    async def collect(self, ctx: CollectorContext) -> list[MetricSample]:
        apps = await ctx.graph.get_all(
            "/applications",
            {"$select": "id,appId,displayName,passwordCredentials,keyCredentials", "$top": "999"},
        )
        samples: list[MetricSample] = []
        for app in apps:
            for field, cred_type in (
                ("passwordCredentials", "password"),
                ("keyCredentials", "certificate"),
            ):
                for cred in app.get(field) or []:
                    expiry = to_timestamp(cred.get("endDateTime"))
                    if expiry is None:
                        continue
                    samples.append(
                        MetricSample(
                            "m365_app_credential_expiry_timestamp",
                            expiry,
                            {
                                "app": str(app.get("displayName") or ""),
                                "app_id": str(app.get("appId") or ""),
                                "credential_type": cred_type,
                                "key_id": str(cred.get("keyId") or ""),
                            },
                        )
                    )
        return samples
