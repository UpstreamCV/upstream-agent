"""Tests for :func:`upstream_agent.supervisor.run_agent`.

A ``FakeCamera``, ``FakeApiClient``, and ``FakeFfmpeg`` are injected
via factory kwargs. No real subprocess, camera, or HTTP is invoked.
"""

from __future__ import annotations

import logging
import sys
import types
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
    ApiAuthError,
    CameraReadError,
    FfmpegExitError,
    FfmpegStartError,
    FfmpegWriteError,
)
from upstream_agent.runtime.backoff import BackoffPolicy
from upstream_agent.sources.base import FrameInfo, VideoSource
from upstream_agent.supervisor import RunResult, run_agent


def _fast_backoff() -> BackoffPolicy:
    """Backoff with zero delays so reconnect tests don't wall-sleep."""

    return BackoffPolicy(
        initial=0.0, multiplier=1.0, max_delay=0.0, jitter_factor=0.0
    )

# Placeholder values — must not appear in any log/event/result.
SECRET_SRT_URL = "srt://stream.test.local:8890/?streamid=RUN_TEST_SECRET"
SECRET_SESSION_TOKEN = "eyJhbGciOiJIUzI1NiI.RUN_TEST_JWT.signature"
SECRET_STREAMID = "RUN_TEST_SECRET"
SECRET_API_TOKEN = "ups_test_real_token_must_not_leak"
DEVICE_ID = "device-uuid-abcdefgh-1111-2222-3333-444444444444"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeFrame:
    """Minimal stand-in for a numpy ndarray.

    Exposes ``shape`` and ``tobytes`` so the supervisor's frame path
    runs end-to-end without numpy.
    """

    def __init__(self, height: int, width: int, payload: bytes | None = None) -> None:
        self.shape = (height, width, 3)
        self._payload = payload if payload is not None else b"BGR" * (width * height)

    def tobytes(self) -> bytes:
        return self._payload


class FakeCamera(VideoSource):
    def __init__(
        self,
        cfg: CameraConfig,
        *,
        frame_shape: tuple[int, int] = (720, 1280),
        read_raises_after: int | None = None,
        read_raises: Exception | None = None,
    ) -> None:
        self._cfg = cfg
        self._frame_height, self._frame_width = frame_shape
        self._read_raises_after = read_raises_after
        self._read_raises = read_raises or CameraReadError("simulated read failure")
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
        self._open = True
        self._info = FrameInfo(
            width=self._frame_width, height=self._frame_height, fps=30.0
        )

    def read_frame(self) -> tuple[bool, Any]:
        self.read_calls += 1
        if (
            self._read_raises_after is not None
            and self.read_calls > self._read_raises_after
        ):
            raise self._read_raises
        return True, FakeFrame(self._frame_height, self._frame_width)

    def close(self) -> None:
        self.close_calls += 1
        self._open = False


class FakeFfmpeg:
    """Stand-in for FfmpegEncoderProcess (duck-typed)."""

    def __init__(
        self,
        command: list[str],
        redacted_command: list[str],
        *,
        start_raises: Exception | None = None,
        write_raises_after: int | None = None,
        write_raises: Exception | None = None,
        poll_returns_after: int | None = None,
        poll_exit_code: int = 1,
    ) -> None:
        self.command = list(command)
        self.redacted_command = list(redacted_command)
        self.start_calls = 0
        self.stop_calls = 0
        self.write_calls: list[bytes] = []
        self._start_raises = start_raises
        self._write_raises_after = write_raises_after
        self._write_raises = write_raises or FfmpegWriteError("simulated write failure")
        self._poll_returns_after = poll_returns_after
        self._poll_exit_code = poll_exit_code
        self._started = False

    @property
    def is_running(self) -> bool:
        return self._started

    def start(self) -> None:
        self.start_calls += 1
        if self._start_raises is not None:
            raise self._start_raises
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


class FakeApiClient:
    def __init__(
        self,
        *,
        approved_width: int = 1280,
        approved_height: int = 720,
        approved_fps: int = 30,
        approved_codec: str = "h264",
        approved_bitrate_kbps: int = 3000,
        approved_latency_mode: str = "balanced",
        srt_url: str | None = SECRET_SRT_URL,
        agent_raises: Exception | None = None,
        get_session_states: list[str] | None = None,
    ) -> None:
        self._approved = ApprovedStreamConfig(
            width=approved_width,
            height=approved_height,
            fps=approved_fps,
            codec=approved_codec,
            bitrate_kbps=approved_bitrate_kbps,
            latency_mode=approved_latency_mode,
        )
        self._srt_url = srt_url
        self._agent_raises = agent_raises
        self._get_session_states: list[str] = list(get_session_states or [])
        self.register_agent_calls: list[AgentRegisterRequest] = []
        self.register_camera_calls: list[CameraRegisterRequest] = []
        self.start_session_calls: list[StreamSessionRequest] = []
        self.stop_session_calls: list[str] = []
        self.get_session_calls: list[str] = []

    def __enter__(self) -> FakeApiClient:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def register_agent(self, req: AgentRegisterRequest) -> AgentRegisterResponse:
        self.register_agent_calls.append(req)
        if self._agent_raises is not None:
            raise self._agent_raises
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
        return StreamSessionResponse(
            session=Session(
                id="sess_99",
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
        # Default: report the session as ``streaming`` so the
        # supervisor's M7.2 ingest-confirmation wait returns
        # immediately. Tests that want a different sequence of
        # states swap in via ``get_session_states`` (FIFO).
        if self._get_session_states:
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


def _state() -> Any:
    from upstream_agent.state import State

    return State(agent_id=None, device_id=DEVICE_ID, last_camera_id=None)


def _happy_probe() -> FfmpegProbeResult:
    return FfmpegProbeResult(
        available=True, version="9.0.0", srt_supported=True,
        h264_encoder="libx264", error=None,
    )


def _make_factories(
    *,
    cam: FakeCamera | None = None,
    client: FakeApiClient | None = None,
    ffmpeg: FakeFfmpeg | None = None,
) -> dict:
    """Build factory kwargs that lock in the supplied fakes."""

    cam = cam or FakeCamera(_cfg().camera)
    client = client or FakeApiClient()

    def camera_factory(c: CameraConfig) -> VideoSource:
        return cam

    def client_factory(c: ApiConfig) -> Any:
        return client

    if ffmpeg is None:
        ffmpeg_holder: list[FakeFfmpeg] = []

        def ffmpeg_factory(cmd: list[str], redacted: list[str]) -> Any:
            ff = FakeFfmpeg(cmd, redacted)
            ffmpeg_holder.append(ff)
            return ff
    else:
        ffmpeg_holder = [ffmpeg]

        def ffmpeg_factory(cmd: list[str], redacted: list[str]) -> Any:
            ffmpeg.command = list(cmd)
            ffmpeg.redacted_command = list(redacted)
            return ffmpeg

    return dict(
        cam=cam, client=client, ffmpeg_factory=ffmpeg_factory,
        ffmpeg_holder=ffmpeg_holder,
        camera_factory=camera_factory, client_factory=client_factory,
    )


def _collect_events() -> tuple[list[tuple[str, str]], Any]:
    events: list[tuple[str, str]] = []

    def on_event(stage: str, message: str) -> None:
        events.append((stage, message))

    return events, on_event


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_completes_max_frames_and_cleans_up_in_order() -> None:
    factories = _make_factories()
    events, on_event = _collect_events()

    result, _ = run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        ffmpeg_probe=_happy_probe(),
        on_event=on_event,
        max_frames=3,
    )

    assert isinstance(result, RunResult)
    assert result.frames_written == 3
    assert result.exit_reason == "max_frames"

    cam: FakeCamera = factories["cam"]
    client: FakeApiClient = factories["client"]
    ffmpeg: FakeFfmpeg = factories["ffmpeg_holder"][0]

    assert cam.open_calls == 1
    assert cam.read_calls == 3
    assert cam.close_calls == 1

    assert len(client.register_agent_calls) == 1
    assert len(client.register_camera_calls) == 1
    assert len(client.start_session_calls) == 1
    assert client.stop_session_calls == ["sess_99"]

    assert ffmpeg.start_calls == 1
    assert len(ffmpeg.write_calls) == 3
    assert ffmpeg.stop_calls == 1

    # Event ordering: heartbeat_stopped → ffmpeg_stopped →
    # session_stopped → camera_released. M7 moved session-stop into
    # the per-attempt cleanup so the old session is released before
    # the next attempt starts; camera close is outer cleanup. The
    # safe-shutdown contract (no zombie FFmpeg, no leaked sessions)
    # is unchanged.
    stages = [stage for stage, _ in events]
    assert stages.index("ffmpeg_stopped") < stages.index("session_stopped")
    assert stages.index("session_stopped") < stages.index("camera_released")


def test_capabilities_payload_reflects_probe() -> None:
    factories = _make_factories()
    run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=1,
    )
    client: FakeApiClient = factories["client"]
    sent = client.register_agent_calls[0]
    assert sent.capabilities == {"ffmpeg": "9.0.0", "srt": True}


def test_save_progress_called_twice_on_success() -> None:
    factories = _make_factories()
    saved: list = []
    run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        ffmpeg_probe=_happy_probe(),
        save_progress=saved.append,
        max_frames=1,
    )
    assert len(saved) == 2
    assert saved[0].agent_id == "agent_1"
    assert saved[1].last_camera_id == "cam_1"


def test_session_started_before_ffmpeg_started() -> None:
    """Strict ordering: API session approval precedes FFmpeg launch."""

    events, on_event = _collect_events()
    factories = _make_factories()
    run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        ffmpeg_probe=_happy_probe(),
        on_event=on_event,
        max_frames=1,
    )
    stages = [s for s, _ in events]
    assert stages.index("api_approved") < stages.index("ffmpeg_started")


def test_uses_server_approved_resolution_not_local_request() -> None:
    """Approved 1920x1080 overrides local config 1280x720 in the FFmpeg argv."""

    client = FakeApiClient(approved_width=1920, approved_height=1080, approved_fps=60)
    cam = FakeCamera(_cfg().camera, frame_shape=(1080, 1920))
    factories = _make_factories(cam=cam, client=client)

    run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=1,
    )
    ffmpeg: FakeFfmpeg = factories["ffmpeg_holder"][0]
    assert "1920x1080" in ffmpeg.command
    assert "60" in ffmpeg.command


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_ffmpeg_start_failure_still_stops_session_and_releases_camera() -> None:
    """FfmpegStartError is classified as fatal — propagates without retry."""

    bad_ffmpeg = FakeFfmpeg([], [], start_raises=FfmpegStartError("boom"))
    factories = _make_factories(ffmpeg=bad_ffmpeg)

    with pytest.raises(FfmpegStartError):
        run_agent(
            _cfg(), _state(),
            camera_factory=factories["camera_factory"],
            client_factory=factories["client_factory"],
            ffmpeg_factory=factories["ffmpeg_factory"],
            ffmpeg_probe=_happy_probe(),
            max_frames=3,
            backoff=_fast_backoff(),
        )

    cam: FakeCamera = factories["cam"]
    client: FakeApiClient = factories["client"]
    assert cam.close_calls == 1
    assert client.stop_session_calls == ["sess_99"]
    assert len(bad_ffmpeg.write_calls) == 0


def test_frame_write_failure_cleans_up_everything() -> None:
    """With ``max_attempts=1`` the supervisor reproduces pre-M7 fail-fast:
    one attempt, recoverable failure, re-raise the typed exception."""

    bad_ffmpeg = FakeFfmpeg([], [], write_raises_after=2)
    factories = _make_factories(ffmpeg=bad_ffmpeg)

    with pytest.raises(FfmpegWriteError):
        run_agent(
            _cfg(), _state(),
            camera_factory=factories["camera_factory"],
            client_factory=factories["client_factory"],
            ffmpeg_factory=factories["ffmpeg_factory"],
            ffmpeg_probe=_happy_probe(),
            max_frames=10,
            max_attempts=1,
            backoff=_fast_backoff(),
        )

    cam: FakeCamera = factories["cam"]
    client: FakeApiClient = factories["client"]
    assert cam.close_calls == 1
    assert bad_ffmpeg.stop_calls == 1
    assert client.stop_session_calls == ["sess_99"]


def test_camera_read_failure_stops_ffmpeg_and_session() -> None:
    bad_cam = FakeCamera(_cfg().camera, read_raises_after=2)
    factories = _make_factories(cam=bad_cam)

    with pytest.raises(CameraReadError):
        run_agent(
            _cfg(), _state(),
            camera_factory=factories["camera_factory"],
            client_factory=factories["client_factory"],
            ffmpeg_factory=factories["ffmpeg_factory"],
            ffmpeg_probe=_happy_probe(),
            max_frames=10,
            max_attempts=1,
            backoff=_fast_backoff(),
        )

    ffmpeg: FakeFfmpeg = factories["ffmpeg_holder"][0]
    client: FakeApiClient = factories["client"]
    assert ffmpeg.stop_calls == 1
    assert bad_cam.close_calls == 1
    assert client.stop_session_calls == ["sess_99"]


def test_ffmpeg_exit_mid_stream_raises_ffmpeg_exit_error_and_cleans_up() -> None:
    bad_ffmpeg = FakeFfmpeg([], [], poll_returns_after=2, poll_exit_code=139)
    factories = _make_factories(ffmpeg=bad_ffmpeg)

    with pytest.raises(FfmpegExitError) as exc_info:
        run_agent(
            _cfg(), _state(),
            camera_factory=factories["camera_factory"],
            client_factory=factories["client_factory"],
            ffmpeg_factory=factories["ffmpeg_factory"],
            ffmpeg_probe=_happy_probe(),
            max_frames=10,
            max_attempts=1,
            backoff=_fast_backoff(),
        )
    assert "139" in str(exc_info.value)

    cam: FakeCamera = factories["cam"]
    client: FakeApiClient = factories["client"]
    assert cam.close_calls == 1
    assert bad_ffmpeg.stop_calls == 1
    assert client.stop_session_calls == ["sess_99"]


def test_api_failure_does_not_start_ffmpeg() -> None:
    """An auth-class API failure is fatal — the typed ApiAuthError
    propagates directly. M7 dropped the M5/M6 StreamingRuntimeError
    wrapper so callers can branch on the original error class."""

    client = FakeApiClient(agent_raises=ApiAuthError("token rejected"))
    factories = _make_factories(client=client)

    with pytest.raises(ApiAuthError):
        run_agent(
            _cfg(), _state(),
            camera_factory=factories["camera_factory"],
            client_factory=factories["client_factory"],
            ffmpeg_factory=factories["ffmpeg_factory"],
            ffmpeg_probe=_happy_probe(),
            max_frames=10,
            max_attempts=1,
            backoff=_fast_backoff(),
        )

    # No ffmpeg was ever constructed (factory not called) because the
    # supervisor fails before reaching that phase.
    assert factories["ffmpeg_holder"] == []
    # Camera was opened then released.
    assert factories["cam"].close_calls == 1
    # No session existed, so no stop call.
    assert client.stop_session_calls == []


def test_keyboard_interrupt_during_loop_cleans_up_then_reraises() -> None:
    bad_cam = FakeCamera(
        _cfg().camera,
        read_raises_after=2,
        read_raises=KeyboardInterrupt(),
    )
    factories = _make_factories(cam=bad_cam)

    with pytest.raises(KeyboardInterrupt):
        run_agent(
            _cfg(), _state(),
            camera_factory=factories["camera_factory"],
            client_factory=factories["client_factory"],
            ffmpeg_factory=factories["ffmpeg_factory"],
            ffmpeg_probe=_happy_probe(),
            max_frames=10,
        )
    ffmpeg: FakeFfmpeg = factories["ffmpeg_holder"][0]
    client: FakeApiClient = factories["client"]
    assert ffmpeg.stop_calls == 1
    assert bad_cam.close_calls == 1
    assert client.stop_session_calls == ["sess_99"]


# ---------------------------------------------------------------------------
# Frame resize
# ---------------------------------------------------------------------------


def test_frame_resize_invokes_cv2_when_camera_doesnt_match_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Camera produces 1280x720 frames; server-approved is 640x480.
    cam = FakeCamera(_cfg().camera, frame_shape=(720, 1280))
    client = FakeApiClient(approved_width=640, approved_height=480, approved_fps=30)
    factories = _make_factories(cam=cam, client=client)

    resize_calls: list[tuple] = []

    fake_cv2 = types.SimpleNamespace(
        resize=lambda frame, size: (
            resize_calls.append((frame.shape, size)),
            FakeFrame(size[1], size[0], payload=b"R" * (size[0] * size[1] * 3)),
        )[1],
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=2,
    )
    assert len(resize_calls) == 2
    assert resize_calls[0][1] == (640, 480)  # cv2.resize expects (width, height)
    # Bytes written to FFmpeg correspond to the resized frame.
    ffmpeg: FakeFfmpeg = factories["ffmpeg_holder"][0]
    assert len(ffmpeg.write_calls[0]) == 640 * 480 * 3


def test_no_resize_when_camera_matches_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If frame shape already matches approved, cv2.resize must not be called."""

    factories = _make_factories()
    # Install a poisoned cv2 — any access blows up the test.
    fake_cv2 = types.SimpleNamespace(
        resize=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("resize was called but should not have been")
        ),
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=2,
    )


# ---------------------------------------------------------------------------
# Leak guards
# ---------------------------------------------------------------------------


def test_no_srt_url_streamid_or_session_token_in_events(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    factories = _make_factories()
    events, on_event = _collect_events()
    run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        ffmpeg_probe=_happy_probe(),
        on_event=on_event,
        max_frames=2,
    )

    for _stage, message in events:
        assert SECRET_SRT_URL not in message
        assert SECRET_SESSION_TOKEN not in message
        assert SECRET_STREAMID not in message
        assert SECRET_API_TOKEN not in message

    for record in caplog.records:
        msg = record.getMessage()
        assert SECRET_SRT_URL not in msg
        assert SECRET_SESSION_TOKEN not in msg
        assert SECRET_STREAMID not in msg


def test_result_object_has_no_secrets() -> None:
    factories = _make_factories()
    result, state = run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=2,
    )
    text = repr(result) + repr(state)
    assert SECRET_SRT_URL not in text
    assert SECRET_SESSION_TOKEN not in text
    assert SECRET_STREAMID not in text


# ---------------------------------------------------------------------------
# API-issued URL behaviour
# ---------------------------------------------------------------------------


def test_api_issued_srt_url_is_used_in_ffmpeg_command() -> None:
    """The API-issued SRT URL is passed straight into the FFmpeg command."""

    factories = _make_factories()
    run_agent(
        _cfg(), _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        ffmpeg_probe=_happy_probe(),
        max_frames=1,
    )
    ffmpeg: FakeFfmpeg = factories["ffmpeg_holder"][0]
    assert SECRET_SRT_URL in ffmpeg.command
