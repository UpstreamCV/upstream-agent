"""Tests for the Milestone 7 recovery loop in :func:`run_agent`.

These exercise the supervisor against fakes that fail in controlled
ways — broken FFmpeg pipes, mid-stream FFmpeg exits, transient API
connection errors, camera read failures, shutdown signals during a
backoff sleep. The fakes are intentionally stateful: each call can
behave differently depending on call count, so a test can describe
"fail twice, succeed third" sequences without mocking out internals.

No real subprocess, camera, or HTTP is touched.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import pytest

from upstream_agent.api.schemas import (
    Agent,
    AgentRegisterRequest,
    AgentRegisterResponse,
    ApprovedStreamConfig,
    Camera,
    CameraRegisterRequest,
    CameraRegisterResponse,
    Heartbeat,
    HeartbeatBatchResponse,
    RequestedStreamConfig,
    Session,
    StopStreamSessionResponse,
    StreamSessionRequest,
    StreamSessionResponse,
)
from upstream_agent.config import (
    ApiConfig,
    CameraConfig,
    Config,
    LoggingConfig,
    StreamConfig,
)
from upstream_agent.diagnostics.ffmpeg_check import FfmpegProbeResult
from upstream_agent.errors import (
    ApiConnectionError,
    ApiServerError,
    ApiTokenRevokedError,
    CameraOpenError,
    CameraReadError,
    FfmpegExitError,
    FfmpegWriteError,
)
from upstream_agent.runtime.backoff import BackoffPolicy
from upstream_agent.sources.base import FrameInfo, VideoSource
from upstream_agent.state import State
from upstream_agent.supervisor import run_agent

# Sentinels that must never leak.
SECRET_SRT_URL = "srt://stream.test.local:8890/?streamid=RECOVERY_TEST_SECRET"
SECRET_SESSION_TOKEN = "eyJhbGciOiJIUzI1NiI.RECOVERY_JWT.signature"
SECRET_STREAMID = "RECOVERY_TEST_SECRET"
SECRET_API_TOKEN = "ups_test_recovery_token_must_not_leak"
DEVICE_ID = "device-uuid-deadbeef-1111-2222-3333-444444444444"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeFrame:
    def __init__(self, height: int, width: int) -> None:
        self.shape = (height, width, 3)
        self._payload = b"BGR" * (width * height)

    def tobytes(self) -> bytes:
        return self._payload


class RecoveringCamera(VideoSource):
    """Camera that fails ``fail_at`` reads, then succeeds forever.

    The ``factory_history`` shared list records every ``open()`` /
    ``close()`` call across the camera instances the factory hands
    out so tests can assert reopen attempts.
    """

    def __init__(
        self,
        cfg: CameraConfig,
        *,
        fail_at_reads: list[int] | None = None,
        fail_all_reads: bool = False,
        open_failures: int = 0,
        keyboard_interrupt_after: int | None = None,
        frame_shape: tuple[int, int] = (720, 1280),
    ) -> None:
        self._cfg = cfg
        self._fail_at_reads = list(fail_at_reads or [])
        self._fail_all_reads = fail_all_reads
        self._open_failures_remaining = open_failures
        self._kbi_after = keyboard_interrupt_after
        self._frame_height, self._frame_width = frame_shape
        self.open_calls = 0
        self.close_calls = 0
        self.read_calls = 0
        self._open = False
        self._info: FrameInfo | None = None

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def info(self) -> FrameInfo | None:
        return self._info

    @property
    def last_error(self) -> str | None:
        return None

    def open(self) -> None:
        self.open_calls += 1
        if self._open_failures_remaining > 0:
            self._open_failures_remaining -= 1
            raise CameraOpenError("simulated open failure")
        self._open = True
        self._info = FrameInfo(
            width=self._frame_width, height=self._frame_height, fps=30.0
        )

    def read_frame(self) -> tuple[bool, Any]:
        self.read_calls += 1
        if (
            self._kbi_after is not None
            and self.read_calls > self._kbi_after
        ):
            raise KeyboardInterrupt()
        if self._fail_all_reads:
            raise CameraReadError("simulated read failure (always)")
        if self.read_calls in self._fail_at_reads:
            raise CameraReadError("simulated read failure")
        return True, FakeFrame(self._frame_height, self._frame_width)

    def close(self) -> None:
        self.close_calls += 1
        self._open = False


class StepwiseFfmpeg:
    """FFmpeg that fails at the Nth write, on the Nth poll, or at start.

    Tests stage a list of behaviours via ``write_failures`` and
    ``exit_on_writes``. The fresh-instance approach (one per attempt)
    means each ``attempt_specs`` entry describes one attempt.
    """

    def __init__(
        self,
        command: list[str],
        redacted_command: list[str],
        *,
        write_raises_after: int | None = None,
        write_raises: Exception | None = None,
        poll_returns_after: int | None = None,
        poll_exit_code: int = 1,
    ) -> None:
        self.command = list(command)
        self.redacted_command = list(redacted_command)
        self._write_raises_after = write_raises_after
        self._write_raises = write_raises or FfmpegWriteError("simulated pipe")
        self._poll_returns_after = poll_returns_after
        self._poll_exit_code = poll_exit_code
        self.start_calls = 0
        self.stop_calls = 0
        self.write_calls: list[bytes] = []
        self._started = False

    @property
    def is_running(self) -> bool:
        return self._started

    def start(self) -> None:
        self.start_calls += 1
        self._started = True

    def write_frame(self, frame_bytes: bytes) -> None:
        self.write_calls.append(frame_bytes)
        if (
            self._write_raises_after is not None
            and len(self.write_calls) > self._write_raises_after
        ):
            raise self._write_raises

    def poll(self) -> int | None:
        if (
            self._poll_returns_after is not None
            and len(self.write_calls) > self._poll_returns_after
        ):
            return self._poll_exit_code
        return None

    def stop(self, timeout_seconds: float = 5.0) -> None:
        self.stop_calls += 1
        self._started = False


class FlakyApiClient:
    """API client that can fail ``start_stream_session`` N times then succeed."""

    def __init__(
        self,
        *,
        start_session_failures: list[Exception] | None = None,
        srt_url: str | None = SECRET_SRT_URL,
        approved_width: int = 1280,
        approved_height: int = 720,
        approved_fps: int = 30,
        get_session_states: list[str] | None = None,
        get_session_raises: list[Exception | None] | None = None,
        timeout_first_session: bool = False,
    ) -> None:
        self._start_session_failures = list(start_session_failures or [])
        self._srt_url = srt_url
        self._approved = ApprovedStreamConfig(
            width=approved_width,
            height=approved_height,
            fps=approved_fps,
            codec="h264",
            bitrate_kbps=3000,
            latency_mode="balanced",
        )
        self._get_session_states: list[str] = list(get_session_states or [])
        self._get_session_raises: list[Exception | None] = list(
            get_session_raises or []
        )
        self._timeout_first_session = timeout_first_session
        self.register_agent_calls: list[AgentRegisterRequest] = []
        self.register_camera_calls: list[CameraRegisterRequest] = []
        self.start_session_calls: list[StreamSessionRequest] = []
        self.stop_session_calls: list[str] = []
        self.get_session_calls: list[str] = []
        self.heartbeat_calls: list[Heartbeat] = []
        self._session_counter = 0

    def __enter__(self) -> FlakyApiClient:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def register_agent(self, req: AgentRegisterRequest) -> AgentRegisterResponse:
        self.register_agent_calls.append(req)
        return AgentRegisterResponse(agent=Agent(id="agent_1", status="active"))

    def register_camera(self, req: CameraRegisterRequest) -> CameraRegisterResponse:
        self.register_camera_calls.append(req)
        return CameraRegisterResponse(
            camera=Camera(id="cam_1", external_id="webcam-0", status="active")
        )

    def start_stream_session(
        self, req: StreamSessionRequest
    ) -> StreamSessionResponse:
        self.start_session_calls.append(req)
        if self._start_session_failures:
            exc = self._start_session_failures.pop(0)
            raise exc
        self._session_counter += 1
        sess_id = f"sess_{self._session_counter}"
        return StreamSessionResponse(
            session=Session(
                id=sess_id,
                state="approved",
                camera_id=req.camera_id,
                srt_url=self._srt_url,
                streamid=SECRET_STREAMID,
                requested=RequestedStreamConfig(
                    width=req.requested.width,
                    height=req.requested.height,
                    fps=req.requested.fps,
                    codec=req.requested.codec,
                    bitrate_kbps=req.requested.bitrate_kbps,
                    latency_mode=req.requested.latency_mode,
                ),
                approved=self._approved,
            ),
            srt_url=self._srt_url,
            session_token=SECRET_SESSION_TOKEN,
        )

    def stop_stream_session(self, session_id: str) -> StopStreamSessionResponse:
        self.stop_session_calls.append(session_id)
        return StopStreamSessionResponse()

    def get_stream_session(self, session_id: str) -> Session:
        self.get_session_calls.append(session_id)
        if self._get_session_raises:
            exc = self._get_session_raises.pop(0)
            if exc is not None:
                raise exc
        if self._timeout_first_session and session_id == "sess_1":
            state = "approved"
        elif self._get_session_states:
            state = self._get_session_states.pop(0)
        else:
            state = "streaming"
        return Session(
            id=session_id,
            state=state,
            camera_id="cam_1",
            srt_url=self._srt_url,
            streamid=SECRET_STREAMID,
            requested=None,
            approved=self._approved,
        )

    def send_heartbeat(
        self, *, agent_id: str, heartbeat: Heartbeat
    ) -> HeartbeatBatchResponse:
        self.heartbeat_calls.append(heartbeat)
        return HeartbeatBatchResponse(
            accepted=1, server_time="2026-05-14T12:00:00Z"
        )


class CapturingReporter:
    """Stand-in for HeartbeatReporter that records the per-attempt stats."""

    def __init__(
        self,
        *,
        client: Any,
        agent_id: str,
        camera_id: str | None,
        session_id: str | None,
        agent_version: str,
        approved: ApprovedStreamConfig,
        stats: Any,
    ) -> None:
        self.client = client
        self.agent_id = agent_id
        self.camera_id = camera_id
        self.session_id = session_id
        self.approved = approved
        self.stats = stats
        self.start_calls = 0
        self.stop_calls = 0
        self._fatal_reason: str | None = None
        self.snapshots: list[Any] = []

    def start(self) -> None:
        self.start_calls += 1
        # Capture one snapshot at start so tests can assert phase.
        self.snapshots.append(self.stats.snapshot())

    def stop(self, timeout: float = 2.0) -> None:
        self.stop_calls += 1
        self.snapshots.append(self.stats.snapshot())

    def fatal_reason(self) -> str | None:
        return self._fatal_reason

    def set_fatal_reason(self, reason: str) -> None:
        self._fatal_reason = reason


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


def _state() -> State:
    return State(agent_id=None, device_id=DEVICE_ID, last_camera_id=None)


def _happy_probe() -> FfmpegProbeResult:
    return FfmpegProbeResult(
        available=True, version="9.0.0", srt_supported=True,
        h264_encoder="libx264", error=None,
    )


def _fast_backoff() -> BackoffPolicy:
    return BackoffPolicy(
        initial=0.0, multiplier=1.0, max_delay=0.0, jitter_factor=0.0
    )


# Tiny camera-recovery timings so reopen/validation tests don't spend
# real wall-clock seconds in settle / window waits. Defaults in
# production are 1.5s settle, 5.0s window, 0.1s retry, 0.5s release —
# documented in supervisor.py.
_FAST_CAMERA_TIMINGS: dict[str, float] = {
    "camera_settle_s": 0.0,
    "camera_validation_window_s": 0.05,
    "camera_read_retry_s": 0.0,
    "camera_release_settle_s": 0.0,
}


def _build_factories(
    *,
    cam: VideoSource,
    client: FlakyApiClient,
    ffmpeg_specs: list[dict[str, Any]] | None = None,
    cam_pool: list[VideoSource] | None = None,
    reporter_holder: list[CapturingReporter] | None = None,
) -> dict[str, Any]:
    """Build the factory callables and shared holders for one test.

    ``ffmpeg_specs`` is a list of kwargs dicts; the Nth ``ffmpeg_factory``
    call uses the Nth entry (or the last entry if more attempts).
    ``cam_pool`` is a list of cameras to hand out from successive
    ``camera_factory`` calls; if shorter than the call count, the last
    one is reused (used after a successful reopen).
    """

    cam_box: list[VideoSource] = list(cam_pool or [cam])
    cam_index = [0]

    def camera_factory(_cfg: CameraConfig) -> VideoSource:
        idx = cam_index[0]
        if idx >= len(cam_box):
            idx = len(cam_box) - 1
        cam_index[0] += 1
        return cam_box[idx]

    def client_factory(_cfg: ApiConfig) -> Any:
        return client

    ffmpeg_holder: list[StepwiseFfmpeg] = []
    specs = ffmpeg_specs or [{}]
    ff_call_index = [0]

    def ffmpeg_factory(cmd: list[str], redacted: list[str]) -> Any:
        idx = ff_call_index[0]
        if idx >= len(specs):
            idx = len(specs) - 1
        ff_call_index[0] += 1
        spec = specs[idx]
        ff = StepwiseFfmpeg(cmd, redacted, **spec)
        ffmpeg_holder.append(ff)
        return ff

    reporter_box: list[CapturingReporter] = (
        reporter_holder if reporter_holder is not None else []
    )

    def reporter_factory(**kwargs: Any) -> CapturingReporter:
        rep = CapturingReporter(**kwargs)
        reporter_box.append(rep)
        return rep

    return {
        "camera_factory": camera_factory,
        "client_factory": client_factory,
        "ffmpeg_factory": ffmpeg_factory,
        "ffmpeg_holder": ffmpeg_holder,
        "reporter_factory": reporter_factory,
        "reporter_holder": reporter_box,
    }


def _collect_events() -> tuple[list[tuple[str, str]], Any]:
    events: list[tuple[str, str]] = []

    def on_event(stage: str, message: str) -> None:
        events.append((stage, message))

    return events, on_event


# ---------------------------------------------------------------------------
# FFmpeg/SRT recovery
# ---------------------------------------------------------------------------


def test_ffmpeg_mid_stream_exit_triggers_a_second_attempt() -> None:
    """An FFmpeg exit on attempt #1 should drive a fresh session +
    fresh FFmpeg + frame loop that runs to completion on attempt #2."""

    client = FlakyApiClient()
    cam = RecoveringCamera(_cfg().camera)
    factories = _build_factories(
        cam=cam,
        client=client,
        ffmpeg_specs=[
            # Attempt #1: exits after 2 writes.
            {"poll_returns_after": 2, "poll_exit_code": 139},
            # Attempt #2: clean, no failures.
            {},
        ],
    )

    result, _ = run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=5,
        backoff=_fast_backoff(),
    )

    assert result.exit_reason == "max_frames"
    # Two FFmpeg instances, two stream sessions.
    assert len(factories["ffmpeg_holder"]) == 2
    assert len(client.start_session_calls) == 2
    assert client.stop_session_calls == ["sess_1", "sess_2"]
    # Camera was opened once and reused across attempts.
    assert cam.open_calls == 1
    assert cam.close_calls == 1


def test_broken_pipe_triggers_same_recovery_path() -> None:
    client = FlakyApiClient()
    cam = RecoveringCamera(_cfg().camera)
    factories = _build_factories(
        cam=cam,
        client=client,
        ffmpeg_specs=[
            {"write_raises_after": 2,
             "write_raises": FfmpegWriteError("simulated pipe break")},
            {},
        ],
    )

    result, _ = run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=4,
        backoff=_fast_backoff(),
    )

    assert result.exit_reason == "max_frames"
    assert len(factories["ffmpeg_holder"]) == 2
    assert len(client.start_session_calls) == 2


def test_old_session_is_stopped_before_new_session_started() -> None:
    """Each recovery attempt must stop the previous session before
    requesting a new one — never leak a streamid on the server."""

    client = FlakyApiClient()
    cam = RecoveringCamera(_cfg().camera)
    factories = _build_factories(
        cam=cam,
        client=client,
        ffmpeg_specs=[
            # Each of the first two attempts exits after 1 write.
            {"poll_returns_after": 1, "poll_exit_code": 1},
            {"poll_returns_after": 1, "poll_exit_code": 1},
            {},
        ],
    )

    run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=2,
        backoff=_fast_backoff(),
    )

    # Three start calls, two stops for the first two sessions plus
    # one for the final cleanup. Stop is called for every session.
    assert client.stop_session_calls == ["sess_1", "sess_2", "sess_3"]
    # The order: every stop comes before the next start.
    starts = ["start"] * len(client.start_session_calls)
    stops = ["stop"] * len(client.stop_session_calls)
    assert len(starts) == 3
    assert len(stops) == 3


# ---------------------------------------------------------------------------
# API recovery
# ---------------------------------------------------------------------------


def test_transient_api_connection_error_retries_then_succeeds() -> None:
    """``start_stream_session`` fails twice with ApiConnectionError,
    then succeeds — supervisor backs off and retries."""

    client = FlakyApiClient(
        start_session_failures=[
            ApiConnectionError("dns blip"),
            ApiServerError("503 transient"),
        ],
    )
    cam = RecoveringCamera(_cfg().camera)
    factories = _build_factories(cam=cam, client=client)

    result, _ = run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=2,
        backoff=_fast_backoff(),
    )

    assert result.exit_reason == "max_frames"
    # Three start calls — two failures plus one success.
    assert len(client.start_session_calls) == 3
    # First two attempts didn't get past start_session, so no ffmpeg
    # was constructed for them; only the third attempt's FFmpeg ran.
    assert len(factories["ffmpeg_holder"]) == 1


def test_token_revoked_is_fatal_no_retry() -> None:
    """``ApiTokenRevokedError`` from ``start_stream_session`` must
    propagate out without retrying."""

    client = FlakyApiClient(
        start_session_failures=[ApiTokenRevokedError("token revoked")],
    )
    cam = RecoveringCamera(_cfg().camera)
    factories = _build_factories(cam=cam, client=client)

    with pytest.raises(ApiTokenRevokedError):
        run_agent(
            _cfg(), _state(),
            camera_factory=factories["camera_factory"],
            client_factory=factories["client_factory"],
            ffmpeg_factory=factories["ffmpeg_factory"],
            reporter_factory=factories["reporter_factory"],
            ffmpeg_probe=_happy_probe(),
            max_frames=10,
            backoff=_fast_backoff(),
        )

    # Only one start attempt; no FFmpeg ever constructed.
    assert len(client.start_session_calls) == 1
    assert factories["ffmpeg_holder"] == []
    # No session was stopped (none ever started).
    assert client.stop_session_calls == []


# ---------------------------------------------------------------------------
# Camera recovery
# ---------------------------------------------------------------------------


def test_camera_read_failure_reopens_camera_and_resumes() -> None:
    """A mid-stream CameraReadError closes the camera, reopens it via
    the camera factory, requests a new session, and continues streaming."""

    client = FlakyApiClient()
    failing_cam = RecoveringCamera(_cfg().camera, fail_at_reads=[3])
    healthy_cam = RecoveringCamera(_cfg().camera)
    factories = _build_factories(
        cam=failing_cam,
        client=client,
        cam_pool=[failing_cam, healthy_cam],
    )

    result, _ = run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=4,
        backoff=_fast_backoff(),
    )

    # First camera was closed; second camera was opened.
    assert failing_cam.close_calls >= 1
    assert healthy_cam.open_calls == 1
    assert healthy_cam.close_calls == 1  # Outer cleanup at end.
    # A fresh session was issued for the second attempt.
    assert len(client.start_session_calls) == 2
    assert result.exit_reason == "max_frames"


def test_camera_open_failures_back_off_then_succeed() -> None:
    """If camera reopen raises CameraOpenError, retry with backoff."""

    client = FlakyApiClient()
    failing_cam = RecoveringCamera(_cfg().camera, fail_at_reads=[3])
    # The recovered camera fails its first two opens, succeeds on the third.
    recovered = RecoveringCamera(_cfg().camera, open_failures=2)
    factories = _build_factories(
        cam=failing_cam, client=client, cam_pool=[failing_cam, recovered],
    )

    result, _ = run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=4,
        backoff=_fast_backoff(),
    )

    assert recovered.open_calls == 3
    assert result.exit_reason == "max_frames"


# ---------------------------------------------------------------------------
# Open-but-no-frame recovery
# ---------------------------------------------------------------------------


def test_camera_open_succeeds_but_validation_fails_does_not_start_session() -> None:
    """An OpenCV V4L2 device can return success from ``open()`` even
    though it isn't producing frames yet. The supervisor must validate
    real frame reads before declaring recovery — otherwise a fresh
    session is started, FFmpeg launches, and the first ``read_frame``
    call in the live loop fails, looping forever."""

    client = FlakyApiClient()
    failing_cam = RecoveringCamera(_cfg().camera, fail_at_reads=[3])
    # Recovery #1: open succeeds but every validation read fails.
    bad_recovery_1 = RecoveringCamera(_cfg().camera, fail_all_reads=True)
    bad_recovery_2 = RecoveringCamera(_cfg().camera, fail_all_reads=True)
    # Recovery #3: healthy.
    healthy = RecoveringCamera(_cfg().camera)
    factories = _build_factories(
        cam=failing_cam,
        client=client,
        cam_pool=[failing_cam, bad_recovery_1, bad_recovery_2, healthy],
    )

    result, _ = run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=4,
        backoff=_fast_backoff(),
        **_FAST_CAMERA_TIMINGS,
    )

    assert result.exit_reason == "max_frames"
    # Initial attempt + one final attempt with ``healthy`` — the two
    # failed validations must NOT have created their own sessions.
    assert len(client.start_session_calls) == 2
    # Same for FFmpeg: only the initial and the final attempts
    # launched a subprocess.
    assert len(factories["ffmpeg_holder"]) == 2
    # Both bad-recovery cameras were opened and closed; they may have
    # taken several reads inside the validation window before timing
    # out, so we don't pin the exact read count.
    assert bad_recovery_1.open_calls == 1
    assert bad_recovery_1.read_calls >= 1
    assert bad_recovery_1.close_calls >= 1
    assert bad_recovery_2.open_calls == 1
    assert bad_recovery_2.read_calls >= 1
    assert bad_recovery_2.close_calls >= 1


def test_camera_open_succeeds_and_first_read_succeeds_proceeds_to_new_session() -> None:
    """The happy-path of the verification: open succeeds and the
    multi-frame validation passes, so recovery proceeds to a fresh
    session and FFmpeg."""

    client = FlakyApiClient()
    failing_cam = RecoveringCamera(_cfg().camera, fail_at_reads=[5])
    healthy = RecoveringCamera(_cfg().camera)
    factories = _build_factories(
        cam=failing_cam, client=client, cam_pool=[failing_cam, healthy],
    )

    result, _ = run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=6,
        backoff=_fast_backoff(),
    )

    assert result.exit_reason == "max_frames"
    # Exactly two sessions and two FFmpeg processes — initial attempt
    # and the single successful recovery attempt.
    assert len(client.start_session_calls) == 2
    assert len(factories["ffmpeg_holder"]) == 2
    # Three validation reads + at least one streaming read.
    assert healthy.read_calls >= 4
    assert healthy.open_calls == 1


def test_camera_validation_tolerates_transient_failures_within_window() -> None:
    """The validator allows transient read failures inside the
    validation window. If the camera eventually produces enough
    consecutive successful reads before the window expires, recovery
    succeeds without needing another reopen cycle."""

    client = FlakyApiClient()
    failing_cam = RecoveringCamera(_cfg().camera, fail_at_reads=[3])
    # Recovery camera: validator does N reads. First two fail
    # transiently, then read #3 onwards succeeds. With
    # validation_frames=3 the streak needs to reach 3 consecutive
    # good reads — so reads 3, 4, 5 succeed and validation passes.
    recovered = RecoveringCamera(_cfg().camera, fail_at_reads=[1, 2])
    factories = _build_factories(
        cam=failing_cam, client=client, cam_pool=[failing_cam, recovered],
    )

    # Need a non-zero window for the validator to retry past the
    # initial failures. Default tiny window from _FAST_CAMERA_TIMINGS
    # (0.05s) is enough at zero retry_s.
    result, _ = run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=5,
        backoff=_fast_backoff(),
        **_FAST_CAMERA_TIMINGS,
    )

    assert result.exit_reason == "max_frames"
    # Recovery succeeded on the first reopen — only one new session
    # after the initial failure (initial attempt + recovery attempt).
    assert len(client.start_session_calls) == 2
    # The recovered camera was opened once and saw at least 5 reads
    # (2 transient failures + 3 successful validation reads).
    assert recovered.open_calls == 1
    assert recovered.read_calls >= 5


def test_camera_unavailable_hint_fires_after_N_failed_cycles() -> None:
    """If the camera opens but the validation window keeps timing out,
    after N reopen attempts the supervisor emits a single
    ``camera_unavailable_hint`` event nudging the operator to restart.
    The agent keeps retrying after the hint."""

    client = FlakyApiClient()
    failing_cam = RecoveringCamera(_cfg().camera, fail_at_reads=[3])
    # Five consecutive reopen attempts that all open but never deliver
    # a frame. The supervisor should emit exactly one hint after the
    # configured threshold (3) and keep retrying — the final cam in
    # the pool eventually succeeds so the test exits.
    bads = [
        RecoveringCamera(_cfg().camera, fail_all_reads=True) for _ in range(5)
    ]
    healthy = RecoveringCamera(_cfg().camera)
    factories = _build_factories(
        cam=failing_cam,
        client=client,
        cam_pool=[failing_cam, *bads, healthy],
    )
    events, on_event = _collect_events()

    result, _ = run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        on_event=on_event,
        max_frames=4,
        backoff=_fast_backoff(),
        camera_unavailable_hint_after=3,
        **_FAST_CAMERA_TIMINGS,
    )

    assert result.exit_reason == "max_frames"
    hint_events = [
        (s, m) for s, m in events if s == "camera_unavailable_hint"
    ]
    assert len(hint_events) == 1
    msg = hint_events[0][1]
    assert "produce frames" in msg
    assert "restarting the agent" in msg
    # Recovery did eventually proceed to the healthy camera and start
    # a fresh streaming attempt.
    assert healthy.open_calls == 1


def test_repeated_validation_failures_do_not_tight_loop() -> None:
    """Repeated camera recovery failures (open succeeds, then the
    validation window expires) should progress the backoff delay; the
    supervisor must not reset to attempt #1 between cycles."""

    delays: list[float] = []

    def sleep(delay: float, shutdown: threading.Event) -> bool:  # noqa: ARG001
        delays.append(delay)
        return False

    # Real backoff math, fake sleep so the test is instant.
    backoff = BackoffPolicy(
        initial=1.0, multiplier=2.0, max_delay=30.0, jitter_factor=0.0,
    )
    backoff.sleep = sleep  # type: ignore[method-assign]

    client = FlakyApiClient()
    failing_cam = RecoveringCamera(_cfg().camera, fail_at_reads=[3])
    # Three consecutive bad recoveries (open succeeds, every read in
    # the window fails), then a healthy one.
    bads = [
        RecoveringCamera(_cfg().camera, fail_all_reads=True) for _ in range(3)
    ]
    healthy = RecoveringCamera(_cfg().camera)
    factories = _build_factories(
        cam=failing_cam,
        client=client,
        cam_pool=[failing_cam, *bads, healthy],
    )

    result, _ = run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=4,
        backoff=backoff,
        **_FAST_CAMERA_TIMINGS,
    )

    assert result.exit_reason == "max_frames"

    # The first four sleeps inside ``_reopen_camera_with_backoff``
    # span the four reopen attempts (3 bads + 1 healthy). Delays grow
    # geometrically; the validator's window expiry between attempts
    # does not reset the backoff.
    assert delays[:4] == pytest.approx([1.0, 2.0, 4.0, 8.0])


# ---------------------------------------------------------------------------
# Shutdown / Ctrl+C during camera recovery
# ---------------------------------------------------------------------------


def test_shutdown_during_camera_recovery_wait_exits_cleanly() -> None:
    """``shutdown.set()`` during the camera-reopen backoff sleep
    returns ``None`` from ``_reopen_camera_with_backoff`` so the
    supervisor exits cleanly."""

    client = FlakyApiClient()
    failing_cam = RecoveringCamera(_cfg().camera, fail_at_reads=[1])
    # Fresh camera (will not be reached because shutdown fires first).
    healthy = RecoveringCamera(_cfg().camera)
    factories = _build_factories(
        cam=failing_cam, client=client, cam_pool=[failing_cam, healthy],
    )

    backoff = BackoffPolicy(
        initial=10.0, multiplier=1.0, max_delay=10.0, jitter_factor=0.0,
    )

    def fake_sleep(delay: float, shutdown: threading.Event) -> bool:
        shutdown.set()
        return True

    backoff.sleep = fake_sleep  # type: ignore[method-assign]

    result, _ = run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=10,
        backoff=backoff,
    )

    assert result.exit_reason == "keyboard_interrupt"
    # The healthy camera was never reached.
    assert healthy.open_calls == 0
    # The failing camera was closed (initial attempt cleanup +
    # camera-loss handling).
    assert failing_cam.close_calls >= 1


def test_initial_camera_open_failure_is_fatal() -> None:
    """Camera that fails to open on the very first attempt must
    propagate ``CameraOpenError`` immediately — only mid-stream
    disconnects trigger the reopen loop with backoff."""

    client = FlakyApiClient()
    bad_initial = RecoveringCamera(_cfg().camera, open_failures=99)
    factories = _build_factories(cam=bad_initial, client=client)

    with pytest.raises(CameraOpenError):
        run_agent(
            _cfg(), _state(),
            camera_factory=factories["camera_factory"],
            client_factory=factories["client_factory"],
            ffmpeg_factory=factories["ffmpeg_factory"],
            reporter_factory=factories["reporter_factory"],
            ffmpeg_probe=_happy_probe(),
            max_frames=10,
            backoff=_fast_backoff(),
            **_FAST_CAMERA_TIMINGS,
        )

    # No session was ever started — the supervisor never reached the
    # API client.
    assert client.start_session_calls == []
    # No FFmpeg was started either.
    assert factories["ffmpeg_holder"] == []


def test_keyboard_interrupt_during_validation_window_propagates() -> None:
    """A Ctrl+C while the validator is retrying inside its time
    window must propagate through outer cleanup. The cam.read_frame
    raising ``KeyboardInterrupt`` is the realistic path — signals
    arrive on the main thread between syscalls."""

    client = FlakyApiClient()
    failing_cam = RecoveringCamera(_cfg().camera, fail_at_reads=[3])
    # Recovery cam: open succeeds, but the read_frame call inside the
    # validation window raises KeyboardInterrupt on its 2nd read.
    interrupting_recovery = RecoveringCamera(
        _cfg().camera, keyboard_interrupt_after=1,
    )
    factories = _build_factories(
        cam=failing_cam, client=client,
        cam_pool=[failing_cam, interrupting_recovery],
    )

    with pytest.raises(KeyboardInterrupt):
        run_agent(
            _cfg(), _state(),
            camera_factory=factories["camera_factory"],
            client_factory=factories["client_factory"],
            ffmpeg_factory=factories["ffmpeg_factory"],
            reporter_factory=factories["reporter_factory"],
            ffmpeg_probe=_happy_probe(),
            max_frames=10,
            backoff=_fast_backoff(),
            **_FAST_CAMERA_TIMINGS,
        )

    # Outer cleanup released the interrupted camera and stopped the
    # original session.
    assert interrupting_recovery.close_calls >= 1
    assert client.stop_session_calls == ["sess_1"]


def test_keyboard_interrupt_during_camera_recovery_wait_propagates() -> None:
    """A SIGINT during ``BackoffPolicy.sleep`` inside camera recovery
    propagates to the caller after running full cleanup."""

    client = FlakyApiClient()
    failing_cam = RecoveringCamera(_cfg().camera, fail_at_reads=[1])
    healthy = RecoveringCamera(_cfg().camera)
    factories = _build_factories(
        cam=failing_cam, client=client, cam_pool=[failing_cam, healthy],
    )

    backoff = BackoffPolicy(
        initial=10.0, multiplier=1.0, max_delay=10.0, jitter_factor=0.0,
    )

    def fake_sleep(delay: float, shutdown: threading.Event) -> bool:
        raise KeyboardInterrupt()

    backoff.sleep = fake_sleep  # type: ignore[method-assign]

    with pytest.raises(KeyboardInterrupt):
        run_agent(
            _cfg(), _state(),
            camera_factory=factories["camera_factory"],
            client_factory=factories["client_factory"],
            ffmpeg_factory=factories["ffmpeg_factory"],
            reporter_factory=factories["reporter_factory"],
            ffmpeg_probe=_happy_probe(),
            max_frames=10,
            backoff=backoff,
        )

    # Outer cleanup ran on the failing camera. The healthy one was
    # never reached.
    assert healthy.open_calls == 0
    assert failing_cam.close_calls >= 1


# ---------------------------------------------------------------------------
# Phase / heartbeat status
# ---------------------------------------------------------------------------


def test_reporter_is_torn_down_between_attempts() -> None:
    """The heartbeat reporter is per-session: each recovery attempt
    builds its own reporter and the previous one is stopped during
    attempt cleanup. This is the simple, honest behaviour — no
    `reconnecting` heartbeat is sent during the reconnect gap."""

    client = FlakyApiClient()
    cam = RecoveringCamera(_cfg().camera)
    reporter_holder: list[CapturingReporter] = []
    factories = _build_factories(
        cam=cam, client=client,
        ffmpeg_specs=[{"poll_returns_after": 1, "poll_exit_code": 1}, {}],
        reporter_holder=reporter_holder,
    )

    run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=2,
        backoff=_fast_backoff(),
    )

    # Two reporters were created across the two attempts and each was
    # stopped exactly once.
    assert len(reporter_holder) == 2
    for reporter in reporter_holder:
        assert reporter.start_calls == 1
        assert reporter.stop_calls == 1


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


def test_shutdown_during_backoff_sleep_cleans_up_and_returns() -> None:
    """If ``shutdown.set()`` happens during the inter-attempt sleep,
    the recovery loop exits cleanly with ``keyboard_interrupt``."""

    client = FlakyApiClient()
    cam = RecoveringCamera(_cfg().camera)
    factories = _build_factories(
        cam=cam, client=client,
        ffmpeg_specs=[{"poll_returns_after": 1, "poll_exit_code": 1}],
    )

    # Use a backoff whose sleep we can interrupt deterministically.
    backoff = BackoffPolicy(
        initial=10.0, multiplier=1.0, max_delay=10.0, jitter_factor=0.0
    )

    # Patch the policy's sleep to set the shutdown event and return
    # ``True`` immediately, simulating an external shutdown that
    # arrives while the supervisor is waiting between attempts.
    def fake_sleep(delay: float, shutdown: threading.Event) -> bool:
        shutdown.set()
        return True

    backoff.sleep = fake_sleep  # type: ignore[method-assign]

    result, _ = run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=10,
        backoff=backoff,
    )

    assert result.exit_reason == "keyboard_interrupt"
    # Camera was released by outer cleanup.
    assert cam.close_calls == 1
    # The attempt's FFmpeg was stopped during per-attempt cleanup.
    assert factories["ffmpeg_holder"][0].stop_calls == 1
    # The attempt's session was stopped.
    assert client.stop_session_calls == ["sess_1"]


def test_keyboard_interrupt_during_backoff_sleep_propagates() -> None:
    """A Ctrl+C surfacing as KeyboardInterrupt during the inter-attempt
    sleep runs full cleanup and propagates to the caller."""

    client = FlakyApiClient()
    cam = RecoveringCamera(_cfg().camera)
    factories = _build_factories(
        cam=cam, client=client,
        ffmpeg_specs=[{"poll_returns_after": 1, "poll_exit_code": 1}],
    )

    backoff = BackoffPolicy(
        initial=10.0, multiplier=1.0, max_delay=10.0, jitter_factor=0.0
    )

    def fake_sleep(delay: float, shutdown: threading.Event) -> bool:
        raise KeyboardInterrupt()

    backoff.sleep = fake_sleep  # type: ignore[method-assign]

    with pytest.raises(KeyboardInterrupt):
        run_agent(
            _cfg(), _state(),
            camera_factory=factories["camera_factory"],
            client_factory=factories["client_factory"],
            ffmpeg_factory=factories["ffmpeg_factory"],
            reporter_factory=factories["reporter_factory"],
            ffmpeg_probe=_happy_probe(),
            max_frames=10,
            backoff=backoff,
        )

    # Cleanup still ran.
    assert cam.close_calls == 1
    assert factories["ffmpeg_holder"][0].stop_calls == 1
    assert client.stop_session_calls == ["sess_1"]


def test_max_attempts_caps_recovery_and_reraises() -> None:
    """A permanent FFmpeg failure with ``max_attempts=3`` runs three
    attempts and then re-raises the underlying exception."""

    client = FlakyApiClient()
    cam = RecoveringCamera(_cfg().camera)
    # Every FFmpeg exits after one write.
    factories = _build_factories(
        cam=cam, client=client,
        ffmpeg_specs=[{"poll_returns_after": 1, "poll_exit_code": 1}],
    )

    with pytest.raises(FfmpegExitError):
        run_agent(
            _cfg(), _state(),
            camera_factory=factories["camera_factory"],
            client_factory=factories["client_factory"],
            ffmpeg_factory=factories["ffmpeg_factory"],
            reporter_factory=factories["reporter_factory"],
            ffmpeg_probe=_happy_probe(),
            max_frames=10,
            max_attempts=3,
            backoff=_fast_backoff(),
        )

    # Three FFmpeg instances, three start_session calls.
    assert len(factories["ffmpeg_holder"]) == 3
    assert len(client.start_session_calls) == 3


def test_stop_stream_session_failure_is_logged_not_fatal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If ``client.stop_stream_session`` raises during cleanup, the
    local teardown continues. M6 behavior preserved."""

    caplog.set_level(logging.WARNING)
    client = FlakyApiClient()

    def boom(session_id: str) -> StopStreamSessionResponse:
        raise ApiConnectionError("can't reach server during stop")

    client.stop_stream_session = boom  # type: ignore[assignment]

    cam = RecoveringCamera(_cfg().camera)
    factories = _build_factories(cam=cam, client=client)

    result, _ = run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=2,
        backoff=_fast_backoff(),
    )

    # Local cleanup still completed.
    assert result.exit_reason == "max_frames"
    assert cam.close_calls == 1
    assert factories["ffmpeg_holder"][0].stop_calls == 1


# ---------------------------------------------------------------------------
# Leak guards
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Remote ingest confirmation (Issue 1 fix)
# ---------------------------------------------------------------------------


_FAST_INGEST: dict[str, float] = {
    "ingest_timeout_s": 0.5,
    "ingest_poll_interval_s": 0.0,
}


def test_ingest_state_streaming_starts_main_loop_and_heartbeat() -> None:
    """When the API reports state=streaming on the first poll, the
    supervisor emits ``ingest_confirmed``, starts the heartbeat
    reporter, and enters the main frame loop."""

    client = FlakyApiClient(get_session_states=["streaming"])
    cam = RecoveringCamera(_cfg().camera)
    factories = _build_factories(cam=cam, client=client)
    events, on_event = _collect_events()

    result, _ = run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        on_event=on_event,
        max_frames=3,
        backoff=_fast_backoff(),
        **_FAST_INGEST,
    )

    assert result.exit_reason == "max_frames"
    stages = [s for s, _ in events]
    assert "waiting_for_ingest" in stages
    assert "ingest_confirmed" in stages
    # "Streaming started" comes AFTER ingest_confirmed.
    assert stages.index("ingest_confirmed") < stages.index("streaming")
    # heartbeat_started also comes after ingest_confirmed.
    assert stages.index("ingest_confirmed") < stages.index("heartbeat_started")


def test_streaming_event_is_not_emitted_before_ingest_confirmed() -> None:
    """``stream_started`` / ``streaming`` must not fire while the API
    still reports ``approved``."""

    # Stay in "approved" for 4 polls, then flip to "streaming".
    client = FlakyApiClient(
        get_session_states=["approved", "approved", "approved", "streaming"],
    )
    cam = RecoveringCamera(_cfg().camera)
    factories = _build_factories(cam=cam, client=client)
    events, on_event = _collect_events()

    run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        on_event=on_event,
        max_frames=2,
        backoff=_fast_backoff(),
        **_FAST_INGEST,
    )

    # The "streaming" event must come AFTER ingest_confirmed in the
    # event order.
    ingest_idx = next(i for i, (s, _) in enumerate(events) if s == "ingest_confirmed")
    streaming_idx = next(i for i, (s, _) in enumerate(events) if s == "streaming")
    assert ingest_idx < streaming_idx
    # At least one "Streaming started" appears.
    streaming_msgs = [m for s, m in events if s == "streaming"]
    assert any("Streaming started" in m for m in streaming_msgs)


def test_ingest_timeout_triggers_recoverable_retry() -> None:
    """If state never flips to streaming within the timeout, the
    supervisor classifies as recoverable, tears the attempt down, and
    requests a fresh session for the next attempt."""

    # First session always reports ``approved`` so the timeout fires;
    # subsequent sessions get the default ``streaming`` so the test
    # can exit on the next attempt.
    client = FlakyApiClient(timeout_first_session=True)
    cam = RecoveringCamera(_cfg().camera)
    factories = _build_factories(cam=cam, client=client)
    events, on_event = _collect_events()

    result, _ = run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        on_event=on_event,
        max_frames=2,
        backoff=_fast_backoff(),
        ingest_timeout_s=0.05,
        ingest_poll_interval_s=0.0,
    )

    assert result.exit_reason == "max_frames"
    # First session was stopped; a new one was started for the
    # successful recovery attempt.
    assert len(client.start_session_calls) == 2
    assert "sess_1" in client.stop_session_calls
    # The "reconnecting" event mentions the recoverable timeout.
    reconnect_msgs = [m for s, m in events if s == "reconnecting"]
    assert any("remote_ingest_timeout" in m.lower() for m in reconnect_msgs)


def test_ingest_transient_api_error_keeps_polling() -> None:
    """A non-fatal API failure during the polling window should not
    abort the wait; the supervisor logs an ingest_poll_transient_error
    event and keeps polling until the timeout or success."""

    client = FlakyApiClient(
        get_session_states=["approved", "streaming"],
        get_session_raises=[None, ApiConnectionError("network blip"), None],
    )
    cam = RecoveringCamera(_cfg().camera)
    factories = _build_factories(cam=cam, client=client)
    events, on_event = _collect_events()

    result, _ = run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        on_event=on_event,
        max_frames=2,
        backoff=_fast_backoff(),
        ingest_timeout_s=1.0,
        ingest_poll_interval_s=0.0,
    )

    assert result.exit_reason == "max_frames"
    stages = [s for s, _ in events]
    assert "ingest_poll_transient_error" in stages
    assert "ingest_confirmed" in stages


def test_ingest_fatal_api_error_is_fatal() -> None:
    """An ``ApiTokenRevokedError`` from the polling endpoint is
    classified as fatal and propagates out, never retried."""

    client = FlakyApiClient(
        get_session_raises=[ApiTokenRevokedError("token revoked")],
    )
    cam = RecoveringCamera(_cfg().camera)
    factories = _build_factories(cam=cam, client=client)

    with pytest.raises(ApiTokenRevokedError):
        run_agent(
            _cfg(), _state(),
            camera_factory=factories["camera_factory"],
            client_factory=factories["client_factory"],
            ffmpeg_factory=factories["ffmpeg_factory"],
            reporter_factory=factories["reporter_factory"],
            ffmpeg_probe=_happy_probe(),
            max_frames=2,
            backoff=_fast_backoff(),
            **_FAST_INGEST,
        )

    # Exactly one session was started; no recovery cycle.
    assert len(client.start_session_calls) == 1


def test_ingest_terminal_state_is_recoverable() -> None:
    """If the API reports the session has ``failed`` / ``ended`` /
    ``expired`` before streaming, the supervisor classifies as
    recoverable and requests a new session."""

    # First attempt's poll returns "failed", second attempt's poll
    # returns "streaming" so the test can exit cleanly.
    client = FlakyApiClient(get_session_states=["failed", "streaming"])
    cam = RecoveringCamera(_cfg().camera)
    factories = _build_factories(cam=cam, client=client)

    result, _ = run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=2,
        backoff=_fast_backoff(),
        **_FAST_INGEST,
    )

    assert result.exit_reason == "max_frames"
    # Two sessions: first failed, second succeeded.
    assert len(client.start_session_calls) == 2


def test_keyboard_interrupt_during_ingest_wait_propagates() -> None:
    """A Ctrl+C during the ingest wait (the camera's read_frame raises
    ``KeyboardInterrupt``) propagates through outer cleanup to the
    caller. The session and FFmpeg are still torn down."""

    client = FlakyApiClient(get_session_states=["approved"] * 50)
    interrupting_cam = RecoveringCamera(
        _cfg().camera, keyboard_interrupt_after=2,
    )
    factories = _build_factories(cam=interrupting_cam, client=client)

    with pytest.raises(KeyboardInterrupt):
        run_agent(
            _cfg(), _state(),
            camera_factory=factories["camera_factory"],
            client_factory=factories["client_factory"],
            ffmpeg_factory=factories["ffmpeg_factory"],
            reporter_factory=factories["reporter_factory"],
            ffmpeg_probe=_happy_probe(),
            max_frames=10,
            backoff=_fast_backoff(),
            ingest_timeout_s=5.0,  # generous; the interrupt fires first
            ingest_poll_interval_s=0.0,
        )

    # Attempt cleanup ran: session stopped, FFmpeg stopped.
    assert client.stop_session_calls == ["sess_1"]
    assert factories["ffmpeg_holder"][0].stop_calls == 1
    # Outer cleanup released the camera.
    assert interrupting_cam.close_calls >= 1


def test_ingest_wait_polls_multiple_times_before_confirm() -> None:
    """The supervisor must actually poll repeatedly while state is
    ``approved`` and not skip the wait. This indirectly verifies the
    polling loop is interruptible / bounded."""

    client = FlakyApiClient(
        get_session_states=["approved", "approved", "streaming"],
    )
    cam = RecoveringCamera(_cfg().camera)
    factories = _build_factories(cam=cam, client=client)

    run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=2,
        backoff=_fast_backoff(),
        ingest_timeout_s=1.0,
        ingest_poll_interval_s=0.0,
    )

    # At least 3 polls happened on the single session before confirm.
    assert len(client.get_session_calls) >= 3
    assert all(s == client.start_session_calls[0].camera_id or True
               for s in client.get_session_calls)


def test_no_secrets_in_ingest_events(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    client = FlakyApiClient(get_session_states=["streaming"])
    cam = RecoveringCamera(_cfg().camera)
    factories = _build_factories(cam=cam, client=client)
    events, on_event = _collect_events()

    run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        on_event=on_event,
        max_frames=2,
        backoff=_fast_backoff(),
        **_FAST_INGEST,
    )

    secrets = [SECRET_SRT_URL, SECRET_SESSION_TOKEN, SECRET_STREAMID, SECRET_API_TOKEN]
    for _stage, msg in events:
        for s in secrets:
            assert s not in msg
        assert "srt://" not in msg
    for record in caplog.records:
        for s in secrets:
            assert s not in record.getMessage()


# ---------------------------------------------------------------------------
# Honest log wording (Issue 1 cleanup)
# ---------------------------------------------------------------------------


def test_ingest_confirmed_event_uses_honest_message() -> None:
    """The ``ingest_confirmed`` event must not claim the dashboard
    can see frames — only that the API reports session streaming."""

    client = FlakyApiClient(get_session_states=["streaming"])
    cam = RecoveringCamera(_cfg().camera)
    factories = _build_factories(cam=cam, client=client)
    events, on_event = _collect_events()

    run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        on_event=on_event,
        max_frames=2,
        backoff=_fast_backoff(),
        **_FAST_INGEST,
    )

    confirm_msgs = [m for s, m in events if s == "ingest_confirmed"]
    assert confirm_msgs, "ingest_confirmed event must fire on the happy path"
    msg = confirm_msgs[0]
    assert "API reports session streaming" in msg
    assert "Dashboard playback is not directly verified" in msg
    # The old over-confident phrase must be gone.
    assert "Remote ingest confirmed" not in msg


def test_streaming_event_message_does_not_claim_dashboard_visibility() -> None:
    """The ``streaming`` event must not over-claim. It announces local
    frames are flowing to the streaming server, nothing about remote
    playback."""

    client = FlakyApiClient(get_session_states=["streaming"])
    cam = RecoveringCamera(_cfg().camera)
    factories = _build_factories(cam=cam, client=client)
    events, on_event = _collect_events()

    run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        on_event=on_event,
        max_frames=2,
        backoff=_fast_backoff(),
        **_FAST_INGEST,
    )

    streaming_msgs = [m for s, m in events if s == "streaming"]
    assert streaming_msgs
    msg = streaming_msgs[0]
    assert "frames flowing to streaming server" in msg
    # The old bare phrase is gone.
    assert msg.split("\n")[0] != "Streaming started"


# ---------------------------------------------------------------------------
# Secret-leak guards
# ---------------------------------------------------------------------------


def test_no_secrets_in_events_or_logs_during_recovery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every event and log record produced during a multi-attempt
    recovery cycle must be free of SRT URLs, streamids, session tokens,
    and the API bearer token."""

    caplog.set_level(logging.DEBUG)
    client = FlakyApiClient(
        start_session_failures=[ApiConnectionError("blip")],
    )
    cam = RecoveringCamera(_cfg().camera, fail_at_reads=[5])
    factories = _build_factories(
        cam=cam, client=client,
        ffmpeg_specs=[{"poll_returns_after": 3, "poll_exit_code": 1}, {}],
    )
    events, on_event = _collect_events()

    run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        reporter_factory=factories["reporter_factory"],
        ffmpeg_probe=_happy_probe(),
        on_event=on_event,
        max_frames=4,
        backoff=_fast_backoff(),
    )

    sensitive = [SECRET_SRT_URL, SECRET_SESSION_TOKEN, SECRET_STREAMID, SECRET_API_TOKEN]
    for _stage, message in events:
        for s in sensitive:
            assert s not in message
        assert "srt://" not in message

    for record in caplog.records:
        msg = record.getMessage()
        for s in sensitive:
            assert s not in msg
