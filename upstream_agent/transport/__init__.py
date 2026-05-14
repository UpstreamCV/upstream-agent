"""SRT transport package.

For Milestone 4 this contains only the SRT URL parser/validator. The
actual SRT push transport (used by Milestone 5+) will land alongside
it in :mod:`upstream_agent.transport.srt` later.
"""

from upstream_agent.transport.srt import ParsedSrtUrl, parse_srt_url

__all__ = ["ParsedSrtUrl", "parse_srt_url"]
