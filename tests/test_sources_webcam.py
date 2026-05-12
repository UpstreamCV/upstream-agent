"""Tests for :class:`upstream_agent.sources.webcam.WebcamSource`.

Strategy: install a fake ``cv2`` module via
``monkeypatch.setitem(sys.modules, "cv2", fake_cv2)`` *before*
:func:`WebcamSource._import_cv2` runs. The fake exposes the property
constants and a configurable ``VideoCapture`` factory whose instances
track ``set()`` / ``release()`` calls so the tests can assert the
exact interaction with OpenCV without needing real hardware.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from upstream_agent.config import CameraConfig
from upstream_agent.errors import (
    CameraDependencyMissingError,
    CameraOpenError,
    CameraReadError,
    UnsupportedCameraSourceError,
)

CAP_PROP_FRAME_WIDTH = 3
CAP_PROP_FRAME_HEIGHT = 4
CAP_PROP_FPS = 5


class FakeVideoCapture:
    """Stand-in for ``cv2.VideoCapture``."""

    def __init__(
        self,
        opens: bool = True,
        read_returns: tuple[bool, Any] = (True, "fake-frame"),
        effective_width: int = 1280,
        effective_height: int = 720,
        effective_fps: float = 30.0,
    ) -> None:
        self._opens = opens
        self._read_returns = read_returns
        self._props = {
            CAP_PROP_FRAME_WIDTH: float(effective_width),
            CAP_PROP_FRAME_HEIGHT: float(effective_height),
            CAP_PROP_FPS: float(effective_fps),
        }
        self.set_calls: list[tuple[int, float]] = []
        self.read_calls: int = 0
        self.release_count: int = 0
        self.is_opened_calls: int = 0

    def isOpened(self) -> bool:  # noqa: N802  (mimics OpenCV API)
        self.is_opened_calls += 1
        return self._opens

    def set(self, prop: int, value: float) -> bool:  # noqa: A003
        self.set_calls.append((prop, value))
        return True

    def get(self, prop: int) -> float:
        return self._props.get(prop, 0.0)

    def read(self) -> tuple[bool, Any]:
        self.read_calls += 1
        return self._read_returns

    def release(self) -> None:
        self.release_count += 1


class CaptureFactory:
    """Configurable factory used to drive successive ``VideoCapture(...)`` calls."""

    def __init__(self) -> None:
        self._next: dict[str, Any] = {}
        self.instances: list[FakeVideoCapture] = []
        self.constructed_with: list[Any] = []

    def configure(self, **kwargs: Any) -> None:
        self._next = kwargs

    def __call__(self, source: Any) -> FakeVideoCapture:
        self.constructed_with.append(source)
        cap = FakeVideoCapture(**self._next)
        self.instances.append(cap)
        return cap

    @property
    def last(self) -> FakeVideoCapture:
        return self.instances[-1]


@pytest.fixture
def fake_cv2(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """Install a fake ``cv2`` module before WebcamSource imports it."""

    factory = CaptureFactory()
    fake = types.SimpleNamespace(
        CAP_PROP_FRAME_WIDTH=CAP_PROP_FRAME_WIDTH,
        CAP_PROP_FRAME_HEIGHT=CAP_PROP_FRAME_HEIGHT,
        CAP_PROP_FPS=CAP_PROP_FPS,
        VideoCapture=factory,
    )
    monkeypatch.setitem(sys.modules, "cv2", fake)
    return fake


def _cfg(
    source: int | str = 0,
    width: int = 1280,
    height: int = 720,
    fps: int = 30,
    source_type: str = "webcam",
) -> CameraConfig:
    return CameraConfig(
        name="Test Camera",
        source_type=source_type,
        source=source,
        requested_width=width,
        requested_height=height,
        requested_fps=fps,
    )


# ---------------------------------------------------------------------------
# Constructor / dispatch
# ---------------------------------------------------------------------------


def test_webcam_source_rejects_non_webcam_source_type() -> None:
    from upstream_agent.sources.webcam import WebcamSource

    cfg = _cfg(source="rtsp://example/stream", source_type="rtsp")
    with pytest.raises(UnsupportedCameraSourceError):
        WebcamSource(cfg)


def test_webcam_source_missing_cv2_raises_dependency_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force `import cv2` inside _import_cv2 to fail. Setting the module
    # to None makes `import cv2` raise ImportError under Python's import
    # machinery.
    monkeypatch.setitem(sys.modules, "cv2", None)

    from upstream_agent.sources.webcam import WebcamSource

    src = WebcamSource(_cfg())
    with pytest.raises(CameraDependencyMissingError) as excinfo:
        src.open()
    assert "opencv-python" in str(excinfo.value)


# ---------------------------------------------------------------------------
# open()
# ---------------------------------------------------------------------------


def test_open_sets_width_height_fps_before_checking_isopened(
    fake_cv2: types.SimpleNamespace,
) -> None:
    fake_cv2.VideoCapture.configure(
        opens=True, effective_width=1280, effective_height=720, effective_fps=30.0
    )
    from upstream_agent.sources.webcam import WebcamSource

    src = WebcamSource(_cfg(width=1280, height=720, fps=30))
    src.open()

    cap = fake_cv2.VideoCapture.last
    # All three property writes must precede the isOpened check.
    props_written = [prop for prop, _ in cap.set_calls]
    assert CAP_PROP_FRAME_WIDTH in props_written
    assert CAP_PROP_FRAME_HEIGHT in props_written
    assert CAP_PROP_FPS in props_written
    assert cap.is_opened_calls >= 1
    # Width/height/fps values match the request.
    set_dict = dict(cap.set_calls)
    assert set_dict[CAP_PROP_FRAME_WIDTH] == 1280
    assert set_dict[CAP_PROP_FRAME_HEIGHT] == 720
    assert set_dict[CAP_PROP_FPS] == 30


def test_open_raises_camera_open_error_and_releases_when_not_opened(
    fake_cv2: types.SimpleNamespace,
) -> None:
    fake_cv2.VideoCapture.configure(opens=False)
    from upstream_agent.sources.webcam import WebcamSource

    src = WebcamSource(_cfg(source=0))
    with pytest.raises(CameraOpenError) as excinfo:
        src.open()
    # Message must guide the user.
    assert "Could not open webcam" in str(excinfo.value)
    assert "index 0" in str(excinfo.value)
    # Handle was released on failure so we don't leak.
    assert fake_cv2.VideoCapture.last.release_count == 1
    # Last error is captured for inspection.
    assert src.last_error is not None
    assert src.is_open is False
    assert src.info is None


def test_open_records_effective_frame_info(
    fake_cv2: types.SimpleNamespace,
) -> None:
    fake_cv2.VideoCapture.configure(
        opens=True, effective_width=640, effective_height=480, effective_fps=15.0
    )
    from upstream_agent.sources.webcam import WebcamSource

    src = WebcamSource(_cfg(width=1920, height=1080, fps=60))
    src.open()
    try:
        assert src.is_open is True
        assert src.info is not None
        # Reflects what the device returned, not what was requested.
        assert src.info.width == 640
        assert src.info.height == 480
        assert src.info.fps == 15.0
    finally:
        src.close()


def test_open_passes_source_index_to_videocapture(
    fake_cv2: types.SimpleNamespace,
) -> None:
    fake_cv2.VideoCapture.configure(opens=True)
    from upstream_agent.sources.webcam import WebcamSource

    src = WebcamSource(_cfg(source=2))
    src.open()
    try:
        assert fake_cv2.VideoCapture.constructed_with == [2]
    finally:
        src.close()


# ---------------------------------------------------------------------------
# read_frame()
# ---------------------------------------------------------------------------


def test_read_frame_returns_frame_on_success(
    fake_cv2: types.SimpleNamespace,
) -> None:
    fake_cv2.VideoCapture.configure(opens=True, read_returns=(True, "the-frame"))
    from upstream_agent.sources.webcam import WebcamSource

    with WebcamSource(_cfg()) as src:
        ok, frame = src.read_frame()
    assert ok is True
    assert frame == "the-frame"


def test_read_frame_raises_camera_read_error_when_read_fails(
    fake_cv2: types.SimpleNamespace,
) -> None:
    fake_cv2.VideoCapture.configure(opens=True, read_returns=(False, None))
    from upstream_agent.sources.webcam import WebcamSource

    src = WebcamSource(_cfg())
    src.open()
    try:
        with pytest.raises(CameraReadError):
            src.read_frame()
        assert src.last_error is not None
    finally:
        src.close()


def test_read_frame_before_open_raises_camera_read_error(
    fake_cv2: types.SimpleNamespace,
) -> None:
    from upstream_agent.sources.webcam import WebcamSource

    src = WebcamSource(_cfg())
    with pytest.raises(CameraReadError):
        src.read_frame()


# ---------------------------------------------------------------------------
# close() / context manager
# ---------------------------------------------------------------------------


def test_context_manager_releases_even_when_read_fails(
    fake_cv2: types.SimpleNamespace,
) -> None:
    fake_cv2.VideoCapture.configure(opens=True, read_returns=(False, None))
    from upstream_agent.sources.webcam import WebcamSource

    with pytest.raises(CameraReadError), WebcamSource(_cfg()) as src:
        src.read_frame()

    assert fake_cv2.VideoCapture.last.release_count == 1


def test_close_is_idempotent(
    fake_cv2: types.SimpleNamespace,
) -> None:
    fake_cv2.VideoCapture.configure(opens=True)
    from upstream_agent.sources.webcam import WebcamSource

    src = WebcamSource(_cfg())
    src.open()
    src.close()
    src.close()  # second call must not raise and must not double-release
    assert fake_cv2.VideoCapture.last.release_count == 1
    assert src.is_open is False


def test_close_before_open_is_safe(
    fake_cv2: types.SimpleNamespace,
) -> None:
    from upstream_agent.sources.webcam import WebcamSource

    src = WebcamSource(_cfg())
    src.close()  # no underlying cap yet; must not raise
    assert src.is_open is False
