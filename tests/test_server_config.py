"""camera_config_from_server: server camera record -> local CameraConfig."""

from __future__ import annotations

import pytest

from upstream_agent.api.schemas import Camera
from upstream_agent.errors import ConfigError
from upstream_agent.server_config import camera_config_from_server


def _camera(**overrides: object) -> Camera:
    base: dict[str, object] = {
        "id": "cam-11111111",
        "name": "Front Door",
        "source_type": "webcam",
        "device_path": "webcam:0",
        "default_width": 640,
        "default_height": 360,
        "default_fps": 15,
    }
    base.update(overrides)
    return Camera.model_validate(base)


def test_maps_webcam_camera_with_server_defaults() -> None:
    cfg = camera_config_from_server(_camera())
    assert cfg.source_type == "webcam"
    assert cfg.source == 0
    assert (cfg.requested_width, cfg.requested_height, cfg.requested_fps) == (640, 360, 15)
    assert cfg.name == "Front Door"


def test_fills_defaults_when_server_omits_config() -> None:
    cfg = camera_config_from_server(
        _camera(default_width=None, default_height=None, default_fps=None, name=None)
    )
    assert (cfg.requested_width, cfg.requested_height, cfg.requested_fps) == (1280, 720, 30)
    assert cfg.name == "camera-cam-1111"


def test_rtsp_camera_is_rejected_with_clear_message() -> None:
    """RTSP capture isn't implemented in this build (webcam-only since
    2026-06-10) — the shell must show an honest error, not a pydantic
    ValidationError."""

    cam = _camera(source_type="rtsp", device_path="rtsp:rtsp://cam.local/stream")
    with pytest.raises(ConfigError) as exc:
        camera_config_from_server(cam)
    assert "cannot capture yet" in str(exc.value)


def test_unknown_source_type_is_rejected() -> None:
    cam = _camera(source_type="file", device_path=None)
    with pytest.raises(ConfigError):
        camera_config_from_server(cam)


def test_mismatched_type_and_path_is_rejected() -> None:
    cam = _camera(source_type="rtsp", device_path="webcam:0")
    with pytest.raises(ConfigError) as exc:
        camera_config_from_server(cam)
    assert "Fix the camera's configuration" in str(exc.value)


def test_non_integer_webcam_index_is_rejected() -> None:
    cam = _camera(device_path="webcam:abc")
    with pytest.raises(ConfigError):
        camera_config_from_server(cam)


def test_missing_device_path_is_rejected() -> None:
    cam = _camera(device_path=None, source_type=None)
    with pytest.raises(ConfigError):
        camera_config_from_server(cam)
