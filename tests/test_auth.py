from __future__ import annotations

import time
from pathlib import Path

import pytest

from m365c.auth import (
    CertificateError,
    CertificateTokenProvider,
    TokenError,
    inspect_certificate,
    load_app_certificate,
)
from m365c.secrets import SecretBytes

from .certs import mismatched_bundle, pem_bundle, pfx_bundle

TENANT = "11111111-1111-1111-1111-111111111111"


def test_valid_pem_bundle() -> None:
    info = inspect_certificate(pem_bundle(days_valid=90))
    assert info.subject == "CN=m365c-test"
    assert len(info.thumbprint) == 40
    assert 89 * 86400 < info.not_after - time.time() < 91 * 86400
    assert not info.expired()


def test_encrypted_pem_and_pfx() -> None:
    assert inspect_certificate(pem_bundle(password=b"pw"), b"pw").subject == "CN=m365c-test"
    assert inspect_certificate(pfx_bundle(b"pw"), b"pw").subject == "CN=m365c-test"
    with pytest.raises(CertificateError):
        inspect_certificate(pem_bundle(password=b"pw"), b"wrong")
    with pytest.raises(CertificateError, match="password"):
        inspect_certificate(pem_bundle(password=b"pw"))  # password missing
    with pytest.raises(CertificateError, match="PKCS#12"):
        inspect_certificate(pfx_bundle(b"pw"), b"wrong")


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        (b"garbage", "not a PEM or PKCS#12"),
        (b"-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n", "no private key"),
        (mismatched_bundle(), "matches the private key"),
    ],
)
def test_unusable_certificates(data: bytes, fragment: str) -> None:
    with pytest.raises(CertificateError, match=fragment):
        inspect_certificate(data)


def test_key_only_pem() -> None:
    bundle = pem_bundle()
    key_only = bundle[: bundle.index(b"-----BEGIN CERTIFICATE-----")]
    with pytest.raises(CertificateError, match="no certificate"):
        inspect_certificate(key_only)


def test_expired_certificate_is_detected() -> None:
    assert inspect_certificate(pem_bundle(days_valid=-30)).expired()


def test_load_app_certificate_names_reference_not_content(tmp_path: Path) -> None:
    bad = tmp_path / "cert.pem"
    bad.write_bytes(b"-----BEGIN PRIVATE KEY-----\nnot-base64\n-----END PRIVATE KEY-----\n")
    with pytest.raises(CertificateError) as info:
        load_app_certificate(f"file:{bad}", None)
    assert "not-base64" not in str(info.value)
    good = tmp_path / "good.pem"
    good.write_bytes(pem_bundle())
    _, password, cert_info = load_app_certificate(f"file:{good}", None)
    assert password is None and cert_info.subject == "CN=m365c-test"


async def test_garbage_certificate_maps_to_token_error() -> None:
    """The credential parses the certificate lazily; that must surface as TokenError
    (auth failure), not a bare ValueError. No network is touched: parsing fails first."""
    provider = CertificateTokenProvider(
        "00000000-0000-0000-0000-000000000000", SecretBytes(b"garbage")
    )
    with pytest.raises(TokenError) as info:
        await provider.get_token(TENANT)
    assert not info.value.transient
    assert "unusable certificate" in str(info.value)
    await provider.aclose()
