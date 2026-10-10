from __future__ import annotations

import asyncio
import json
import socket
import time
from pathlib import Path

import aiohttp
import pytest
from prometheus_client import CollectorRegistry

from m365c.auth import CertificateError, StaticTokenProvider
from m365c.fixtures import fixture_transport
from m365c.main import serve

from .certs import pem_bundle
from .conftest import GRAPH_FIXTURES, TENANT_ID


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def write_config(cfg: Path, port: int, persist: Path, cert: Path) -> None:
    cfg.mkdir()
    (cfg / "settings.yaml").write_text(
        "app:\n  client_id: 00000000-0000-0000-0000-000000000000\n"
        f"secrets:\n  cert: file:{cert.as_posix()}\n"
        f"server:\n  host: 127.0.0.1\n  port: {port}\n"
        f"cache:\n  persist_dir: {persist.as_posix()}\n",
        encoding="utf-8",
    )
    (cfg / "tenants.yaml").write_text(
        f"tenants:\n  - id: {TENANT_ID}\n    name: contoso.onmicrosoft.com\n"
        "    customer: contoso\n    collectors: [license, servicehealth]\n",
        encoding="utf-8",
    )


async def test_serve_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("m365c.scheduler.MAX_INITIAL_JITTER_S", 0.0)
    port = free_port()
    persist = tmp_path / "cache"
    persist.mkdir()
    # A snapshot of a tenant that was removed while the service was down (holds PII).
    orphan = persist / "99999999-9999-9999-9999-999999999999__users.json"
    orphan.write_text(
        json.dumps(
            {"tenant_id": "99999999-9999-9999-9999-999999999999", "collector": "users",
             "collected_at": 1.0, "samples": []}
        ),
        encoding="utf-8",
    )  # fmt: skip
    # A persisted snapshot for a configured collector, from 2h ago.
    (persist / f"{TENANT_ID}__license.json").write_text(
        json.dumps(
            {"tenant_id": TENANT_ID, "collector": "license", "collected_at": 1000.0,
             "samples": [{"name": "m365_license_total", "value": 3, "labels": {"sku": "OLD"}}]}
        ),
        encoding="utf-8",
    )  # fmt: skip
    cert = tmp_path / "unused.pem"
    cfg = tmp_path / "config"
    write_config(cfg, port, persist, cert)

    stop = asyncio.Event()
    registry = CollectorRegistry()
    task = asyncio.create_task(
        serve(
            cfg,
            tokens=StaticTokenProvider(),
            transport=fixture_transport(GRAPH_FIXTURES),
            registry=registry,
            stop=stop,
        )
    )
    base = f"http://127.0.0.1:{port}"
    async with aiohttp.ClientSession() as http:
        for _ in range(200):
            try:
                async with http.get(f"{base}/readyz") as r:
                    if r.status == 200:
                        break
            except aiohttp.ClientConnectionError:
                pass
            await asyncio.sleep(0.05)
        else:
            pytest.fail("service never became ready")
        async with http.get(f"{base}/healthz") as r:
            assert r.status == 200
        for _ in range(100):  # wait until license has replaced the restored snapshot
            async with http.get(f"{base}/metrics") as r:
                body = await r.text()
            if 'sku="SPB"' in body:
                break
            await asyncio.sleep(0.05)
    assert (
        'm365_license_total{customer="contoso",sku="SPB",tenant="contoso.onmicrosoft.com"} 25.0'
        in body
    )
    assert 'sku="OLD"' not in body
    assert "m365_service_status" in body
    assert not orphan.exists()  # orphaned persisted user data deleted at startup

    stop.set()
    await asyncio.wait_for(task, timeout=10)  # graceful shutdown returns


async def test_serve_restored_snapshot_has_last_success(tmp_path: Path) -> None:
    """After a restart, restored data carries its collection time (so Stale can fire)
    even before the collector runs again."""
    port = free_port()
    persist = tmp_path / "cache"
    persist.mkdir()
    (persist / f"{TENANT_ID}__license.json").write_text(
        json.dumps(
            {"tenant_id": TENANT_ID, "collector": "license", "collected_at": 1000.0, "samples": []}
        ),
        encoding="utf-8",
    )
    cfg = tmp_path / "config"
    write_config(cfg, port, persist, tmp_path / "unused.pem")
    stop = asyncio.Event()
    registry = CollectorRegistry()
    task = asyncio.create_task(
        serve(cfg, tokens=StaticTokenProvider(), transport=fixture_transport(GRAPH_FIXTURES),
              registry=registry, stop=stop)
    )  # fmt: skip
    for _ in range(200):
        value = registry.get_sample_value(
            "m365_collector_last_success_timestamp",
            {"customer": "contoso", "tenant": "contoso.onmicrosoft.com", "collector": "license"},
        )
        if value is not None:
            break
        await asyncio.sleep(0.02)
    assert value == 1000.0
    assert (
        registry.get_sample_value(
            "m365_collector_success",
            {"customer": "contoso", "tenant": "contoso.onmicrosoft.com", "collector": "license"},
        )
        is None
    )  # no run yet: success is not claimed
    stop.set()
    await asyncio.wait_for(task, timeout=10)


async def test_serve_fails_fast_on_bad_certificate(tmp_path: Path) -> None:
    cert = tmp_path / "cert.pem"
    cert.write_bytes(b"garbage")
    cfg = tmp_path / "config"
    write_config(cfg, free_port(), tmp_path / "cache", cert)
    with pytest.raises(CertificateError):
        await serve(cfg)


async def test_serve_starts_with_expired_certificate_and_exports_expiry(tmp_path: Path) -> None:
    cert = tmp_path / "cert.pem"
    cert.write_bytes(pem_bundle(days_valid=-30))
    cfg = tmp_path / "config"
    write_config(cfg, free_port(), tmp_path / "cache", cert)
    (cfg / "tenants.yaml").write_text("tenants: []\n", encoding="utf-8")
    stop = asyncio.Event()
    registry = CollectorRegistry()
    task = asyncio.create_task(serve(cfg, registry=registry, stop=stop))
    expiry = None
    for _ in range(200):
        expiry = next(
            (s.value for m in registry.collect() for s in m.samples
             if s.name == "m365_cert_expiry_timestamp"),
            None,
        )  # fmt: skip
        if expiry is not None:
            break
        await asyncio.sleep(0.02)
    assert expiry is not None and expiry < time.time()  # so M365CertificateExpiring fires
    stop.set()
    await asyncio.wait_for(task, timeout=10)


async def test_serve_client_secret_tenants_ignore_unusable_default_certificate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only client-secret tenants: a missing default certificate (as the Compose file still
    configures one) does not stop the service, and the tenant collects with its own secret."""
    from m365c import main as main_module
    from m365c.auth import TenantTokenProvider

    from .identity import FAKE_SECRET, FakeIdentity

    monkeypatch.setattr("m365c.scheduler.MAX_INITIAL_JITTER_S", 0.0)
    identity = FakeIdentity()
    monkeypatch.setattr(
        main_module,
        "TenantTokenProvider",
        lambda: TenantTokenProvider(credential_kwargs={"transport": identity, "retry_total": 0}),
    )
    secret = tmp_path / "contoso_client_secret"
    secret.write_text(FAKE_SECRET, encoding="utf-8")
    cfg = tmp_path / "config"
    write_config(cfg, free_port(), tmp_path / "cache", tmp_path / "missing-cert.pem")
    (cfg / "tenants.yaml").write_text(
        f"tenants:\n  - id: {TENANT_ID}\n    name: contoso.onmicrosoft.com\n"
        "    customer: contoso\n    collectors: [license]\n"
        "    auth:\n      method: client_secret\n"
        f"      client_id: 33333333-3333-3333-3333-333333333333\n"
        f"      client_secret: file:{secret.as_posix()}\n",
        encoding="utf-8",
    )
    stop = asyncio.Event()
    registry = CollectorRegistry()
    task = asyncio.create_task(
        serve(cfg, transport=fixture_transport(GRAPH_FIXTURES), registry=registry, stop=stop)
    )
    labels = {"customer": "contoso", "tenant": "contoso.onmicrosoft.com", "collector": "license"}
    for _ in range(200):
        if registry.get_sample_value("m365_collector_success", labels) is not None:
            break
        await asyncio.sleep(0.02)
    assert registry.get_sample_value("m365_collector_success", labels) == 1.0
    assert identity.secrets_seen == [FAKE_SECRET]
    stop.set()
    await asyncio.wait_for(task, timeout=10)
