"""Tests for :class:`upstream_agent.runtime.stats.RuntimeStats`.

The stats object is updated from the supervisor's frame loop and read
from the heartbeat reporter thread, so behaviour under concurrent
access matters as much as the per-method semantics.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from upstream_agent.runtime.stats import (
    FPS_WINDOW_FRAMES,
    RuntimeStats,
    StatsSnapshot,
)


def test_initial_snapshot_is_empty() -> None:
    stats = RuntimeStats()
    snap = stats.snapshot()
    assert isinstance(snap, StatsSnapshot)
    assert snap.frames_written == 0
    assert snap.fps is None
    assert snap.camera_connected is False
    assert snap.ffmpeg_running is False
    assert snap.last_frame_at is None
    assert snap.last_error is None


def test_record_frame_increments_counter_and_last_frame_at() -> None:
    stats = RuntimeStats()
    when = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)
    stats.record_frame(monotonic=1.0, wall_clock=when)
    snap = stats.snapshot()
    assert snap.frames_written == 1
    assert snap.last_frame_at == when


def test_fps_is_none_until_two_frames() -> None:
    stats = RuntimeStats()
    assert stats.snapshot().fps is None
    stats.record_frame(monotonic=10.0)
    assert stats.snapshot().fps is None
    stats.record_frame(monotonic=11.0)
    snap = stats.snapshot()
    assert snap.fps is not None
    # Two frames 1.0s apart -> (2 - 1) / 1.0 = 1.0 fps.
    assert snap.fps == 1.0


def test_fps_uses_sliding_window() -> None:
    stats = RuntimeStats()
    # Record exactly FPS_WINDOW_FRAMES + 1 frames at 1/30 sec spacing
    # to overflow the window and confirm older samples are dropped.
    base = 100.0
    for i in range(FPS_WINDOW_FRAMES + 1):
        stats.record_frame(monotonic=base + i * (1 / 30))
    snap = stats.snapshot()
    assert snap.fps is not None
    # Window holds the last FPS_WINDOW_FRAMES timestamps; their span is
    # (FPS_WINDOW_FRAMES - 1) / 30 seconds and there are
    # (FPS_WINDOW_FRAMES - 1) intervals — exactly 30 fps.
    assert abs(snap.fps - 30.0) < 0.01


def test_fps_returns_none_for_zero_or_negative_span() -> None:
    stats = RuntimeStats()
    # Two frames at the exact same monotonic instant -> span 0 -> fps
    # is None rather than ZeroDivisionError.
    stats.record_frame(monotonic=5.0)
    stats.record_frame(monotonic=5.0)
    assert stats.snapshot().fps is None


def test_set_camera_and_ffmpeg_flags() -> None:
    stats = RuntimeStats()
    stats.set_camera_connected(True)
    stats.set_ffmpeg_running(True)
    snap = stats.snapshot()
    assert snap.camera_connected is True
    assert snap.ffmpeg_running is True
    stats.set_ffmpeg_running(False)
    assert stats.snapshot().ffmpeg_running is False


def test_record_error_round_trip() -> None:
    stats = RuntimeStats()
    stats.record_error("camera read failed")
    assert stats.snapshot().last_error == "camera read failed"
    stats.record_error(None)
    assert stats.snapshot().last_error is None


def test_record_error_truncates_to_512_chars() -> None:
    """Heartbeat schema caps last_error at 512 chars; the stats object
    truncates so the next heartbeat payload always validates."""

    stats = RuntimeStats()
    huge = "x" * 1000
    stats.record_error(huge)
    captured = stats.snapshot().last_error
    assert captured is not None
    assert len(captured) == 512


def test_thread_safety_concurrent_record_frame() -> None:
    """Eight writers each writing 1000 frames should produce exactly
    8000 in frames_written — no torn updates."""

    stats = RuntimeStats()
    workers = 8
    per_worker = 1000

    def worker() -> None:
        for _ in range(per_worker):
            stats.record_frame(monotonic=0.0)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert stats.snapshot().frames_written == workers * per_worker


def test_snapshot_is_a_frozen_dataclass() -> None:
    """Readers cannot mutate live state via the snapshot."""

    import dataclasses

    stats = RuntimeStats()
    snap = stats.snapshot()
    # `frozen=True` raises FrozenInstanceError on assignment.
    import pytest

    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.frames_written = 99  # type: ignore[misc]
