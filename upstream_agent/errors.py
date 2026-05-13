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


class ApiError(UpstreamError):
    """Base class for all UpStream API client errors."""


class ApiConnectionError(ApiError):
    """Raised when the API server cannot be reached (timeout, DNS, refused, etc.)."""


class ApiAuthError(ApiError):
    """Raised when the server rejects the credentials (401/403)."""


class ApiTokenRevokedError(ApiAuthError):
    """Raised when the server signals that the token has been revoked."""


class ApiValidationError(ApiError):
    """Raised when the server rejects a request body (400/422)."""


class ApiRateLimitError(ApiError):
    """Raised when the server returns 429."""


class ApiServerError(ApiError):
    """Raised when the server returns 5xx."""


class ApiContractError(ApiError):
    """Raised when an API response is missing required fields or is not valid JSON."""
