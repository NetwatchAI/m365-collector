"""Per-tenant app-only tokens for the single multi-tenant Netwatch app registration.

Every tenant uses the same client ID and certificate; only the tenant ID changes. One
``CertificateCredential`` is created per tenant and reused, so azure-identity's in-memory
token cache does the caching. Tokens never leave memory and are never logged.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog
from azure.core.exceptions import AzureError, ClientAuthenticationError
from azure.identity.aio import CertificateCredential
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs12

from m365c.secrets import SecretBytes, load_secret

log = structlog.get_logger(__name__)

GRAPH_SCOPE = "https://graph.microsoft.com/.default"


class TokenError(Exception):
    """A token could not be obtained for a tenant.

    ``transient`` is True for identity-service outages and network errors (retry later);
    False for real auth failures (``AADSTS`` errors: consent missing, cert not uploaded,
    tenant not found, unusable certificate).
    """

    def __init__(self, tenant_id: str, message: str, *, transient: bool = False) -> None:
        super().__init__(f"token request for tenant {tenant_id} failed: {message}")
        self.tenant_id = tenant_id
        self.transient = transient


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

    Called once at startup so a broken certificate fails fast instead of failing every
    token request later.
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
        raise CertificateError(
            f"{cert_ref.split(':', 1)[0]}: certificate unusable: {exc}"
        ) from None
    return cert, password, info


class TokenProvider(Protocol):
    async def get_token(self, tenant_id: str) -> str: ...

    async def aclose(self) -> None: ...


class CertificateTokenProvider:
    def __init__(
        self,
        client_id: str,
        certificate: SecretBytes,
        password: SecretBytes | None = None,
    ) -> None:
        self._client_id = client_id
        self._certificate = certificate
        self._password = password
        self._credentials: dict[str, CertificateCredential] = {}
        self._lock = asyncio.Lock()

    async def _credential(self, tenant_id: str) -> CertificateCredential:
        async with self._lock:
            cred = self._credentials.get(tenant_id)
            if cred is None:
                try:
                    cred = CertificateCredential(
                        tenant_id,
                        self._client_id,
                        certificate_data=self._certificate.get(),
                        password=self._password.get() if self._password else None,
                    )
                except (ValueError, TypeError) as exc:  # certificate parsed lazily here
                    raise TokenError(tenant_id, f"unusable certificate: {exc}") from None
                self._credentials[tenant_id] = cred
            return cred

    async def get_token(self, tenant_id: str) -> str:
        cred = await self._credential(tenant_id)
        try:
            token = await cred.get_token(GRAPH_SCOPE)
        except ClientAuthenticationError as exc:
            # Only the AAD error summary; azure-identity messages do not contain tokens.
            message = (exc.message or type(exc).__name__).splitlines()[0]
            # AADSTS codes are definitive (consent, cert, tenant); anything else (identity
            # service 5xx, malformed response) is treated as transient.
            raise TokenError(tenant_id, message, transient="AADSTS" not in message) from None
        except AzureError as exc:  # network errors talking to login.microsoftonline.com
            raise TokenError(tenant_id, type(exc).__name__, transient=True) from None
        except (ValueError, TypeError) as exc:  # unreadable certificate data
            raise TokenError(tenant_id, f"unusable certificate: {exc}") from None
        log.debug(
            "token_acquired",
            tenant_id=tenant_id,
            token_expires_at=datetime.fromtimestamp(token.expires_on, UTC).isoformat(),
        )
        return token.token

    async def forget(self, tenant_id: str) -> None:
        async with self._lock:
            cred = self._credentials.pop(tenant_id, None)
        if cred is not None:
            await cred.close()

    async def aclose(self) -> None:
        async with self._lock:
            creds = list(self._credentials.values())
            self._credentials.clear()
        for cred in creds:
            await cred.close()


class StaticTokenProvider:
    """Returns a fixed token; for tests and the offline ``--fixtures`` mode."""

    def __init__(self, token: str = "fake-token") -> None:
        self._token = token

    async def get_token(self, tenant_id: str) -> str:
        return self._token

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
