"""Tests for :func:`upstream_agent.transport.srt.parse_srt_url`.

These tests use placeholder SRT URLs only — no real ingest hosts,
ports, or stream IDs. The placeholder ``TEST_PLACEHOLDER`` is asserted
not to appear in the parsed result, since the parser must not store
the raw streamid.
"""

from __future__ import annotations

import pytest

from upstream_agent.errors import SrtUrlError
from upstream_agent.transport.srt import ParsedSrtUrl, parse_srt_url

_PLACEHOLDER_STREAMID = "TEST_PLACEHOLDER_streamid"


def test_valid_srt_url_with_all_query_params_parses() -> None:
    result = parse_srt_url(
        f"srt://ingest.test.local:8890/?streamid={_PLACEHOLDER_STREAMID}"
        "&mode=caller&latency=200"
    )

    assert isinstance(result, ParsedSrtUrl)
    assert result.has_host is True
    assert result.has_port is True
    assert result.port == 8890
    assert result.has_streamid is True
    assert result.mode == "caller"
    assert result.latency_ms == 200


def test_parsed_url_does_not_expose_streamid_in_repr() -> None:
    parsed = parse_srt_url(
        f"srt://ingest.test.local:8890/?streamid={_PLACEHOLDER_STREAMID}"
    )
    # Round-trip through repr / str — the raw streamid must be gone.
    assert _PLACEHOLDER_STREAMID not in repr(parsed)
    assert _PLACEHOLDER_STREAMID not in str(parsed)


def test_parsed_url_does_not_expose_host_in_repr() -> None:
    parsed = parse_srt_url("srt://confidential-host.example:8890/?streamid=x")
    assert "confidential-host.example" not in repr(parsed)


def test_non_srt_scheme_raises() -> None:
    with pytest.raises(SrtUrlError):
        parse_srt_url("rtsp://host:8890/")


def test_missing_scheme_raises() -> None:
    with pytest.raises(SrtUrlError):
        parse_srt_url("host:8890/?streamid=x")


def test_missing_host_raises() -> None:
    with pytest.raises(SrtUrlError):
        parse_srt_url("srt://:8890/?streamid=x")


def test_missing_port_raises() -> None:
    with pytest.raises(SrtUrlError):
        parse_srt_url("srt://host/?streamid=x")


def test_missing_streamid_is_allowed() -> None:
    result = parse_srt_url("srt://host.test:8890/")
    assert result.has_host is True
    assert result.has_port is True
    assert result.port == 8890
    assert result.has_streamid is False
    assert result.mode is None
    assert result.latency_ms is None


def test_omitted_mode_returns_none() -> None:
    result = parse_srt_url(
        f"srt://host.test:8890/?streamid={_PLACEHOLDER_STREAMID}&latency=120"
    )
    assert result.mode is None
    assert result.latency_ms == 120


def test_invalid_latency_value_is_dropped_not_raised() -> None:
    result = parse_srt_url(
        f"srt://host.test:8890/?streamid={_PLACEHOLDER_STREAMID}&latency=not-a-number"
    )
    assert result.has_streamid is True
    assert result.latency_ms is None


def test_mixed_case_scheme_is_accepted() -> None:
    result = parse_srt_url("SRT://host.test:8890/?streamid=x")
    assert result.has_streamid is True


def test_empty_url_raises() -> None:
    with pytest.raises(SrtUrlError):
        parse_srt_url("")


def test_whitespace_only_url_raises() -> None:
    with pytest.raises(SrtUrlError):
        parse_srt_url("   ")


def test_invalid_port_raises() -> None:
    with pytest.raises(SrtUrlError):
        parse_srt_url("srt://host:notaport/")
