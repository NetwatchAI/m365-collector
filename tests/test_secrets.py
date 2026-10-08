from __future__ import annotations

from pathlib import Path

import pytest

from m365c.auth import decode_jwt_claims
from m365c.fixtures import fake_jwt
from m365c.secrets import KeyVaultBackend, SecretError, VaultBackend, load_secret


def test_file_backend(tmp_path: Path) -> None:
    pem = tmp_path / "cert.pem"
    pem.write_bytes(b"-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n")
    secret = load_secret(f"file:{pem}")
    assert secret.get().startswith(b"-----BEGIN")
    assert "abc" not in repr(secret) and "abc" not in str(secret)


def test_file_backend_errors(tmp_path: Path) -> None:
    with pytest.raises(SecretError, match="file not found"):
        load_secret(f"file:{tmp_path / 'missing.pem'}")
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    with pytest.raises(SecretError, match="empty"):
        load_secret(f"file:{empty}")


def test_unknown_prefix() -> None:
    with pytest.raises(SecretError, match="unsupported secret reference 'env'"):
        load_secret("env:FOO")
    with pytest.raises(SecretError):
        load_secret("/run/secrets/no-prefix")


def test_reference_parsing() -> None:
    assert VaultBackend.parse("secret/m365c/cert#pem") == ("secret", "m365c/cert", "pem")
    with pytest.raises(SecretError):
        VaultBackend.parse("secret/m365c/cert")
    assert KeyVaultBackend.parse("https://kv1.vault.azure.net/m365c-cert") == (
        "https://kv1.vault.azure.net",
        "m365c-cert",
    )
    with pytest.raises(SecretError):
        KeyVaultBackend.parse("kv1/m365c-cert")


def test_vault_requires_addr(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("hvac")
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    with pytest.raises(SecretError, match="VAULT_ADDR"):
        load_secret("vault:secret/m365c/cert#pem")


def test_decode_jwt_claims() -> None:
    assert decode_jwt_claims(fake_jwt(["User.Read.All"]))["roles"] == ["User.Read.All"]
    with pytest.raises(ValueError):
        decode_jwt_claims("not-a-jwt")
