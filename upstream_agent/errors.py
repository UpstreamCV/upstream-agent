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


class CameraError(UpstreamError):
    """Base class for camera-related failures."""


class CameraOpenError(CameraError):
    """Raised when a camera device cannot be opened."""


class CameraReadError(CameraError):
    """Raised when reading a frame from an opened camera fails."""


class UnsupportedCameraSourceError(CameraError):
    """Raised when a configured ``source_type`` is not implemented in this build."""


class CameraDependencyMissingError(CameraError):
    """Raised when a required camera system library (e.g. OpenCV) is not installed."""
