"""A fake Microsoft Entra token endpoint, plugged into real azure-identity credentials as their
azure-core transport. No network: requests to login.microsoftonline.com are answered here."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from azure.core.exceptions import ServiceRequestError
from azure.core.pipeline.transport import AsyncHttpResponse, AsyncHttpTransport, HttpRequest

from m365c.fixtures import fake_jwt

PARITY_ROLES = ["Reports.Read.All", "User.Read.All", "AuditLog.Read.All", "Organization.Read.All"]
FAKE_SECRET = "Zq8~fake-client-secret-VALUE.0123456789"  # never a real secret


class _Response(AsyncHttpResponse):
    def __init__(self, request: HttpRequest, status: int, body: dict[str, Any]) -> None:
        super().__init__(request, None)
        self.status_code = status
        self.headers = {"Content-Type": "application/json"}
        self.reason = "OK" if status < 400 else "Error"
        self.content_type = "application/json"
        self._body = json.dumps(body).encode()

    def body(self) -> bytes:
        return self._body


Handler = Callable[[dict[str, str]], tuple[int, dict[str, Any]]]


class FakeIdentity(AsyncHttpTransport):
    """``handler(form_fields) -> (status, json_body)``; records every token request."""

    def __init__(self, handler: Handler | None = None) -> None:
        self.handler = handler or token_ok()
        self.requests: list[dict[str, str]] = []
        self.network_error = False

    async def __aenter__(self) -> FakeIdentity:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def send(self, request: HttpRequest, **kwargs: Any) -> _Response:  # type: ignore[override]
        if self.network_error:
            raise ServiceRequestError("connection refused")
        form = {str(k): str(v) for k, v in dict(request.data or {}).items()}
        self.requests.append(form)
        status, body = self.handler(form)
        return _Response(request, status, body)

    @property
    def secrets_seen(self) -> list[str]:
        return [r["client_secret"] for r in self.requests if "client_secret" in r]


def token_ok(roles: list[str] | None = None) -> Handler:
    def handler(form: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return 200, {
            "token_type": "Bearer",
            "expires_in": 3600,
            "access_token": fake_jwt(PARITY_ROLES if roles is None else roles),
        }

    return handler


def token_error(code: int, oauth_error: str = "invalid_client") -> Handler:
    """An Entra error whose description echoes the request body, as a worst case for leaks."""

    def handler(form: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return 401, {
            "error": oauth_error,
            "error_description": f"AADSTS{code}: something went wrong. Request: {form}",
            "error_codes": [code],
        }

    return handler
