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
import os
import re
import tempfile
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
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: not valid UTF-8 ({exc.reason} at byte {exc.start})") from exc
    except OSError as exc:  # e.g. a file locked by an editor on Windows
        raise ConfigError(f"{path}: cannot read ({exc.strerror or exc})") from exc
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
    return parse_tenants(_read_yaml(path, required=True), path, known_collectors)


def parse_tenants(
    data: dict[str, Any], path: Path, known_collectors: list[str]
) -> list[TenantConfig]:
    if "tenants" not in data and not os.environ.get("M365C_TENANTS"):
        # An empty or truncated file must not silently drop every tenant.
        raise ConfigError(
            f"{path}: missing 'tenants:' key (file empty or truncated?); "
            "write 'tenants: []' to run with no tenants"
        )
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


def write_tenants(config_dir: Path, tenants: list[TenantConfig], known: list[str]) -> Path:
    """Validate, then atomically replace tenants.yaml (temp file + os.replace), so the
    hot-reload watcher never sees a partial or invalid file. Comments are not kept."""
    path = config_dir / TENANTS_FILE
    data = {"tenants": [t.model_dump(mode="json") for t in tenants]}
    parse_tenants(data, path, known)  # raises ConfigError before anything is written
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tenants-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def load_thresholds(config_dir: Path) -> Thresholds:
    path = config_dir / THRESHOLDS_FILE
    data = _read_yaml(path, required=False)
    try:
        return Thresholds(**data)
    except ValidationError as exc:
        raise ConfigError(_format_validation(path, exc)) from exc


def hot_reload_env_overrides() -> list[str]:
    """M365C_* variables that override the hot-reloaded files (edits to them then appear
    to do nothing, so this is logged at startup)."""
    return sorted(
        k for k in os.environ if k == "M365C_TENANTS" or k.startswith("M365C_THRESHOLDS_")
    )


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
    """Polls tenants.yaml and thresholds.yaml and reloads them on change.

    A change is applied only once the file's (mtime, size) has been stable for one poll,
    so a half-written file from a non-atomic save is not picked up. An invalid or
    unreadable file is logged and ignored: the last good configuration stays active, and
    the watcher itself never dies on an error.
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
        self._applied = {name: self._signature(name) for name in self.FILES}
        self._pending: dict[str, tuple[float, int] | None] = dict.fromkeys(self.FILES)

    def _signature(self, name: str) -> tuple[float, int] | None:
        try:
            st = (self._dir / name).stat()
        except OSError:
            return None
        return (st.st_mtime, st.st_size)

    async def _apply(self, name: str) -> None:
        if name == TENANTS_FILE:
            tenants = load_tenants(self._dir, self._known)
            log.info("config_reloaded", file=name, tenants=len(tenants))
            await self._on_tenants(tenants)
        else:
            thresholds = load_thresholds(self._dir)
            log.info("config_reloaded", file=name)
            await self._on_thresholds(thresholds)

    async def check_once(self) -> None:
        for name in self.FILES:
            current = self._signature(name)
            if current == self._applied[name]:
                self._pending[name] = None
                continue
            if current != self._pending[name]:
                self._pending[name] = current  # changed: wait until it is stable
                continue
            self._applied[name] = current
            self._pending[name] = None
            try:
                await self._apply(name)
            except ConfigError as exc:
                log.error("config_reload_failed", file=name, error=str(exc))
            except Exception as exc:
                log.exception("config_reload_failed", file=name, error=str(exc))

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.check_once()
            except Exception:  # never let hot reload stop for good
                log.exception("config_watcher_error")
