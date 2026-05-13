"""Tests for the exception hierarchy."""

from __future__ import annotations

from upstream_agent.errors import (
    ApiAuthError,
    ApiConnectionError,
    ApiContractError,
    ApiError,
    ApiRateLimitError,
    ApiServerError,
    ApiTokenRevokedError,
    ApiValidationError,
    CameraDependencyMissingError,
    CameraError,
    CameraOpenError,
    CameraReadError,
    ConfigError,
    StateError,
    UnsupportedCameraSourceError,
    UpstreamError,
)


def test_config_error_inherits_upstream_error() -> None:
    assert issubclass(ConfigError, UpstreamError)


def test_state_error_inherits_upstream_error() -> None:
    assert issubclass(StateError, UpstreamError)


def test_camera_error_inherits_upstream_error() -> None:
    assert issubclass(CameraError, UpstreamError)


def test_camera_open_error_inherits_camera_error() -> None:
    assert issubclass(CameraOpenError, CameraError)
    assert issubclass(CameraOpenError, UpstreamError)


def test_camera_read_error_inherits_camera_error() -> None:
    assert issubclass(CameraReadError, CameraError)
    assert issubclass(CameraReadError, UpstreamError)


def test_unsupported_camera_source_error_inherits_camera_error() -> None:
    assert issubclass(UnsupportedCameraSourceError, CameraError)
    assert issubclass(UnsupportedCameraSourceError, UpstreamError)


def test_camera_dependency_missing_error_inherits_camera_error() -> None:
    assert issubclass(CameraDependencyMissingError, CameraError)
    assert issubclass(CameraDependencyMissingError, UpstreamError)


def test_camera_open_error_carries_message() -> None:
    exc = CameraOpenError("could not open index 0")
    assert "could not open index 0" in str(exc)


def test_api_error_inherits_upstream_error() -> None:
    assert issubclass(ApiError, UpstreamError)


def test_api_connection_error_inherits_api_error() -> None:
    assert issubclass(ApiConnectionError, ApiError)
    assert issubclass(ApiConnectionError, UpstreamError)


def test_api_auth_error_inherits_api_error() -> None:
    assert issubclass(ApiAuthError, ApiError)


def test_api_token_revoked_error_inherits_auth_error() -> None:
    assert issubclass(ApiTokenRevokedError, ApiAuthError)
    assert issubclass(ApiTokenRevokedError, ApiError)


def test_api_validation_error_inherits_api_error() -> None:
    assert issubclass(ApiValidationError, ApiError)


def test_api_rate_limit_error_inherits_api_error() -> None:
    assert issubclass(ApiRateLimitError, ApiError)


def test_api_server_error_inherits_api_error() -> None:
    assert issubclass(ApiServerError, ApiError)


def test_api_contract_error_inherits_api_error() -> None:
    assert issubclass(ApiContractError, ApiError)


def test_api_connection_error_carries_message() -> None:
    exc = ApiConnectionError("could not connect to http://example.test")
    assert "example.test" in str(exc)
