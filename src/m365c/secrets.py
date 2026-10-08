"""Secret loading with pluggable backends selected by prefix.

- ``file:/path``                                   read a file (always available)
- ``vault:<mount>/<path>#<key>``                   HashiCorp Vault KV v2 (extra ``[vault]``)
- ``keyvault:https://<kv>.vault.azure.net/<name>`` Azure Key Vault secret (extra ``[azure]``)

Secrets are returned in memory as :class:`SecretBytes`, whose repr never shows the value.
Nothing here writes secrets to disk or logs them.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any, Protocol


class SecretError(Exception):
    """Secret reference invalid or the backend failed. The message never contains the value."""


class SecretBytes:
    __slots__ = ("_value",)

    def __init__(self, value: bytes) -> None:
        self._value = value

    def get(self) -> bytes:
        return self._value

    def __repr__(self) -> str:
        return "SecretBytes(**redacted**)"

    __str__ = __repr__

    def __len__(self) -> int:
        return len(self._value)


class SecretBackend(Protocol):
    def load(self, ref: str) -> bytes: ...


class FileBackend:
    def load(self, ref: str) -> bytes:
        path = Path(ref)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise SecretError(f"file:{ref}: file not found") from exc
        except OSError as exc:
            raise SecretError(f"file:{ref}: cannot read ({exc.strerror})") from exc


class VaultBackend:
    """HashiCorp Vault KV v2.

    Connection and auth come from the environment:

    - ``VAULT_ADDR`` (required), ``VAULT_NAMESPACE`` (optional)
    - AppRole: ``VAULT_ROLE_ID`` + ``VAULT_SECRET_ID``
    - Kubernetes: ``VAULT_K8S_ROLE`` (+ ``VAULT_K8S_TOKEN_PATH``, ``VAULT_K8S_MOUNT``)
    - ``VAULT_TOKEN`` as a fallback for local development
    """

    def _client(self) -> Any:
        try:
            import hvac
        except ImportError as exc:
            raise SecretError(
                "vault: backend needs the optional extra: install m365c[vault]"
            ) from exc
        addr = os.environ.get("VAULT_ADDR")
        if not addr:
            raise SecretError("vault: VAULT_ADDR is not set")
        client = hvac.Client(url=addr, namespace=os.environ.get("VAULT_NAMESPACE"))
        if os.environ.get("VAULT_ROLE_ID") and os.environ.get("VAULT_SECRET_ID"):
            client.auth.approle.login(
                role_id=os.environ["VAULT_ROLE_ID"], secret_id=os.environ["VAULT_SECRET_ID"]
            )
        elif os.environ.get("VAULT_K8S_ROLE"):
            token_path = os.environ.get(
                "VAULT_K8S_TOKEN_PATH", "/var/run/secrets/kubernetes.io/serviceaccount/token"
            )
            jwt = Path(token_path).read_text(encoding="utf-8").strip()
            client.auth.kubernetes.login(
                role=os.environ["VAULT_K8S_ROLE"],
                jwt=jwt,
                mount_point=os.environ.get("VAULT_K8S_MOUNT", "kubernetes"),
            )
        elif os.environ.get("VAULT_TOKEN"):
            client.token = os.environ["VAULT_TOKEN"]
        else:
            raise SecretError(
                "vault: no auth configured (VAULT_ROLE_ID/VAULT_SECRET_ID, VAULT_K8S_ROLE "
                "or VAULT_TOKEN)"
            )
        return client

    @staticmethod
    def parse(ref: str) -> tuple[str, str, str]:
        path, sep, key = ref.partition("#")
        mount, slash, sub = path.strip("/").partition("/")
        if not sep or not key or not slash or not sub:
            raise SecretError(f"vault:{ref}: expected vault:<mount>/<path>#<key>")
        return mount, sub, key

    def load(self, ref: str) -> bytes:
        mount, path, key = self.parse(ref)
        client = self._client()
        try:
            resp = client.secrets.kv.v2.read_secret_version(
                path=path, mount_point=mount, raise_on_deleted_version=True
            )
        except Exception as exc:
            raise SecretError(f"vault:{mount}/{path}: read failed ({type(exc).__name__})") from exc
        data = resp.get("data", {}).get("data", {})
        if key not in data:
            raise SecretError(f"vault:{mount}/{path}: key {key!r} not found")
        value = data[key]
        return value.encode("utf-8") if isinstance(value, str) else bytes(value)


class KeyVaultBackend:
    """Azure Key Vault secret, authenticated with ``DefaultAzureCredential``.

    Certificates imported into Key Vault are exposed as secrets; PKCS#12 content is
    base64 encoded, PEM content is returned as-is.
    """

    @staticmethod
    def parse(ref: str) -> tuple[str, str]:
        if not ref.startswith("https://"):
            raise SecretError(
                f"keyvault:{ref}: expected keyvault:https://<kv>.vault.azure.net/<name>"
            )
        base, _, name = ref.rstrip("/").rpartition("/")
        if not name or base.count("/") != 2:
            raise SecretError(
                f"keyvault:{ref}: expected keyvault:https://<kv>.vault.azure.net/<name>"
            )
        return base, name

    def load(self, ref: str) -> bytes:
        vault_url, name = self.parse(ref)
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient
        except ImportError as exc:
            raise SecretError(
                "keyvault: backend needs the optional extra: install m365c[azure]"
            ) from exc
        try:
            with DefaultAzureCredential() as cred:
                secret = SecretClient(vault_url=vault_url, credential=cred).get_secret(name)
        except Exception as exc:
            raise SecretError(
                f"keyvault:{vault_url}/{name}: read failed ({type(exc).__name__})"
            ) from exc
        value = secret.value or ""
        if secret.properties.content_type == "application/x-pkcs12":
            return base64.b64decode(value)
        return value.encode("utf-8")


BACKENDS: dict[str, SecretBackend] = {
    "file": FileBackend(),
    "vault": VaultBackend(),
    "keyvault": KeyVaultBackend(),
}


def load_secret(ref: str) -> SecretBytes:
    """Resolve a ``<backend>:<ref>`` secret reference."""
    prefix, sep, rest = ref.partition(":")
    if not sep or prefix not in BACKENDS:
        raise SecretError(
            f"unsupported secret reference {ref.split(':', 1)[0]!r}; "
            f"use one of {', '.join(p + ':' for p in BACKENDS)}"
        )
    value = BACKENDS[prefix].load(rest)
    if not value:
        raise SecretError(f"{prefix}: secret is empty")
    return SecretBytes(value)
