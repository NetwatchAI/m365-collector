"""Configuration models, loading, validation and hot reload.

Three YAML files live in one config directory:

- ``settings.yaml``   process settings (default auth, secrets, HTTP, server, cache, intervals)
- ``tenants.yaml``    client tenants to collect from, optionally with their own auth (hot reloaded)
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
from datetime import date
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal

import structlog
import yaml
from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from m365c.secrets import validate_ref

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
        # The value is not echoed: a client secret pasted into client_id must not leak.
        raise ValueError("not a GUID (expected xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)") from exc


Guid = Annotated[str, BeforeValidator(_validate_guid)]
# A secret *reference* (file:, env:, vault:, keyvault:). Anything else is treated as a probable
# inline secret and rejected without echoing it (hide_input_in_errors on every model).
SecretRef = Annotated[str, AfterValidator(validate_ref)]

AuthMethod = Literal["certificate", "client_secret"]
ReportPeriod = Literal["D7", "D30", "D90", "D180"]
OneDriveSource = Literal["report", "quota", "auto"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


# --------------------------------------------------------------------------- settings


class AppConfig(_Strict):
    """Default auth for tenants without their own ``auth:`` block."""

    method: AuthMethod = "certificate"
    client_id: Guid
    client_secret: SecretRef | None = Field(
        default=None, description="Secret reference of the client secret (method client_secret)"
    )
    client_secret_expires: date | None = None

    @model_validator(mode="after")
    def _method_fields(self) -> AppConfig:
        if self.method == "client_secret" and self.client_secret is None:
            raise ValueError("method client_secret needs app.client_secret (a secret reference)")
        if self.method != "client_secret" and (
            self.client_secret is not None or self.client_secret_expires is not None
        ):
            raise ValueError("client_secret fields are only valid with method client_secret")
        return self


class SecretsConfig(_Strict):
    cert: SecretRef | None = Field(
        default=None,
        description="Secret reference holding the PEM (or PKCS#12) certificate; required only "
        "if some tenant uses certificate auth with the default certificate",
    )
    cert_password: SecretRef | None = Field(
        default=None, description="Optional secret reference for an encrypted key / PFX password"
    )


class ReportsConfig(_Strict):
    period: ReportPeriod = "D7"


class OneDriveConfig(_Strict):
    source: OneDriveSource = "report"


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
        env_prefix="M365C_", env_nested_delimiter="__", extra="forbid", hide_input_in_errors=True
    )

    app: AppConfig
    secrets: SecretsConfig = SecretsConfig()
    reports: ReportsConfig = ReportsConfig()
    onedrive: OneDriveConfig = OneDriveConfig()
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


class TenantAuth(_Strict):
    """Per-tenant auth, overriding ``settings.yaml`` ``app:``. Secrets are references only."""

    method: AuthMethod
    client_id: Guid | None = Field(default=None, description="Default: app.client_id")
    client_secret: SecretRef | None = None
    client_secret_expires: date | None = Field(
        default=None, description="Expiry of the client secret (the app cannot read it itself)"
    )
    cert: SecretRef | None = Field(default=None, description="Default: secrets.cert")
    cert_password: SecretRef | None = None

    @model_validator(mode="after")
    def _method_fields(self) -> TenantAuth:
        if self.method == "client_secret":
            if self.client_secret is None:
                raise ValueError("method client_secret needs client_secret (a secret reference)")
            if self.cert is not None or self.cert_password is not None:
                raise ValueError("cert fields are only valid with method certificate")
        elif self.client_secret is not None or self.client_secret_expires is not None:
            raise ValueError("client_secret fields are only valid with method client_secret")
        return self


class TenantReports(_Strict):
    period: ReportPeriod | None = None


class TenantOneDrive(_Strict):
    source: OneDriveSource | None = None


class TenantConfig(_Strict):
    id: Guid
    name: str = Field(min_length=1, description="Tenant display/domain name, used as label")
    customer: str = Field(description="Customer slug, used as label and for thresholds")
    auth: TenantAuth | None = None
    reports: TenantReports | None = None
    onedrive: TenantOneDrive | None = None
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
    model_config = SettingsConfigDict(
        env_prefix="M365C_", extra="forbid", hide_input_in_errors=True
    )

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


# The mail thresholds apply to the configured report period; the old ``_7d`` keys (named
# after the original fixed period) are still accepted.
_SENT = AliasChoices("mail_sent_max", "mail_sent_max_7d")
_RECEIVED = AliasChoices("mail_received_max", "mail_received_max_7d")


class ThresholdValues(_Strict):
    license_util_warn: float = Field(default=0.95, gt=0)
    storage_used_warn: float = Field(default=0.90, gt=0, le=1)
    inactive_days: float = Field(default=30, gt=0)
    mail_sent_max: float = Field(default=800, ge=0, validation_alias=_SENT)
    mail_received_max: float = Field(default=800, ge=0, validation_alias=_RECEIVED)
    mailbox_size_gb_min: float = Field(default=10, ge=0)
    onedrive_size_gb_min: float = Field(default=1, ge=0)


class ThresholdOverrides(_Strict):
    license_util_warn: float | None = Field(default=None, gt=0)
    storage_used_warn: float | None = Field(default=None, gt=0, le=1)
    inactive_days: float | None = Field(default=None, gt=0)
    mail_sent_max: float | None = Field(default=None, ge=0, validation_alias=_SENT)
    mail_received_max: float | None = Field(default=None, ge=0, validation_alias=_RECEIVED)
    mailbox_size_gb_min: float | None = Field(default=None, ge=0)
    onedrive_size_gb_min: float | None = Field(default=None, ge=0)


class Thresholds(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="M365C_THRESHOLDS_",
        env_nested_delimiter="__",
        extra="forbid",
        hide_input_in_errors=True,
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


# --------------------------------------------------------------------------- resolution


@dataclass(frozen=True)
class ResolvedAuth:
    """Effective auth of one tenant: its own ``auth:`` block or the settings default."""

    method: str
    client_id: str
    secret_ref: str  # client secret or certificate reference, never the value
    password_ref: str | None = None
    client_secret_expires: date | None = None


def resolve_auth(settings: Settings, tenant: TenantConfig) -> ResolvedAuth:
    auth, app = tenant.auth, settings.app
    if auth is not None:
        client_id = auth.client_id or app.client_id
        if auth.method == "client_secret":
            assert auth.client_secret is not None  # model validator
            return ResolvedAuth(
                "client_secret", client_id, auth.client_secret, None, auth.client_secret_expires
            )
        if auth.cert is not None:
            return ResolvedAuth("certificate", client_id, auth.cert, auth.cert_password)
        cert, password = settings.secrets.cert, settings.secrets.cert_password
    elif app.method == "client_secret":
        assert app.client_secret is not None  # model validator
        return ResolvedAuth(
            "client_secret", app.client_id, app.client_secret, None, app.client_secret_expires
        )
    else:
        client_id = app.client_id
        cert, password = settings.secrets.cert, settings.secrets.cert_password
    if cert is None:
        raise ConfigError(
            f"tenant {tenant.name!r} uses certificate auth but settings.yaml has no secrets.cert "
            "(set it, give the tenant auth.cert, or use auth.method: client_secret)"
        )
    return ResolvedAuth("certificate", client_id, cert, password)


def validate_auth(settings: Settings, tenants: list[TenantConfig]) -> dict[str, ResolvedAuth]:
    """Resolve auth for every enabled tenant; ConfigError if one cannot be resolved."""
    return {t.id: resolve_auth(settings, t) for t in tenants if t.enabled}


def uses_default_certificate(settings: Settings, tenants: list[TenantConfig]) -> bool:
    cert = settings.secrets.cert
    return cert is not None and any(
        a.method == "certificate" and a.secret_ref == cert
        for a in validate_auth(settings, tenants).values()
    )


def report_period(settings: Settings, tenant: TenantConfig) -> str:
    if tenant.reports is not None and tenant.reports.period is not None:
        return tenant.reports.period
    return settings.reports.period


def onedrive_source(settings: Settings, tenant: TenantConfig) -> str:
    if tenant.onedrive is not None and tenant.onedrive.source is not None:
        return tenant.onedrive.source
    return settings.onedrive.source


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
    data = {"tenants": [t.model_dump(mode="json", exclude_none=True) for t in tenants]}
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
    tenants = load_tenants(config_dir, known_collectors)
    try:
        validate_auth(settings, tenants)
    except ConfigError as exc:
        raise ConfigError(f"{config_dir / TENANTS_FILE}: {exc}") from None
    return LoadedConfig(settings=settings, tenants=tenants, thresholds=load_thresholds(config_dir))


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
        settings: Settings | None = None,
    ) -> None:
        self._dir = config_dir
        self._settings = settings
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
            if self._settings is not None:
                try:
                    validate_auth(self._settings, tenants)
                except ConfigError as exc:
                    raise ConfigError(f"{self._dir / name}: {exc}") from None
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
