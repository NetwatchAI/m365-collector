"""Collector registry."""

from __future__ import annotations

from m365c.collectors.appsecrets import AppSecretsCollector
from m365c.collectors.base import BaseCollector, CollectorContext, MetricSample
from m365c.collectors.license import LicenseCollector
from m365c.collectors.mailbox import MailboxCollector
from m365c.collectors.mailtraffic import MailTrafficCollector
from m365c.collectors.onedrive import OneDriveCollector
from m365c.collectors.securescore import SecureScoreCollector
from m365c.collectors.servicehealth import ServiceHealthCollector
from m365c.collectors.users import UsersCollector

__all__ = ["BaseCollector", "CollectorContext", "MetricSample", "all_collectors", "permissions"]

_CLASSES: tuple[type[BaseCollector], ...] = (
    LicenseCollector,
    UsersCollector,
    MailboxCollector,
    OneDriveCollector,
    MailTrafficCollector,
    ServiceHealthCollector,
    SecureScoreCollector,
    AppSecretsCollector,
)


def all_collectors() -> dict[str, BaseCollector]:
    return {cls.name: cls() for cls in _CLASSES}


def permissions(names: list[str] | None = None) -> list[str]:
    """Union of Graph application permissions required by the given (or all) collectors."""
    selected = [c for c in _CLASSES if names is None or c.name in names]
    return sorted({p for c in selected for p in c.required_permissions})
