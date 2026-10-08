"""Offline Graph emulation from a directory of fixture files (``m365c ... --fixtures``).

Request paths map to file names: the API version prefix and ``(...)`` function
arguments are dropped and ``/`` becomes ``_``. A ``$skiptoken`` query adds ``__<token>``.

    GET /v1.0/subscribedSkus                         -> subscribedSkus.json
    GET /v1.0/users/<id>/drive                       -> users_<id>_drive.json
    GET /v1.0/users?$skiptoken=page2                 -> users__page2.json
    GET /v1.0/reports/getMailboxUsageDetail(...)     -> 302 to the download host, which
                                                        serves reports_getMailboxUsageDetail.csv

Missing files answer 404 with a Graph-style error body.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import httpx

from m365c.auth import StaticTokenProvider

DOWNLOAD_HOST = "reports.fixtures.invalid"


def fixture_name(url: httpx.URL) -> str:
    path = re.sub(r"^/(v1\.0|beta)/", "", url.path)
    path = re.sub(r"\(.*?\)", "", path)
    name = path.strip("/").replace("/", "_")
    skiptoken = url.params.get("$skiptoken")
    return f"{name}__{skiptoken}" if skiptoken else name


def _not_found(name: str) -> httpx.Response:
    return httpx.Response(
        404, json={"error": {"code": "itemNotFound", "message": f"no fixture for {name}"}}
    )


def fixture_transport(directory: Path) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == DOWNLOAD_HOST:
            assert "Authorization" not in request.headers, "auth header leaked to download host"
            path = directory / request.url.path.lstrip("/")
            return (
                httpx.Response(200, content=path.read_bytes())
                if path.is_file()
                else _not_found(path.name)
            )
        name = fixture_name(request.url)
        if name.startswith("reports_"):
            if not (directory / f"{name}.csv").is_file():
                return _not_found(name)
            return httpx.Response(
                302, headers={"Location": f"https://{DOWNLOAD_HOST}/{name}.csv?tempauth=x"}
            )
        path = directory / f"{name}.json"
        if not path.is_file():
            return _not_found(name)
        return httpx.Response(
            200, content=path.read_bytes(), headers={"Content-Type": "application/json"}
        )

    return httpx.MockTransport(handler)


def fake_jwt(roles: list[str]) -> str:
    """An unsigned JWT-shaped string carrying ``roles``; never accepted by Graph."""

    def enc(obj: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc({'roles': roles})}."


def fixture_token_provider(roles: list[str]) -> StaticTokenProvider:
    return StaticTokenProvider(fake_jwt(roles))
