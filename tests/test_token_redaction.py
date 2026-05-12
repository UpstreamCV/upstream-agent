"""Tests for the ``redact_token`` helper and ``TokenRedactionFilter``."""

from __future__ import annotations

import logging
from pathlib import Path

from upstream_agent.logging_setup import (
    TokenRedactionFilter,
    redact_token,
    setup_logging,
)


# ---------------------------------------------------------------------------
# redact_token() helper
# ---------------------------------------------------------------------------


def test_redact_token_none_returns_unset_sentinel() -> None:
    assert redact_token(None) == "<unset>"


def test_redact_token_empty_string_returns_unset_sentinel() -> None:
    assert redact_token("") == "<unset>"


def test_redact_token_short_token_is_fully_masked() -> None:
    assert redact_token("abc") == "****"


def test_redact_token_exactly_eight_chars_is_fully_masked() -> None:
    assert redact_token("12345678") == "****"


def test_redact_token_long_token_keeps_only_last_four() -> None:
    assert redact_token("sk_live_abcd1234") == "****1234"


def test_redact_token_never_returns_the_full_token() -> None:
    secret = "sk_live_supersecret_dont_leak_5678"
    out = redact_token(secret)
    assert secret not in out
    assert out.endswith("5678")
    assert out.startswith("****")


# ---------------------------------------------------------------------------
# TokenRedactionFilter — direct unit tests
# ---------------------------------------------------------------------------


def _make_record(msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord(
        name="upstream.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg=msg,
        args=args or None,
        exc_info=None,
    )


def test_filter_redacts_known_secret_in_message() -> None:
    secret = "sk_live_supersecret_value"
    f = TokenRedactionFilter([secret])
    rec = _make_record(f"authenticating with token {secret} now")
    assert f.filter(rec) is True
    assert secret not in rec.getMessage()
    assert "****" in rec.getMessage()


def test_filter_redacts_known_secret_in_args() -> None:
    secret = "sk_live_args_secret_value"
    f = TokenRedactionFilter([secret])
    rec = _make_record("auth header: %s", secret)
    f.filter(rec)
    msg = rec.getMessage()
    assert secret not in msg
    assert "****" in msg


def test_filter_redacts_bearer_pattern_without_known_secret() -> None:
    f = TokenRedactionFilter()
    rec = _make_record("sending request Bearer abcdefghij and more")
    f.filter(rec)
    msg = rec.getMessage()
    assert "abcdefghij" not in msg
    assert "Bearer ****" in msg


def test_filter_redacts_authorization_header_pattern() -> None:
    f = TokenRedactionFilter()
    rec = _make_record("Authorization: deadbeef-token-value")
    f.filter(rec)
    msg = rec.getMessage()
    assert "deadbeef-token-value" not in msg
    assert "Authorization: ****" in msg


def test_filter_redacts_jwt_shaped_token() -> None:
    fake_jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.SflKxwRJSMeKKF2QT4f"
    f = TokenRedactionFilter()
    rec = _make_record(f"got id token {fake_jwt} back")
    f.filter(rec)
    msg = rec.getMessage()
    assert fake_jwt not in msg
    assert "****" in msg


def test_filter_redacts_token_equals_pattern() -> None:
    f = TokenRedactionFilter()
    rec = _make_record("query string: token=verysecret-do-not-leak")
    f.filter(rec)
    msg = rec.getMessage()
    assert "verysecret-do-not-leak" not in msg
    assert "****" in msg


def test_filter_is_idempotent_on_already_redacted_text() -> None:
    f = TokenRedactionFilter(["sk_live_xxx"])
    rec = _make_record("auth Bearer **** header")
    f.filter(rec)
    assert rec.getMessage() == "auth Bearer **** header"


# ---------------------------------------------------------------------------
# setup_logging() integration — writes to a rotating file and redacts
# ---------------------------------------------------------------------------


def test_setup_logging_writes_redacted_log_file(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    secret = "sk_live_filewrite_secret_99"
    setup_logging(level="info", log_dir=log_dir, sensitive_values=[secret])

    logger = logging.getLogger("upstream.test")
    logger.info("about to call api with token %s now", secret)

    for handler in logging.getLogger().handlers:
        handler.flush()

    log_file = log_dir / "agent.log"
    assert log_file.exists()
    contents = log_file.read_text(encoding="utf-8")
    assert secret not in contents
    assert "****" in contents


def test_setup_logging_is_idempotent(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    setup_logging(level="info", log_dir=log_dir, sensitive_values=[])
    setup_logging(level="info", log_dir=log_dir, sensitive_values=[])
    root = logging.getLogger()
    # Exactly one rotating file handler and one stream handler — no doubles.
    assert len(root.handlers) == 2
    # Every handler carries exactly one TokenRedactionFilter.
    for handler in root.handlers:
        assert any(isinstance(f, TokenRedactionFilter) for f in handler.filters)
        assert len(handler.filters) == 1
