"""Collector registry and the Graph permissions the collectors need."""

from __future__ import annotations

from m365c.collectors.appsecrets import AppSecretsCollector
from m365c.collectors.base import (
    BaseCollector,
    CollectorContext,
    MetricSample,
    Requirement,
    TenantOptions,
    accepted_roles,
)
from m365c.collectors.license import LICENSE_READ, LicenseCollector
from m365c.collectors.mailbox import MailboxCollector
from m365c.collectors.mailtraffic import MailTrafficCollector
from m365c.collectors.onedrive import OneDriveCollector
from m365c.collectors.securescore import SecureScoreCollector
from m365c.collectors.servicehealth import ServiceHealthCollector
from m365c.collectors.users import SIGNIN_READ, USER_READ, UsersCollector

__all__ = [
    "PARITY_COLLECTORS",
    "PARITY_PERMISSIONS",
    "BaseCollector",
    "CollectorContext",
    "MetricSample",
    "Requirement",
    "TenantOptions",
    "all_collectors",
    "format_requirement",
    "known_roles",
    "permissions",
]

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

# What the legacy Zabbix templates' app registrations are granted, and all that the
# template-parity collectors need (onedrive in its default ``report`` source).
PARITY_PERMISSIONS: tuple[Requirement, ...] = (
    ("Reports.Read.All",),
    USER_READ,
    SIGNIN_READ,
    LICENSE_READ,
)
PARITY_COLLECTORS: tuple[str, ...] = ("license", "users", "mailbox", "mailtraffic", "onedrive")


def all_collectors() -> dict[str, BaseCollector]:
    return {cls.name: cls() for cls in _CLASSES}


def _groups(cls: type[BaseCollector]) -> list[Requirement]:
    return [*cls.required_permissions, *(g for g, _ in cls.optional_permissions)]


def permissions(names: list[str] | None = None) -> list[str]:
    """Every Graph application role the given (or all) collectors can use: required and
    optional, every alternative of an any-of group."""
    selected = [c for c in _CLASSES if names is None or c.name in names]
    return sorted({p for c in selected for group in _groups(c) for p in group})


def known_roles() -> list[str]:
    """permissions() plus the broader roles that also satisfy them (BROADER_ROLES)."""
    selected = [g for c in _CLASSES for g in _groups(c)]
    return sorted({r for g in selected for r in accepted_roles(g)})


def format_requirement(group: Requirement) -> str:
    return " or ".join(group)
