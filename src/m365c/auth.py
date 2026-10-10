"""Per-tenant app-only tokens with pluggable auth methods.

Each tenant resolves to one :class:`~m365c.config.ResolvedAuth` (its own ``auth:`` block or
the ``settings.yaml`` default): a method, a client ID and a secret *reference*. Methods:

- ``certificate``   azure-identity ``CertificateCredential`` (PEM or PKCS#12)
- ``client_secret`` azure-identity ``ClientSecretCredential``

More methods (workload identity, federated credentials) plug in by adding an
:class:`AuthMethodHandler` to :data:`AUTH_METHODS`.

Secrets are loaded lazily per tenant, so one tenant's broken reference does not stop the
others. Credentials are cached per tenant under ``(tenant_id, method, client_id, secret
fingerprint)``: a changed reference, a rotated value (a ``file:`` secret's mtime/size is
re-checked at most every :data:`RECHECK_S` seconds) or a hot-reloaded ``auth:`` block yields a
new credential and the old one is closed. azure-identity's in-memory cache does the token
caching. Tokens and secrets never leave memory and are never logged; identity-service errors
are reduced to a known AADSTS code and a fixed description.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar, Protocol

import structlog
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import AzureError, ClientAuthenticationError
from azure.identity.aio import CertificateCredential, ClientSecretCredential
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs12

from m365c.config import ResolvedAuth
from m365c.secrets import SecretBytes, SecretError, file_signature, load_secret, ref_backend

log = structlog.get_logger(__name__)

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
RECHECK_S = 60.0

# AADSTS code -> (reason label, description). Descriptions are fixed text: the identity
# service's own error_description is never logged or re-raised, since it may echo request data.
ENTRA_ERRORS: dict[str, tuple[str, str]] = {
    "7000215": ("invalid_client_secret", "invalid client secret"),
    "7000222": ("secret_expired", "client secret expired"),
    "700016": (
        "app_not_found",
        "application not found in the tenant (wrong client ID or no consent)",
    ),
    "90002": ("tenant_not_found", "tenant not found"),
    "65001": ("consent_missing", "admin consent missing"),
    "700027": ("invalid_certificate", "certificate not accepted (not uploaded to the app?)"),
    "7000112": ("app_disabled", "application is disabled"),
}
_AADSTS = re.compile(r"AADSTS(\d+)")
_OAUTH_ERRORS = ("invalid_client", "unauthorized_client", "invalid_grant", "invalid_request")


class TokenError(Exception):
    """A token could not be obtained for a tenant.

    ``transient`` is True for identity-service outages and network errors (retry later);
    False for real auth failures (an ``AADSTS`` code, an unloadable secret). ``reason`` is a
    short label exported as ``m365_tenant_auth_error{reason}``.
    """

    def __init__(
        self,
        tenant_id: str,
        message: str,
        *,
        transient: bool = False,
        reason: str = "auth_failed",
    ) -> None:
        super().__init__(f"token request for tenant {tenant_id} failed: {message}")
        self.tenant_id = tenant_id
        self.transient = transient
        self.reason = "transient" if transient else reason


def classify_auth_failure(text: str) -> tuple[str, str, bool]:
    """``(reason, safe message, transient)`` for an identity-service error text.

    The returned message is built from the AADSTS code only, never from ``text``.
    """
    match = _AADSTS.search(text)
    if match:
        code = match.group(1)
        reason, description = ENTRA_ERRORS.get(code, ("auth_failed", "authentication failed"))
        return reason, f"{description} (AADSTS{code})", False
    for oauth in _OAUTH_ERRORS:
        if oauth in text:
            return "invalid_client", f"identity service refused the request ({oauth})", False
    return "transient", "identity service error", True


class CertificateError(Exception):
    """The configured certificate cannot be used. The message never contains key material."""


@dataclass(frozen=True)
class CertificateInfo:
    subject: str
    thumbprint: str  # SHA-1, upper-case hex: what the Entra portal shows
    not_before: float
    not_after: float

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else datetime.now(UTC).timestamp()) >= self.not_after


_PEM_KEY = re.compile(rb"-----BEGIN ([A-Z ]*)PRIVATE KEY-----.+?-----END \1PRIVATE KEY-----", re.S)


def inspect_certificate(data: bytes, password: bytes | None = None) -> CertificateInfo:
    """Parse the PEM (key + certificate) or PKCS#12 bundle and check the key matches.

    Called before a credential is built, so a broken certificate fails with a clear error
    instead of failing every token request later.
    """
    key: Any
    certs: list[x509.Certificate]
    if b"-----BEGIN" in data:
        match = _PEM_KEY.search(data)
        if match is None:
            raise CertificateError("PEM contains no private key (need key + certificate)")
        try:
            key = serialization.load_pem_private_key(match.group(0), password)
        except TypeError as exc:
            raise CertificateError(f"private key password problem: {exc}") from None
        except ValueError:
            raise CertificateError("private key could not be loaded (wrong password?)") from None
        try:
            certs = x509.load_pem_x509_certificates(data)
        except ValueError:
            raise CertificateError("PEM contains no certificate (need key + certificate)") from None
    else:
        try:
            key, cert, extra = pkcs12.load_key_and_certificates(data, password)
        except ValueError:
            raise CertificateError("not a PEM or PKCS#12 bundle, or wrong password") from None
        if key is None or cert is None:
            raise CertificateError("PKCS#12 bundle needs both a private key and a certificate")
        certs = [cert, *extra]

    spki = serialization.PublicFormat.SubjectPublicKeyInfo
    key_pub = key.public_key().public_bytes(serialization.Encoding.DER, spki)
    for cert in certs:
        if cert.public_key().public_bytes(serialization.Encoding.DER, spki) == key_pub:
            return CertificateInfo(
                subject=cert.subject.rfc4514_string(),
                thumbprint=cert.fingerprint(hashes.SHA1()).hex().upper(),
                not_before=cert.not_valid_before_utc.timestamp(),
                not_after=cert.not_valid_after_utc.timestamp(),
            )
    raise CertificateError("no certificate in the bundle matches the private key")


def load_app_certificate(
    cert_ref: str, password_ref: str | None
) -> tuple[SecretBytes, SecretBytes | None, CertificateInfo]:
    """Load the certificate (and password) secrets and check them. Raises SecretError or
    CertificateError with a message that names the reference, never the content."""
    cert = load_secret(cert_ref)
    password = load_secret(password_ref) if password_ref else None
    try:
        info = inspect_certificate(cert.get(), password.get() if password else None)
    except CertificateError as exc:
        raise CertificateError(f"{ref_backend(cert_ref)}: certificate unusable: {exc}") from None
    return cert, password, info


# --------------------------------------------------------------------------- auth methods


@dataclass(frozen=True)
class CredentialMaterial:
    """Loaded secret material for one tenant. ``fingerprint`` identifies a rotation."""

    secret: SecretBytes
    password: SecretBytes | None = None
    certificate: CertificateInfo | None = None

    @property
    def fingerprint(self) -> str:
        pw = self.password.fingerprint() if self.password else ""
        return f"{self.secret.fingerprint()}{pw}"


class AuthMethodHandler(Protocol):
    name: ClassVar[str]

    def load(self, auth: ResolvedAuth) -> CredentialMaterial:
        """Load and check the secret. Raises SecretError/CertificateError (no values)."""
        ...

    def credential(
        self, tenant_id: str, auth: ResolvedAuth, material: CredentialMaterial, **kwargs: Any
    ) -> AsyncTokenCredential: ...


class CertificateMethod:
    name: ClassVar[str] = "certificate"

    def load(self, auth: ResolvedAuth) -> CredentialMaterial:
        cert, password, info = load_app_certificate(auth.secret_ref, auth.password_ref)
        return CredentialMaterial(cert, password, info)

    def credential(
        self, tenant_id: str, auth: ResolvedAuth, material: CredentialMaterial, **kwargs: Any
    ) -> AsyncTokenCredential:
        return CertificateCredential(
            tenant_id,
            auth.client_id,
            certificate_data=material.secret.get(),
            password=material.password.get() if material.password else None,
            **kwargs,
        )


class ClientSecretMethod:
    name: ClassVar[str] = "client_secret"

    def load(self, auth: ResolvedAuth) -> CredentialMaterial:
        raw = load_secret(auth.secret_ref).get().strip()  # files usually end with a newline
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            raise SecretError(
                f"{ref_backend(auth.secret_ref)}: client secret is not UTF-8"
            ) from None
        if not raw:
            raise SecretError(f"{ref_backend(auth.secret_ref)}: secret is empty")
        return CredentialMaterial(SecretBytes(raw))

    def credential(
        self, tenant_id: str, auth: ResolvedAuth, material: CredentialMaterial, **kwargs: Any
    ) -> AsyncTokenCredential:
        return ClientSecretCredential(
            tenant_id, auth.client_id, material.secret.get().decode("utf-8"), **kwargs
        )


AUTH_METHODS: dict[str, AuthMethodHandler] = {
    CertificateMethod.name: CertificateMethod(),
    ClientSecretMethod.name: ClientSecretMethod(),
}


# --------------------------------------------------------------------------- providers


class TokenProvider(Protocol):
    async def get_token(self, tenant_id: str) -> str: ...

    async def configure(self, auths: Mapping[str, ResolvedAuth]) -> None:
        """Set the effective auth of every active tenant (startup and hot reload)."""
        ...

    def granted_roles(self, tenant_id: str) -> frozenset[str] | None:
        """Application roles in the tenant's current token; None if unknown."""
        ...

    async def aclose(self) -> None: ...


def roles_from_token(token: str) -> frozenset[str] | None:
    try:
        roles = decode_jwt_claims(token).get("roles")
    except ValueError:
        return None
    if not isinstance(roles, list):
        return frozenset()
    return frozenset(str(r) for r in roles)


@dataclass
class _Entry:
    auth: ResolvedAuth
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    material: CredentialMaterial | None = None
    signature: tuple[int, int] | None = None
    checked_at: float = 0.0
    credential: AsyncTokenCredential | None = None
    key: tuple[str, str, str, str] | None = None
    last_token: str | None = None
    roles: frozenset[str] | None = None
    logged_error: str | None = None


class TenantTokenProvider:
    """Tokens for every configured tenant, each with its own auth method and secret."""

    def __init__(
        self,
        auths: Mapping[str, ResolvedAuth] | None = None,
        *,
        methods: Mapping[str, AuthMethodHandler] | None = None,
        credential_kwargs: Mapping[str, Any] | None = None,
        recheck_s: float = RECHECK_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._methods = dict(methods or AUTH_METHODS)
        self._credential_kwargs = dict(credential_kwargs or {})
        self._recheck_s = recheck_s
        self._clock = clock
        self._entries: dict[str, _Entry] = {
            tid: _Entry(auth) for tid, auth in (auths or {}).items()
        }

    async def configure(self, auths: Mapping[str, ResolvedAuth]) -> None:
        stale: list[AsyncTokenCredential] = []
        for tid in list(self._entries):
            entry = self._entries[tid]
            if auths.get(tid) != entry.auth:
                # Removed, or its auth block changed: re-resolve the secret next time.
                del self._entries[tid]
                if entry.credential is not None:
                    stale.append(entry.credential)
        for tid, auth in auths.items():
            if tid not in self._entries:
                if auth.method not in self._methods:
                    raise ValueError(f"unknown auth method {auth.method!r}")
                self._entries[tid] = _Entry(auth)
        for cred in stale:
            await cred.close()

    def granted_roles(self, tenant_id: str) -> frozenset[str] | None:
        entry = self._entries.get(tenant_id)
        return entry.roles if entry else None

    def methods(self) -> dict[str, str]:
        """tenant_id -> auth method (for display)."""
        return {tid: e.auth.method for tid, e in self._entries.items()}

    def certificates(self) -> list[CertificateInfo]:
        return [
            e.material.certificate
            for e in self._entries.values()
            if e.material is not None and e.material.certificate is not None
        ]

    async def _ready(self, tenant_id: str) -> tuple[_Entry, AsyncTokenCredential]:
        entry = self._entries.get(tenant_id)
        if entry is None:
            raise TokenError(tenant_id, "no auth configured for this tenant", reason="config")
        async with entry.lock:
            auth = entry.auth
            handler = self._methods[auth.method]
            now = self._clock()
            reload = entry.material is None
            if not reload and now - entry.checked_at >= self._recheck_s:
                entry.checked_at = now
                signature = file_signature(auth.secret_ref)
                reload = signature is not None and signature != entry.signature
            if reload:
                signature = file_signature(auth.secret_ref)  # before reading: no lost update
                try:
                    material = await asyncio.to_thread(handler.load, auth)
                except (SecretError, CertificateError) as exc:
                    if entry.logged_error != str(exc):
                        entry.logged_error = str(exc)
                        log.error(
                            "tenant_secret_unavailable",
                            tenant_id=tenant_id,
                            method=auth.method,
                            backend=ref_backend(auth.secret_ref),
                            error=str(exc),
                            hint="retried on the next cycle",
                        )
                    raise TokenError(
                        tenant_id, f"secret unavailable: {exc}", reason="secret_unavailable"
                    ) from None
                entry.material, entry.signature, entry.checked_at = material, signature, now
                entry.logged_error = None
            assert entry.material is not None
            key = (tenant_id, auth.method, auth.client_id, entry.material.fingerprint)
            if entry.key != key or entry.credential is None:
                old = entry.credential
                entry.credential = handler.credential(
                    tenant_id, auth, entry.material, **self._credential_kwargs
                )
                entry.key, entry.last_token = key, None
                if old is not None:
                    await old.close()
                    log.info("credential_rotated", tenant_id=tenant_id, method=auth.method)
            return entry, entry.credential

    async def get_token(self, tenant_id: str) -> str:
        entry, cred = await self._ready(tenant_id)
        try:
            token = await cred.get_token(GRAPH_SCOPE)
        except ClientAuthenticationError as exc:
            reason, message, transient = classify_auth_failure(str(exc.message or ""))
            if not transient:
                entry.material = None  # re-read the secret next time: it may have been fixed
            raise TokenError(tenant_id, message, transient=transient, reason=reason) from None
        except AzureError as exc:  # network errors talking to login.microsoftonline.com
            raise TokenError(tenant_id, type(exc).__name__, transient=True) from None
        except (ValueError, TypeError):  # unreadable key material
            entry.material = None
            raise TokenError(
                tenant_id, f"unusable {entry.auth.method} credential", reason="invalid_credential"
            ) from None
        if token.token != entry.last_token:
            entry.last_token = token.token
            entry.roles = roles_from_token(token.token)
            log.debug(
                "token_acquired",
                tenant_id=tenant_id,
                method=entry.auth.method,
                roles=len(entry.roles) if entry.roles is not None else None,
                token_expires_at=datetime.fromtimestamp(token.expires_on, UTC).isoformat(),
            )
        return token.token

    async def forget(self, tenant_id: str) -> None:
        entry = self._entries.pop(tenant_id, None)
        if entry is not None and entry.credential is not None:
            await entry.credential.close()

    async def aclose(self) -> None:
        entries = list(self._entries.values())
        self._entries.clear()
        for entry in entries:
            if entry.credential is not None:
                await entry.credential.close()


class StaticTokenProvider:
    """Returns a fixed token; for tests and the offline ``--fixtures`` mode. If the token is
    JWT-shaped, its ``roles`` claim drives permission gating like a real token."""

    def __init__(self, token: str = "fake-token") -> None:
        self._token = token
        self._roles = roles_from_token(token)
        self.auths: dict[str, ResolvedAuth] = {}

    async def get_token(self, tenant_id: str) -> str:
        return self._token

    async def configure(self, auths: Mapping[str, ResolvedAuth]) -> None:
        self.auths = dict(auths)

    def granted_roles(self, tenant_id: str) -> frozenset[str] | None:
        return self._roles

    async def aclose(self) -> None:
        return None


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload locally (no signature check; for inspecting our own token)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError) as exc:
        raise ValueError("token is not a JWT") from exc
    if not isinstance(claims, dict):
        raise ValueError("token is not a JWT")
    return claims
