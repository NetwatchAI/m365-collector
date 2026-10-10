"""Throwaway certificates for tests (generated in memory, never real)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID


def _make(
    days_valid: float, *, start_days_ago: float = 1, rsa_key: bool = False
) -> tuple[ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey, x509.Certificate]:
    key: ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if rsa_key
        else ec.generate_private_key(ec.SECP256R1())
    )
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "m365c-test")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=start_days_ago))
        .not_valid_after(now + timedelta(days=days_valid))
        .sign(key, hashes.SHA256())
    )
    return key, cert


def pem_bundle(
    days_valid: float = 365, password: bytes | None = None, *, rsa_key: bool = False
) -> bytes:
    """``rsa_key=True`` for bundles a real CertificateCredential must sign with (RS256)."""
    key, cert = _make(days_valid, start_days_ago=max(1, -days_valid + 1), rsa_key=rsa_key)
    enc = (
        serialization.BestAvailableEncryption(password)
        if password
        else serialization.NoEncryption()
    )
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc)
    return key_pem + cert.public_bytes(serialization.Encoding.PEM)


def pfx_bundle(password: bytes) -> bytes:
    key, cert = _make(365)
    return pkcs12.serialize_key_and_certificates(
        b"m365c", key, cert, None, serialization.BestAvailableEncryption(password)
    )


def mismatched_bundle() -> bytes:
    key, _ = _make(365)
    _, other_cert = _make(365)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return key_pem + other_cert.public_bytes(serialization.Encoding.PEM)
