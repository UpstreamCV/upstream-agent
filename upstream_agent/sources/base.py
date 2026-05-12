"""Abstract base for video sources.

A :class:`VideoSource` is the agent's adapter to whatever produces
frames — a local webcam today, an RTSP stream tomorrow, a video file
for tests, a PTZ/IP camera in a later milestone. Every source answers
the same five questions:

* Can I be opened? (``open()``)
* What resolution am I currently producing? (``info.width/height``)
* What FPS am I currently producing? (``info.fps``)
* Am I currently connected? (``is_open``)
* What was my last error? (``last_error``)

Concrete sources subclass :class:`VideoSource` and implement the
abstract methods. The base class provides ``__enter__``/``__exit__`` so
every source can be used as a context manager — callers that do so are
guaranteed a ``close()`` call even if ``read_frame()`` raises, which is
how the camera-check orchestrator guarantees resource cleanup.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

Frame = Any
"""Opaque frame type.

:class:`WebcamSource` returns ``numpy.ndarray`` (OpenCV's BGR frame
format). Future sources may yield ``bytes``, ``av.VideoFrame``, or
something else. Callers that need pixel access should narrow with
``isinstance`` at the use site.
"""


@dataclass(frozen=True)
class FrameInfo:
    """Effective parameters reported by an opened source."""

    width: int
    height: int
    fps: float


class VideoSource(ABC):
    """Abstract base for all video sources."""

    @property
    @abstractmethod
    def is_open(self) -> bool:
        """Whether the underlying device/stream is currently open."""

    @property
    @abstractmethod
    def info(self) -> FrameInfo | None:
        """Effective frame info after ``open()``; ``None`` before."""

    @property
    @abstractmethod
    def last_error(self) -> str | None:
        """Human-readable description of the most recent failure, if any."""

    @abstractmethod
    def open(self) -> None:
        """Open the source. Raise :class:`CameraOpenError` on failure."""

    @abstractmethod
    def read_frame(self) -> tuple[bool, Frame | None]:
        """Read one frame. Raise :class:`CameraReadError` on failure."""

    @abstractmethod
    def close(self) -> None:
        """Release resources. Must be safe to call when not open."""

    def __enter__(self) -> VideoSource:
        self.open()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
