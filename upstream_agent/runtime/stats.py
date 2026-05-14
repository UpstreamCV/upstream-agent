"""Thread-safe runtime stats shared between the frame loop and heartbeat.

The supervisor calls :meth:`RuntimeStats.record_frame` on every frame
written to FFmpeg. The heartbeat reporter (on its own daemon thread)
calls :meth:`RuntimeStats.snapshot` once per interval to build the
heartbeat payload.

The snapshot is a frozen dataclass: the reader holds a stable view
of the counters and cannot accidentally mutate live state. All
mutation paths take a small ``threading.Lock`` so concurrent writers
and readers see consistent values.

FPS is computed over a sliding window of the most recent
:data:`FPS_WINDOW_FRAMES` monotonic timestamps. It is ``None`` until
at least two frames have been recorded.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

FPS_WINDOW_FRAMES: int = 60


@dataclass(frozen=True)
class StatsSnapshot:
    """Immutable view of :class:`RuntimeStats` at one point in time.

    ``fps`` is ``None`` until at least two frames have been recorded.
    ``last_frame_at`` is the wall-clock UTC datetime of the most
    recent frame; ``None`` if no frames have been recorded yet.
    """

    frames_written: int
    fps: float | None
    camera_connected: bool
    ffmpeg_running: bool
    last_frame_at: datetime | None
    last_error: str | None


class RuntimeStats:
    """Thread-safe counters for the live streaming runtime."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frames_written: int = 0
        self._frame_times: deque[float] = deque(maxlen=FPS_WINDOW_FRAMES)
        self._camera_connected: bool = False
        self._ffmpeg_running: bool = False
        self._last_frame_at: datetime | None = None
        self._last_error: str | None = None

    def record_frame(
        self,
        *,
        monotonic: float | None = None,
        wall_clock: datetime | None = None,
    ) -> None:
        """Mark that one frame was successfully written to FFmpeg.

        The two optional parameters exist so tests can drive the FPS
        window deterministically. Production callers pass nothing and
        the method uses :func:`time.monotonic` and
        :func:`datetime.now`.
        """

        mono = monotonic if monotonic is not None else time.monotonic()
        wall = wall_clock if wall_clock is not None else datetime.now(tz=timezone.utc)
        with self._lock:
            self._frames_written += 1
            self._frame_times.append(mono)
            self._last_frame_at = wall

    def set_camera_connected(self, value: bool) -> None:
        with self._lock:
            self._camera_connected = value

    def set_ffmpeg_running(self, value: bool) -> None:
        with self._lock:
            self._ffmpeg_running = value

    def record_error(self, message: str | None) -> None:
        """Set or clear the most recent error message.

        The heartbeat schema caps ``last_error`` at 512 characters;
        this method truncates anything longer so the next heartbeat
        payload always validates.
        """

        if message is not None and len(message) > 512:
            message = message[:512]
        with self._lock:
            self._last_error = message

    def snapshot(self) -> StatsSnapshot:
        with self._lock:
            fps = self._compute_fps_locked()
            return StatsSnapshot(
                frames_written=self._frames_written,
                fps=fps,
                camera_connected=self._camera_connected,
                ffmpeg_running=self._ffmpeg_running,
                last_frame_at=self._last_frame_at,
                last_error=self._last_error,
            )

    def _compute_fps_locked(self) -> float | None:
        if len(self._frame_times) < 2:
            return None
        span = self._frame_times[-1] - self._frame_times[0]
        if span <= 0:
            return None
        return (len(self._frame_times) - 1) / span
