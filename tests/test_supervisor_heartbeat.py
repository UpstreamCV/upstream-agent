"""Heartbeat-lifecycle tests for :func:`upstream_agent.supervisor.run_agent`.

The existing ``test_supervisor.py`` covers the M5 cleanup ordering and
secret-leak guards. This file focuses on the M6 additions:

* The heartbeat reporter starts after FFmpeg.
* ``stats.record_frame`` is invoked for every frame written.
* Cleanup stops the reporter before FFmpeg/camera/session.
* A fatal heartbeat reason ends the loop with
  ``exit_reason == "fatal_heartbeat"``.
* With ``state.agent_id=None`` the reporter is skipped and the stream
  still runs.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import pytest

from tests.test_supervisor import (  # type: ignore[import-not-found]
    DEVICE_ID,
    SECRET_API_TOKEN,
    SECRET_SRT_URL,
    FakeApiClient,
    FakeCamera,
    FakeFfmpeg,
)
from upstream_agent.config import (
    ApiConfig,
    CameraConfig,
    Config,
    LoggingConfig,
    StreamConfig,
)
from upstream_agent.diagnostics.ffmpeg_check import FfmpegProbeResult
from upstream_agent.runtime.stats import RuntimeStats
from upstream_agent.state import State
from upstream_agent.supervisor import RunResult, run_agent

# ---------------------------------------------------------------------------
# FakeReporter (records lifecycle events; optionally signals fatal)
# ---------------------------------------------------------------------------


class FakeReporter:
    """Lifecycle-tracking stand-in for :class:`HeartbeatReporter`."""

    def __init__(
        self,
        *,
        client: Any,
        agent_id: str,
        camera_id: str | None,
        session_id: str | None,
        agent_version: str,
        approved: Any,
        stats: RuntimeStats,
        fatal_after_calls: int | None = None,
        fatal_reason: str = "ApiTokenRevokedError",
    ) -> None:
        self.client = client
        self.agent_id = agent_id
        self.camera_id = camera_id
        self.session_id = session_id
        self.agent_version = agent_version
        self.approved = approved
        self.stats = stats
        self.start_calls = 0
        self.stop_calls = 0
        self._fatal_after_calls = fatal_after_calls
        self._fatal_reason = fatal_reason
        self._frame_observations = 0
        self._stored_reason: str | None = None
        self._started_event = threading.Event()

    def start(self) -> None:
        self.start_calls += 1
        self._started_event.set()

    def stop(self, timeout: float = 2.0) -> None:
        self.stop_calls += 1

    def fatal_reason(self) -> str | None:
        if self._fatal_after_calls is None:
            return self._stored_reason
        # Each call to fatal_reason() is the supervisor checking after
        # writing a frame; count those as "ticks" so tests can trigger
        # fatal exit deterministically.
        self._frame_observations += 1
        if self._frame_observations >= self._fatal_after_calls:
            self._stored_reason = self._fatal_reason
        return self._stored_reason


def _cfg() -> Config:
    return Config(
        api=ApiConfig(base_url="http://api.test.local", token=SECRET_API_TOKEN),
        camera=CameraConfig(
            name="Front Door",
            source_type="webcam",
            source=0,
            requested_width=1280,
            requested_height=720,
            requested_fps=30,
        ),
        stream=StreamConfig(latency_mode="balanced"),
        logging=LoggingConfig(),
    )


def _state_with_agent(agent_id: str | None = "agent_1") -> State:
    return State(agent_id=agent_id, device_id=DEVICE_ID, last_camera_id=None)


def _happy_probe() -> FfmpegProbeResult:
    return FfmpegProbeResult(
        available=True, version="9.0.0", srt_supported=True,
        h264_encoder="libx264", error=None,
    )


def _make_factory_kit(
    *,
    fake_reporter_kwargs: dict | None = None,
    cam: FakeCamera | None = None,
    client: FakeApiClient | None = None,
):
    cam = cam or FakeCamera(_cfg().camera)
    client = client or FakeApiClient()
    fake_reporter_holder: list[FakeReporter] = []
    fake_ffmpeg_holder: list[FakeFfmpeg] = []
    fake_reporter_kwargs = fake_reporter_kwargs or {}

    def camera_factory(_: CameraConfig) -> Any:
        return cam

    def client_factory(_: ApiConfig) -> Any:
        return client

    def ffmpeg_factory(cmd: list[str], redacted: list[str]) -> Any:
        ff = FakeFfmpeg(cmd, redacted)
        fake_ffmpeg_holder.append(ff)
        return ff

    def reporter_factory(**kwargs: Any) -> Any:
        reporter = FakeReporter(**kwargs, **fake_reporter_kwargs)
        fake_reporter_holder.append(reporter)
        return reporter

    return dict(
        cam=cam,
        client=client,
        ffmpeg_holder=fake_ffmpeg_holder,
        reporter_holder=fake_reporter_holder,
        camera_factory=camera_factory,
        client_factory=client_factory,
        ffmpeg_factory=ffmpeg_factory,
        reporter_factory=reporter_factory,
    )


# ---------------------------------------------------------------------------
# Happy path: reporter start, frame stats, ordered stop
# ---------------------------------------------------------------------------


def test_reporter_started_after_ffmpeg_and_stopped_during_cleanup() -> None:
    kit = _make_factory_kit()
    events: list[tuple[str, str]] = []

    def on_event(stage: str, message: str) -> None:
        events.append((stage, message))

    run_agent(
        _cfg(), _state_with_agent(),
        camera_factory=kit["camera_factory"],
        client_factory=kit["client_factory"],
        ffmpeg_factory=kit["ffmpeg_factory"],
        reporter_factory=kit["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        on_event=on_event,
        max_frames=3,
    )

    assert len(kit["reporter_holder"]) == 1
    reporter: FakeReporter = kit["reporter_holder"][0]
    ffmpeg: FakeFfmpeg = kit["ffmpeg_holder"][0]

    # Reporter starts exactly once and stops exactly once.
    assert reporter.start_calls == 1
    assert reporter.stop_calls == 1

    # Heartbeat is wired with the correct identity.
    assert reporter.agent_id == "agent_1"
    assert reporter.camera_id == "cam_1"
    assert reporter.session_id == "sess_99"

    # Event ordering: heartbeat_started after ffmpeg_started, and
    # heartbeat_stopped before ffmpeg_stopped during cleanup.
    stages = [s for s, _ in events]
    assert stages.index("heartbeat_started") > stages.index("ffmpeg_started")
    assert stages.index("heartbeat_stopped") < stages.index("ffmpeg_stopped")

    # Frame loop wrote three frames.
    assert len(ffmpeg.write_calls) == 3


def test_runtime_stats_record_frame_called_for_every_frame() -> None:
    """The frame loop must call ``stats.record_frame()`` once per
    successful write. The FakeReporter exposes the stats object passed
    in, so we can read frames_written from it."""

    kit = _make_factory_kit()
    run_agent(
        _cfg(), _state_with_agent(),
        camera_factory=kit["camera_factory"],
        client_factory=kit["client_factory"],
        ffmpeg_factory=kit["ffmpeg_factory"],
        reporter_factory=kit["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=4,
    )
    reporter: FakeReporter = kit["reporter_holder"][0]
    snap = reporter.stats.snapshot()
    assert snap.frames_written == 4
    # camera_connected and ffmpeg_running were both flipped on then
    # off by the supervisor; the final snapshot reflects the post-loop
    # state (ffmpeg_running=False, camera_connected=True — close()
    # does not flip camera_connected back, which is fine for M6).
    assert snap.ffmpeg_running is False


# ---------------------------------------------------------------------------
# Fatal heartbeat → exit_reason
# ---------------------------------------------------------------------------


def test_fatal_heartbeat_exits_loop_with_correct_reason() -> None:
    kit = _make_factory_kit(
        fake_reporter_kwargs={"fatal_after_calls": 2},
    )

    result, _ = run_agent(
        _cfg(), _state_with_agent(),
        camera_factory=kit["camera_factory"],
        client_factory=kit["client_factory"],
        ffmpeg_factory=kit["ffmpeg_factory"],
        reporter_factory=kit["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=100,
    )
    assert isinstance(result, RunResult)
    assert result.exit_reason == "fatal_heartbeat"
    # Loop exited well before max_frames.
    assert result.frames_written < 100

    reporter: FakeReporter = kit["reporter_holder"][0]
    # Reporter stop was still invoked during cleanup.
    assert reporter.stop_calls == 1
    # Camera/FFmpeg/session were cleaned up.
    assert kit["ffmpeg_holder"][0].stop_calls == 1
    assert kit["cam"].close_calls == 1
    assert kit["client"].stop_session_calls == ["sess_99"]


# ---------------------------------------------------------------------------
# Missing agent_id: reporter is skipped
# ---------------------------------------------------------------------------


def test_missing_agent_id_skips_reporter_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If `_register_if_needed` somehow returns without setting
    state.agent_id, the supervisor must log a warning and run without
    a reporter — never crash the stream."""

    caplog.set_level(logging.WARNING)
    kit = _make_factory_kit()

    # Patch the supervisor's registration helper to strip agent_id off
    # the working state after registration completes. This drives the
    # ``agent_id is None`` branch deterministically without faking the
    # API client's responses.
    from upstream_agent import supervisor as supervisor_module

    real = supervisor_module._register_if_needed

    def patched(*, cfg, state, client, capabilities, save_progress):
        new_state = real(
            cfg=cfg,
            state=state,
            client=client,
            capabilities=capabilities,
            save_progress=save_progress,
        )
        if new_state is not None:
            new_state = new_state.model_copy(update={"agent_id": None})
        return new_state

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(supervisor_module, "_register_if_needed", patched)
        result, _ = run_agent(
            _cfg(), _state_with_agent(agent_id=None),
            camera_factory=kit["camera_factory"],
            client_factory=kit["client_factory"],
            ffmpeg_factory=kit["ffmpeg_factory"],
            reporter_factory=kit["reporter_factory"],
            ffmpeg_probe=_happy_probe(),
            max_frames=2,
        )
    finally:
        monkeypatch.undo()

    # No reporter was constructed.
    assert kit["reporter_holder"] == []
    # Stream still ran to completion.
    assert result.frames_written == 2
    # Warning was logged.
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("agent_id missing" in m for m in msgs)


# ---------------------------------------------------------------------------
# Cleanup ordering on KeyboardInterrupt
# ---------------------------------------------------------------------------


def test_keyboard_interrupt_stops_reporter_before_ffmpeg() -> None:
    bad_cam = FakeCamera(
        _cfg().camera,
        read_raises_after=2,
        read_raises=KeyboardInterrupt(),
    )
    kit = _make_factory_kit(cam=bad_cam)
    events: list[tuple[str, str]] = []

    def on_event(stage: str, message: str) -> None:
        events.append((stage, message))

    with pytest.raises(KeyboardInterrupt):
        run_agent(
            _cfg(), _state_with_agent(),
            camera_factory=kit["camera_factory"],
            client_factory=kit["client_factory"],
            ffmpeg_factory=kit["ffmpeg_factory"],
            reporter_factory=kit["reporter_factory"],
            ffmpeg_probe=_happy_probe(),
            on_event=on_event,
            max_frames=10,
        )

    reporter: FakeReporter = kit["reporter_holder"][0]
    assert reporter.stop_calls == 1
    stages = [s for s, _ in events]
    assert stages.index("heartbeat_stopped") < stages.index("ffmpeg_stopped")


# ---------------------------------------------------------------------------
# Leak guard
# ---------------------------------------------------------------------------


def test_reporter_receives_no_secrets_via_kwargs() -> None:
    """The reporter is built with agent_id, camera_id, session_id, and
    the approved config — never the SRT URL, streamid, or session
    token."""

    kit = _make_factory_kit()
    run_agent(
        _cfg(), _state_with_agent(),
        camera_factory=kit["camera_factory"],
        client_factory=kit["client_factory"],
        ffmpeg_factory=kit["ffmpeg_factory"],
        reporter_factory=kit["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=1,
    )
    reporter: FakeReporter = kit["reporter_holder"][0]
    text = (
        repr(reporter.agent_id)
        + repr(reporter.camera_id)
        + repr(reporter.session_id)
        + repr(reporter.approved)
    )
    assert SECRET_SRT_URL not in text
    assert SECRET_API_TOKEN not in text
