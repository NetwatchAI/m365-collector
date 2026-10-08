"""Configuration models, loading, validation and hot reload.

Three YAML files live in one config directory:

- ``settings.yaml``   process settings (app identity, secrets, HTTP, server, cache, intervals)
- ``tenants.yaml``    client tenants to collect from (hot reloaded)
- ``thresholds.yaml`` alert thresholds, with per-customer overrides (hot reloaded)

Every value can be overridden by an environment variable with the ``M365C_`` prefix,
using ``__`` as the nesting delimiter, e.g. ``M365C_HTTP__MAX_CONCURRENCY=10``.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal

import structlog
import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

log = structlog.get_logger(__name__)

SETTINGS_FILE = "settings.yaml"
TENANTS_FILE = "tenants.yaml"
THRESHOLDS_FILE = "thresholds.yaml"

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$")
_DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


class ConfigError(Exception):
    """Raised when configuration is missing or invalid. The message is user facing."""


def parse_duration(value: Any) -> float:
    """Parse ``30s``, ``5m``, ``12h``, ``1d`` or a bare number of seconds."""
    if isinstance(value, bool):
        raise ValueError("duration must be a number or a string like '5m'")
    if isinstance(value, int | float):
        seconds = float(value)
    elif isinstance(value, str):
        match = _DURATION_RE.match(value)
        if not match:
            raise ValueError(f"invalid duration {value!r}; use e.g. '30s', '5m', '12h', '1d'")
        seconds = float(match.group(1)) * _DURATION_UNITS[match.group(2)]
    else:
        raise ValueError("duration must be a number or a string like '5m'")
    if seconds <= 0:
        raise ValueError("duration must be positive")
    return seconds


Duration = Annotated[float, BeforeValidator(parse_duration)]


def _validate_guid(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as exc:
        raise ValueError(f"{value!r} is not a GUID") from exc


Guid = Annotated[str, BeforeValidator(_validate_guid)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- settings


class AppConfig(_Strict):
    client_id: Guid


class SecretsConfig(_Strict):
    cert: str = Field(description="Secret reference holding the PEM (or PKCS#12) certificate")
    cert_password: str | None = Field(
        default=None, description="Optional secret reference for an encrypted key / PFX password"
    )


class HttpConfig(_Strict):
    max_concurrency: int = Field(default=20, ge=1)
    per_tenant_concurrency: int = Field(default=4, ge=1)
    timeout_s: float = Field(default=60, gt=0)
    max_retries: int = Field(default=5, ge=0)


class ServerConfig(_Strict):
    host: str = "0.0.0.0"
    port: int = Field(default=9365, ge=1, le=65535)


class CacheConfig(_Strict):
    persist_dir: Path | None = None


def _yaml_sources(
    settings_cls: type[BaseSettings],
    init_settings: PydanticBaseSettingsSource,
    env_settings: PydanticBaseSettingsSource,
) -> tuple[PydanticBaseSettingsSource, ...]:
    # Env first so that M365C_* overrides values passed in from the YAML file.
    return env_settings, init_settings


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="M365C_", env_nested_delimiter="__", extra="forbid"
    )

    app: AppConfig
    secrets: SecretsConfig
    http: HttpConfig = HttpConfig()
    server: ServerConfig = ServerConfig()
    cache: CacheConfig = CacheConfig()
    intervals: dict[str, Duration] = Field(default_factory=dict)
    reload_interval: Duration = 30.0

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return _yaml_sources(settings_cls, init_settings, env_settings)


# --------------------------------------------------------------------------- tenants

_CUSTOMER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class TenantConfig(_Strict):
    id: Guid
    name: str = Field(min_length=1, description="Tenant display/domain name, used as label")
    customer: str = Field(description="Customer slug, used as label and for thresholds")
    collectors: Literal["all"] | list[str] = "all"
    enabled: bool = True

    @field_validator("customer")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not _CUSTOMER_RE.match(value):
            raise ValueError("customer must be a lowercase slug ([a-z0-9_-], starting alnum)")
        return value

    def enabled_collectors(self, available: list[str]) -> list[str]:
        if self.collectors == "all":
            return list(available)
        return [c for c in available if c in self.collectors]


class TenantsFile(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="M365C_", extra="forbid")

    tenants: list[TenantConfig] = Field(default_factory=list)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return _yaml_sources(settings_cls, init_settings, env_settings)

    @field_validator("tenants")
    @classmethod
    def _unique(cls, tenants: list[TenantConfig]) -> list[TenantConfig]:
        for attr in ("id", "name"):
            seen: set[str] = set()
            for t in tenants:
                value = getattr(t, attr)
                if value in seen:
                    raise ValueError(f"duplicate tenant {attr} {value!r}")
                seen.add(value)
        return tenants


# --------------------------------------------------------------------------- thresholds


class ThresholdValues(_Strict):
    license_util_warn: float = Field(default=0.95, gt=0)
    storage_used_warn: float = Field(default=0.90, gt=0, le=1)
    inactive_days: float = Field(default=30, gt=0)
    mail_sent_max_7d: float = Field(default=800, ge=0)
    mail_received_max_7d: float = Field(default=800, ge=0)


class ThresholdOverrides(_Strict):
    license_util_warn: float | None = Field(default=None, gt=0)
    storage_used_warn: float | None = Field(default=None, gt=0, le=1)
    inactive_days: float | None = Field(default=None, gt=0)
    mail_sent_max_7d: float | None = Field(default=None, ge=0)
    mail_received_max_7d: float | None = Field(default=None, ge=0)


class Thresholds(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="M365C_THRESHOLDS_", env_nested_delimiter="__", extra="forbid"
    )

    defaults: ThresholdValues = ThresholdValues()
    customers: dict[str, ThresholdOverrides] = Field(default_factory=dict)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return _yaml_sources(settings_cls, init_settings, env_settings)

    def resolve(self, customer: str) -> dict[str, float]:
        """Customer override where set, otherwise the default."""
        values = self.defaults.model_dump()
        override = self.customers.get(customer)
        if override is not None:
            values.update(override.model_dump(exclude_none=True))
        return values


# --------------------------------------------------------------------------- loading


def _read_yaml(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigError(f"{path}: file not found")
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def _format_validation(path: Path, exc: ValidationError) -> str:
    lines = [f"{path}: invalid configuration"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def load_settings(config_dir: Path) -> Settings:
    path = config_dir / SETTINGS_FILE
    data = _read_yaml(path, required=True)
    try:
        return Settings(**data)
    except ValidationError as exc:
        raise ConfigError(_format_validation(path, exc)) from exc


def load_tenants(config_dir: Path, known_collectors: list[str]) -> list[TenantConfig]:
    path = config_dir / TENANTS_FILE
    data = _read_yaml(path, required=True)
    try:
        parsed = TenantsFile(**data)
    except ValidationError as exc:
        raise ConfigError(_format_validation(path, exc)) from exc
    for t in parsed.tenants:
        if t.collectors != "all":
            unknown = sorted(set(t.collectors) - set(known_collectors))
            if unknown:
                raise ConfigError(
                    f"{path}: tenant {t.name!r} lists unknown collectors {unknown}; "
                    f"known: {sorted(known_collectors)}"
                )
    return parsed.tenants


def load_thresholds(config_dir: Path) -> Thresholds:
    path = config_dir / THRESHOLDS_FILE
    data = _read_yaml(path, required=False)
    try:
        return Thresholds(**data)
    except ValidationError as exc:
        raise ConfigError(_format_validation(path, exc)) from exc


def validate_intervals(settings: Settings, known_collectors: list[str]) -> None:
    unknown = sorted(set(settings.intervals) - set(known_collectors))
    if unknown:
        raise ConfigError(
            f"settings.yaml: intervals reference unknown collectors {unknown}; "
            f"known: {sorted(known_collectors)}"
        )


@dataclass
class LoadedConfig:
    settings: Settings
    tenants: list[TenantConfig]
    thresholds: Thresholds


def load_all(config_dir: Path, known_collectors: list[str]) -> LoadedConfig:
    settings = load_settings(config_dir)
    validate_intervals(settings, known_collectors)
    return LoadedConfig(
        settings=settings,
        tenants=load_tenants(config_dir, known_collectors),
        thresholds=load_thresholds(config_dir),
    )


# --------------------------------------------------------------------------- hot reload


class ConfigWatcher:
    """Polls tenants.yaml and thresholds.yaml mtimes and reloads them on change.

    An invalid file is logged and ignored: the last good configuration stays active.
    """

    FILES: ClassVar[tuple[str, ...]] = (TENANTS_FILE, THRESHOLDS_FILE)

    def __init__(
        self,
        config_dir: Path,
        known_collectors: list[str],
        on_tenants: Callable[[list[TenantConfig]], Awaitable[None]],
        on_thresholds: Callable[[Thresholds], Awaitable[None]],
        interval: float,
    ) -> None:
        self._dir = config_dir
        self._known = known_collectors
        self._on_tenants = on_tenants
        self._on_thresholds = on_thresholds
        self._interval = interval
        self._mtimes = {name: self._mtime(name) for name in self.FILES}

    def _mtime(self, name: str) -> float | None:
        try:
            return (self._dir / name).stat().st_mtime
        except FileNotFoundError:
            return None

    async def check_once(self) -> None:
        for name in self.FILES:
            current = self._mtime(name)
            if current == self._mtimes[name]:
                continue
            self._mtimes[name] = current
            try:
                if name == TENANTS_FILE:
                    tenants = load_tenants(self._dir, self._known)
                    log.info("config_reloaded", file=name, tenants=len(tenants))
                    await self._on_tenants(tenants)
                else:
                    thresholds = load_thresholds(self._dir)
                    log.info("config_reloaded", file=name)
                    await self._on_thresholds(thresholds)
            except ConfigError as exc:
                log.error("config_reload_failed", file=name, error=str(exc))

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self.check_once()
