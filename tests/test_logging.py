from __future__ import annotations

import json
import logging

import pytest
import structlog

from m365c.logging import REDACTED, configure_logging, is_sensitive_key, redact_processor


def test_sensitive_key_rules() -> None:
    for key in ("authorization", "Authorization", "access_token", "client_assertion",
                "client_secret", "password", "refresh_token", "my_secret_value", "TOKEN"):  # fmt: skip
        assert is_sensitive_key(key), key
    for key in ("token_expires_at", "tenant", "upn", "status"):
        assert not is_sensitive_key(key), key


def test_redact_processor_nested() -> None:
    event = {
        "event": "x",
        "access_token": "abc",
        "token_expires_at": "2026-10-08T00:00:00Z",
        "headers": {"Authorization": "Bearer abc.def.ghi", "Accept": "application/json"},
        "items": [{"client_secret": "s"}, {"ok": "fine"}],
        "body": {"nested": {"password": "p", "keep": 1}},
    }
    out = redact_processor(None, "info", event)
    assert out["access_token"] == REDACTED
    assert out["token_expires_at"] == "2026-10-08T00:00:00Z"
    assert out["headers"] == {"Authorization": REDACTED, "Accept": "application/json"}
    assert out["items"] == [{"client_secret": REDACTED}, {"ok": "fine"}]
    assert out["body"] == {"nested": {"password": REDACTED, "keep": 1}}


def test_redact_credential_shaped_strings() -> None:
    jwt = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJyb2xlcyI6WyJVc2VyLlJlYWQuQWxsIl19.c2lnbmF0dXJl"
    out = redact_processor(
        None,
        "info",
        {
            "event": f"call failed with header Bearer {jwt}",
            "url": "https://reportsncu.office.com/x.csv?tempauth=VERYSECRET&x=1",
            "pem": "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----",
        },
    )
    assert jwt not in json.dumps(out)
    assert "VERYSECRET" not in out["url"] and out["url"].endswith("&x=1")
    assert out["pem"] == REDACTED


def test_end_to_end_json_output_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("DEBUG", json=True)
    structlog.get_logger("t").info(
        "token_acquired",
        access_token="leak1",
        token_expires_at="soon",
        extra={"authorization": "leak2"},
    )
    logging.getLogger("azure.identity").warning("assertion %s", "Bearer leak3")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    blob = json.dumps(lines)
    assert "leak1" not in blob and "leak2" not in blob and "leak3" not in blob
    assert lines[0]["token_expires_at"] == "soon"
    assert lines[0]["level"] == "info"


def test_httpx_debug_cannot_emit(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("DEBUG", json=True)
    for name in ("httpx", "httpcore.http11"):
        logging.getLogger(name).setLevel(logging.DEBUG)  # someone tries to turn it on
        logging.getLogger(name).debug(
            "send_request_headers.started headers=[(b'Authorization', b'Bearer x')]"
        )
        logging.getLogger(name).info("HTTP Request: GET https://x/?tempauth=abc")
    assert capsys.readouterr().out == ""


def test_redaction_gaps_from_review(capsys: pytest.CaptureFixture[str]) -> None:
    """Review M7: dict reprs in exception text, non-dict Mappings, form-encoded secrets
    and stdlib %s args must all be redacted."""
    import types

    configure_logging("INFO", json=True)
    log = structlog.get_logger("t")
    try:
        raise RuntimeError(str({"access_token": "LEAK_A", "client_secret": "LEAK_B"}))
    except RuntimeError:
        log.exception("boom")
    log.info("hdrs", headers=types.MappingProxyType({"Authorization": "LEAK_C"}))
    log.info("body", body="grant_type=client_credentials&client_secret=LEAK_D&scope=x")
    logging.getLogger("aiohttp.client").warning("request %s", {"password": "LEAK_E"})
    out = capsys.readouterr().out
    for leak in ("LEAK_A", "LEAK_B", "LEAK_C", "LEAK_D", "LEAK_E"):
        assert leak not in out, leak
    assert "client_credentials" in out  # non-secret parts survive


def test_loaded_secret_values_are_scrubbed_anywhere(
    tmp_path: pytest.TempPathFactory,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any secret loaded through m365c.secrets is scrubbed from every log line, in raw and
    URL-encoded form, whatever key or message shape it appears in."""
    from m365c.secrets import load_secret

    monkeypatch.setenv("M365C_TEST_SCRUB", "Ab+c/d=e~fake secret 42")
    load_secret("env:M365C_TEST_SCRUB")
    configure_logging("DEBUG", json=True)
    structlog.get_logger("t").error(
        "boom", detail="identity said: Ab+c/d=e~fake secret 42 was wrong",
        body="client_id=x&client_secret_value=Ab%2Bc%2Fd%3De~fake+secret+42",
    )  # fmt: skip
    logging.getLogger("azure.identity").error("echo %s", "Ab+c/d=e~fake secret 42")
    out = capsys.readouterr().out
    assert "fake secret 42" not in out and "fake+secret+42" not in out
    assert out.count(REDACTED) >= 3


def test_azure_identity_warnings_are_silenced(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("DEBUG", json=True)
    logging.getLogger("azure.identity.aio._internal").warning("get_token failed: raw text")
    assert "raw text" not in capsys.readouterr().out
