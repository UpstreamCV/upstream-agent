"""SRT URL parsing and safe-to-display metadata.

:func:`parse_srt_url` accepts an SRT URL like
``srt://host:8890/?streamid=...&mode=caller&latency=200`` and returns
a :class:`ParsedSrtUrl` describing only what is safe to show: the
boolean presence of host / port / streamid, the port number, the
SRT mode, and the latency. The host, the raw streamid, and the raw
query string are **never** stored on the dataclass — they stay in the
caller's local scope.

Raises :class:`~upstream_agent.errors.SrtUrlError` when the scheme is
not ``srt``, the host is missing, or the port is missing. A missing
``streamid`` is **not** an error: some deployments embed it in the
path, others in a non-query form. Callers can treat
``has_streamid=False`` as a downgrade signal without rejecting.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from upstream_agent.errors import SrtUrlError


@dataclass(frozen=True)
class ParsedSrtUrl:
    """Safe-to-display metadata extracted from an SRT URL.

    Every field is either a boolean, a small integer, or a short
    enumerated string — never a raw URL, hostname, streamid, or token.
    """

    has_host: bool
    has_port: bool
    port: int | None
    has_streamid: bool
    mode: str | None
    latency_ms: int | None


def _first_query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    value = values[0]
    return value if value else None


def parse_srt_url(url: str) -> ParsedSrtUrl:
    """Validate an SRT URL and return safe-to-display metadata.

    Raises :class:`SrtUrlError` for missing or non-SRT schemes, missing
    host, or missing port. The URL string itself is never echoed in
    the error message.
    """

    if not isinstance(url, str) or not url.strip():
        raise SrtUrlError("SRT URL is empty.")

    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        # urlsplit raises ValueError on malformed IPv6 brackets etc.
        raise SrtUrlError(f"SRT URL is malformed: {exc.__class__.__name__}.") from None

    if parts.scheme.lower() != "srt":
        raise SrtUrlError("SRT URL must use the 'srt' scheme.")

    try:
        host = parts.hostname
    except ValueError as exc:
        raise SrtUrlError(f"SRT URL host is malformed: {exc.__class__.__name__}.") from None
    if not host:
        raise SrtUrlError("SRT URL is missing a host.")

    try:
        port = parts.port
    except ValueError:
        raise SrtUrlError("SRT URL port is not a valid integer.") from None
    if port is None or port <= 0:
        raise SrtUrlError("SRT URL is missing a port.")

    query = parse_qs(parts.query, keep_blank_values=False)
    streamid = _first_query_value(query, "streamid")
    mode = _first_query_value(query, "mode")
    latency_raw = _first_query_value(query, "latency")
    latency_ms: int | None
    if latency_raw is None:
        latency_ms = None
    else:
        try:
            latency_ms = int(latency_raw)
        except ValueError:
            latency_ms = None

    return ParsedSrtUrl(
        has_host=True,
        has_port=True,
        port=port,
        has_streamid=bool(streamid),
        mode=mode,
        latency_ms=latency_ms,
    )
