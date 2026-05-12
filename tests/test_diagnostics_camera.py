"""Tests for :func:`upstream_agent.diagnostics.camera.run_camera_check`.

These tests stub :class:`WebcamSource` so the orchestrator's logic
(success path, open failure, read failure, unsupported source_type) is
exercised without going through OpenCV at all.
"""

from __future__ import annotations

import pytest

from upstream_agent.config import CameraConfig
from upstream_agent.diagnostics import camera as camera_module
from upstream_agent.diagnostics.camera import CameraCheckResult, run_camera_check
from upstream_agent.errors import CameraOpenError, CameraReadError
from upstream_agent.sources.base import FrameInfo, VideoSource


class StubVideoSource(VideoSource):
    """Configurable in-memory VideoSource for orchestrator tests."""

    def __init__(
        self,
        cfg: CameraConfig,
        *,
        open_raises: Exception | None = None,
        read_raises: Exception | None = None,
        info: FrameInfo | None = None,
    ) -> None:
        self._cfg = cfg
        self._open_raises = open_raises
        self._read_raises = read_raises
        self._info = info
        self._is_open = False
        self.close_count = 0

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def info(self) -> FrameInfo | None:
        return self._info if self._is_open else None

    @property
    def last_error(self) -> str | None:
        return None

    def open(self) -> None:
        if self._open_raises is not None:
            raise self._open_raises
        self._is_open = True

    def read_frame(self) -> tuple[bool, object]:
        if self._read_raises is not None:
            raise self._read_raises
        return True, "frame"

    def close(self) -> None:
        self.close_count += 1
        self._is_open = False


def _cfg(source_type: str = "webcam", source: int | str = 0) -> CameraConfig:
    return CameraConfig(
        name="Test",
        source_type=source_type,
        source=source,
        requested_width=1280,
        requested_height=720,
        requested_fps=30,
    )


def _install_stub(
    monkeypatch: pytest.MonkeyPatch,
    **stub_kwargs: object,
) -> list[StubVideoSource]:
    instances: list[StubVideoSource] = []

    def factory(cfg: CameraConfig) -> StubVideoSource:
        stub = StubVideoSource(cfg, **stub_kwargs)  # type: ignore[arg-type]
        instances.append(stub)
        return stub

    monkeypatch.setattr(camera_module, "WebcamSource", factory)
    return instances


def test_happy_path_returns_success_with_frame_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = FrameInfo(width=1280, height=720, fps=30.0)
    instances = _install_stub(monkeypatch, info=info)

    result = run_camera_check(_cfg())

    assert isinstance(result, CameraCheckResult)
    assert result.success is True
    assert result.info == info
    assert result.error is None
    assert result.source_type == "webcam"
    assert result.source == 0
    assert result.requested_width == 1280
    # close() was called once via the context manager.
    assert instances[0].close_count == 1


def test_open_failure_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stub(monkeypatch, open_raises=CameraOpenError("could not open webcam at index 0"))

    result = run_camera_check(_cfg())

    assert result.success is False
    assert result.info is None
    assert result.error is not None
    assert "could not open" in result.error.lower()
    # When __enter__ itself raises, __exit__ does not run — by Python's
    # context-manager contract. The source must clean up its own
    # partial state on open() failure; that's WebcamSource's
    # responsibility, verified separately in test_sources_webcam.


def test_read_failure_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    instances = _install_stub(
        monkeypatch,
        info=FrameInfo(width=1280, height=720, fps=30.0),
        read_raises=CameraReadError("no frame"),
    )

    result = run_camera_check(_cfg())

    assert result.success is False
    assert result.info is None
    assert result.error is not None
    assert "no frame" in result.error
    assert instances[0].close_count == 1


def test_unsupported_source_type_short_circuits_without_constructing_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: list[CameraConfig] = []

    def factory(cfg: CameraConfig) -> object:
        constructed.append(cfg)
        raise AssertionError("WebcamSource must not be constructed for rtsp")

    monkeypatch.setattr(camera_module, "WebcamSource", factory)

    cfg = _cfg(source_type="rtsp", source="rtsp://example/stream")
    result = run_camera_check(cfg)

    assert result.success is False
    assert result.error is not None
    assert "rtsp" in result.error
    assert "not supported yet" in result.error
    assert constructed == []
