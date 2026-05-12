"""Exception hierarchy for upstream-agent.

The CLI catches :class:`UpstreamError` at the top level and reports the
message to stderr before exiting non-zero. All package-raised errors should
inherit from :class:`UpstreamError` so they participate in this contract.
"""

from __future__ import annotations


class UpstreamError(Exception):
    """Base class for all upstream-agent errors."""


class ConfigError(UpstreamError):
    """Raised when the YAML config cannot be loaded or fails validation."""


class StateError(UpstreamError):
    """Raised when the local agent state file cannot be loaded or written."""
