"""Tests for the ``redact_token`` helper.

The full :class:`TokenRedactionFilter` is exercised in a follow-up test set
once the filter itself is added in the next milestone-1 step.
"""

from __future__ import annotations

from upstream_agent.logging_setup import redact_token


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
