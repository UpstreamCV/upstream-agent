"""Local webcam source built on top of OpenCV's ``cv2.VideoCapture``.

Design notes:

* ``cv2`` is imported lazily inside :func:`_import_cv2` so an
  ``ImportError`` becomes a typed :class:`CameraDependencyMissingError`
  with an actionable install hint. The rest of the package (config,
  state, ``upstream init``, ``upstream status``) stays importable on
  machines without OpenCV.
* Width/height/FPS are written to the capture *before* checking
  ``isOpened()`` — some V4L2 drivers refuse property writes after the
  first ``read()``.
* Effective values are read back via ``cv2.get(...)`` and exposed via
  :attr:`info`. They are not guaranteed to equal the requested values:
  the device negotiates. The CLI reports the difference when relevant.
* ``close()`` is idempotent so context-manager use is safe even when
  ``open()`` itself raised.
"""

from __future__ import annotations

import logging
import platform
from typing import Any

from upstream_agent.config import CameraConfig
from upstream_agent.errors import (
    CameraDependencyMissingError,
    CameraOpenError,
    CameraReadError,
    UnsupportedCameraSourceError,
)
from upstream_agent.sources.base import Frame, FrameInfo, VideoSource

logger = logging.getLogger(__name__)


def _import_cv2() -> Any:
    """Import ``cv2`` or raise :class:`CameraDependencyMissingError`."""

    try:
        import cv2  # noqa: PLC0415
    except ImportError as exc:
        raise CameraDependencyMissingError(
            "OpenCV (opencv-python) is required for webcam capture. "
            "Install it with: pip install opencv-python"
        ) from exc
    return cv2


class WebcamSource(VideoSource):
    """A local webcam exposed via OpenCV's ``VideoCapture``."""

    def __init__(self, cfg: CameraConfig) -> None:
        if cfg.source_type != "webcam":
            raise UnsupportedCameraSourceError(
                f"WebcamSource only supports source_type 'webcam', got '{cfg.source_type}'."
            )
        self._cfg = cfg
        self._cap: Any = None
        self._info: FrameInfo | None = None
        self._last_error: str | None = None

    @property
    def is_open(self) -> bool:
        return self._cap is not None

    @property
    def info(self) -> FrameInfo | None:
        return self._info

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def open(self) -> None:
        cv2 = _import_cv2()
        source = self._cfg.source

        # On macOS, request the AVFoundation backend explicitly so the
        # default backend selection isn't ambiguous (OpenCV could
        # otherwise pick a different backend depending on its build).
        # On other platforms (Linux/Windows) leave the backend choice
        # to OpenCV's default — passing AVFoundation there would fail.
        cap_backend = getattr(cv2, "CAP_AVFOUNDATION", None)
        if platform.system() == "Darwin" and cap_backend is not None:
            cap = cv2.VideoCapture(source, cap_backend)
        else:
            cap = cv2.VideoCapture(source)

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._cfg.requested_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._cfg.requested_height)
        cap.set(cv2.CAP_PROP_FPS, self._cfg.requested_fps)

        if not cap.isOpened():
            cap.release()
            self._last_error = f"VideoCapture.isOpened() returned False for source {source!r}."
            logger.warning("webcam open failed: %s", self._last_error)
            raise CameraOpenError(
                f"Could not open webcam at index {source}. "
                f"Check that the camera is connected, not already in use, "
                f"and that the source index in your config is correct."
            )

        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = float(cap.get(cv2.CAP_PROP_FPS))

        self._cap = cap
        self._info = FrameInfo(width=width, height=height, fps=fps)
        logger.info(
            "webcam opened: source=%s effective=%dx%d@%.2f",
            source,
            width,
            height,
            fps,
        )

    def read_frame(self) -> tuple[bool, Frame | None]:
        if self._cap is None:
            raise CameraReadError("Camera is not open. Call open() first.")
        ok, frame = self._cap.read()
        if not ok:
            self._last_error = "VideoCapture.read() returned no frame."
            logger.warning("webcam read failed: %s", self._last_error)
            raise CameraReadError(
                "Camera was opened but failed to read a frame. "
                "The device may have been disconnected or is producing no data."
            )
        return True, frame

    def close(self) -> None:
        if self._cap is None:
            return
        try:
            self._cap.release()
        finally:
            self._cap = None
