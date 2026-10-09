"""structlog configuration: JSON lines on stdout, with secret redaction.

Standard-library loggers (azure-identity, aiohttp, ...) are routed through the same
processor chain, so redaction applies to them too, including their formatted ``%s``
arguments and exception tracebacks. httpx/httpcore are pinned to WARNING and filtered so
that their request lines and headers are never emitted, even when the root level is
DEBUG. The Graph client itself never logs the path of a pre-authenticated report
download URL (the path is the credential).

Redaction: sensitive keys anywhere in nested mappings/sequences, plus credential-shaped
text in any string (Bearer tokens, JWTs, PEM private keys, ``?tempauth=`` style query
parameters, and ``key=value`` / ``'key': 'value'`` pairs for secret-like keys).
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

REDACTED = "[REDACTED]"

_EXACT_KEYS = frozenset(
    {"authorization", "access_token", "client_assertion", "client_secret", "password"}
)
_ALLOWED_KEYS = frozenset({"token_expires_at"})

# Values that look like credentials even when they appear under an innocent key.
_VALUE_PATTERNS = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]+=*"), f"Bearer {REDACTED}"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), REDACTED),
    (
        re.compile(r"(?i)([?&](?:tempauth|sig|code|token|access_token)=)[^&\s\"']+"),
        rf"\1{REDACTED}",
    ),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        REDACTED,
    ),
    # key=value, key: value, 'key': 'value', "key"="value" for secret-like keys, e.g. a
    # dict repr inside an exception message or a form-encoded token request body.
    (
        re.compile(
            r"(?i)([\"']?\b(?:client_secret|access_token|refresh_token|id_token|"
            r"client_assertion|password|passwd|secret|token|authorization)\b[\"']?"
            r"\s*[:=]\s*)([\"']?)(?!Bearer\b)(?!\[REDACTED\])[^\"'&\s,;}]+"
        ),
        rf"\1\2{REDACTED}",
    ),
)

_QUIET_LOGGERS = ("httpx", "httpcore", "hpack", "urllib3")


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _ALLOWED_KEYS:
        return False
    return lowered in _EXACT_KEYS or "secret" in lowered or "token" in lowered


def scrub_string(value: str) -> str:
    for pattern, repl in _VALUE_PATTERNS:
        value = pattern.sub(repl, value)
    return value


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):  # dict, MappingProxy, httpx.Headers, CIMultiDict, ...
        return {
            k: (REDACTED if isinstance(k, str) and is_sensitive_key(k) else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple | set):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        return scrub_string(value)
    if isinstance(value, bytes):
        return REDACTED
    return value


def redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Redact sensitive keys anywhere in the event, and credential-shaped strings."""
    for key in list(event_dict):
        if is_sensitive_key(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _redact(event_dict[key])
    return event_dict


class _QuietLoggersFilter(logging.Filter):
    """Handler filter: drops records below WARNING from HTTP-library loggers (and their
    children), regardless of how their levels are changed later."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        return not any(
            record.name == name or record.name.startswith(name + ".") for name in _QUIET_LOGGERS
        )


def _shared_processors() -> list[Any]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
        redact_processor,
    ]


def configure_logging(level: str = "INFO", *, json: bool = True) -> None:
    lvl = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    renderer: Any = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *_shared_processors(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_shared_processors()[1:],
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stderr if not json else sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(_QuietLoggersFilter())

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(lvl)

    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    # azure-core's HTTP logging policy prints request/response headers at INFO.
    logging.getLogger("azure").setLevel(max(lvl, logging.WARNING))
