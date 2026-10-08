"""Per-tenant app-only tokens for the single multi-tenant Netwatch app registration.

Every tenant uses the same client ID and certificate; only the tenant ID changes. One
``CertificateCredential`` is created per tenant and reused, so azure-identity's in-memory
token cache does the caching. Tokens never leave memory and are never logged.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog
from azure.core.exceptions import ClientAuthenticationError
from azure.identity.aio import CertificateCredential

from m365c.secrets import SecretBytes

log = structlog.get_logger(__name__)

GRAPH_SCOPE = "https://graph.microsoft.com/.default"


class TokenError(Exception):
    """A token could not be obtained for a tenant (consent missing, bad cert, ...)."""

    def __init__(self, tenant_id: str, message: str) -> None:
        super().__init__(f"token request for tenant {tenant_id} failed: {message}")
        self.tenant_id = tenant_id


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
                cred = CertificateCredential(
                    tenant_id,
                    self._client_id,
                    certificate_data=self._certificate.get(),
                    password=self._password.get() if self._password else None,
                )
                self._credentials[tenant_id] = cred
            return cred

    async def get_token(self, tenant_id: str) -> str:
        cred = await self._credential(tenant_id)
        try:
            token = await cred.get_token(GRAPH_SCOPE)
        except ClientAuthenticationError as exc:
            # Only the AAD error summary; azure-identity messages do not contain tokens.
            message = (exc.message or type(exc).__name__).splitlines()[0]
            raise TokenError(tenant_id, message) from None
        except ValueError as exc:  # unreadable certificate data
            raise TokenError(tenant_id, f"invalid certificate: {exc}") from None
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
