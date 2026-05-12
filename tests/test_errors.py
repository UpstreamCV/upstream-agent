"""Tests for the exception hierarchy."""

from __future__ import annotations

from upstream_agent.errors import (
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
