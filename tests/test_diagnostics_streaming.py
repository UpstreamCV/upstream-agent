"""Tests for :func:`upstream_agent.diagnostics.streaming.run_streaming_check`.

The fake API client reused from :mod:`tests.test_diagnostics_api`
gives the orchestrator a deterministic stream-approval flow without
ever touching httpx. The FFmpeg probe is always injected so no real
``ffmpeg`` subprocess runs.
"""

from __future__ import annotations

import logging
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
from upstream_agent.diagnostics.streaming import (
    StreamingCheckResult,
    run_streaming_check,
)
from upstream_agent.errors import ApiAuthError, ApiServerError
from upstream_agent.state import State

# Placeholder values — must not appear in any logged or returned data.
SECRET_SRT_URL = "srt://stream.test.local:8890/?streamid=STREAMING_TEST_SECRET"
SECRET_SESSION_TOKEN = "eyJhbGciOiJIUzI1NiI.STREAMING_TEST_JWT.signature"
SECRET_STREAMID = "STREAMING_TEST_SECRET"
DEVICE_ID = "device-uuid-abcdefgh-1111-2222-3333-444444444444"


# ---------------------------------------------------------------------------
# Shared fakes / fixtures
# ---------------------------------------------------------------------------


class FakeUpstreamApiClient:
    """Stand-in for UpstreamApiClient. No httpx involved."""

    def __init__(
        self,
        *,
        agent_response: AgentRegisterResponse | None = None,
        agent_raises: Exception | None = None,
        camera_response: CameraRegisterResponse | None = None,
        camera_raises: Exception | None = None,
        session_response: StreamSessionResponse | None = None,
        session_raises: Exception | None = None,
        stop_raises: Exception | None = None,
    ) -> None:
        self.agent_response = agent_response
        self.agent_raises = agent_raises
        self.camera_response = camera_response
        self.camera_raises = camera_raises
        self.session_response = session_response
        self.session_raises = session_raises
        self.stop_raises = stop_raises

        self.register_agent_calls: list[AgentRegisterRequest] = []
        self.register_camera_calls: list[CameraRegisterRequest] = []
        self.start_session_calls: list[StreamSessionRequest] = []
        self.stop_session_calls: list[str] = []

    def __enter__(self) -> FakeUpstreamApiClient:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    def register_agent(self, req: AgentRegisterRequest) -> AgentRegisterResponse:
        self.register_agent_calls.append(req)
        if self.agent_raises is not None:
            raise self.agent_raises
        assert self.agent_response is not None
        return self.agent_response

    def register_camera(self, req: CameraRegisterRequest) -> CameraRegisterResponse:
        self.register_camera_calls.append(req)
        if self.camera_raises is not None:
            raise self.camera_raises
        assert self.camera_response is not None
        return self.camera_response

    def start_stream_session(self, req: StreamSessionRequest) -> StreamSessionResponse:
        self.start_session_calls.append(req)
        if self.session_raises is not None:
            raise self.session_raises
        assert self.session_response is not None
        return self.session_response

    def stop_stream_session(self, session_id: str) -> StopStreamSessionResponse:
        self.stop_session_calls.append(session_id)
        if self.stop_raises is not None:
            raise self.stop_raises
        return StopStreamSessionResponse()


def _cfg() -> Config:
    return Config(
        api=ApiConfig(
            base_url="http://api.test.local",
            token="ups_test_real_token_value",
        ),
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
        available=True,
        version="9.0.0",
        srt_supported=True,
        h264_encoder="libx264",
        error=None,
    )


def _happy_agent() -> AgentRegisterResponse:
    return AgentRegisterResponse(
        agent=Agent(id="agent_1", status="active"),
    )


def _happy_camera() -> CameraRegisterResponse:
    return CameraRegisterResponse(
        camera=Camera(id="cam_1", external_id="webcam-0", status="active")
    )


def _happy_session(srt_url: str | None = SECRET_SRT_URL) -> StreamSessionResponse:
    approved = {
        "width": 1280,
        "height": 720,
        "fps": 30,
        "codec": "h264",
        "bitrate_kbps": 3000,
        "latency_mode": "balanced",
    }
    return StreamSessionResponse(
        session=Session(
            id="sess_99",
            state="approved",
            srt_url=srt_url,
            streamid=SECRET_STREAMID,
            requested=RequestedStreamConfig(**approved),
            approved=ApprovedStreamConfig(**approved),
        ),
        srt_url=srt_url,
        session_token=SECRET_SESSION_TOKEN,
        expires_at="2026-05-13T06:00:00Z",
    )


def _happy_client() -> FakeUpstreamApiClient:
    return FakeUpstreamApiClient(
        agent_response=_happy_agent(),
        camera_response=_happy_camera(),
        session_response=_happy_session(),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_success_with_all_phases_passing() -> None:
    fake = _happy_client()
    result, updated = run_streaming_check(
        _cfg(), _state(),
        client_factory=lambda cfg: fake,
        ffmpeg_probe=_happy_probe(),
    )

    assert isinstance(result, StreamingCheckResult)
    assert result.success is True
    assert result.ffmpeg_available is True
    assert result.ffmpeg_version == "9.0.0"
    assert result.srt_supported is True
    assert result.h264_encoder == "libx264"
    assert result.agent_id == "agent_1"
    assert result.camera_id == "cam_1"
    assert result.session_id == "sess_99"
    assert result.approved_codec == "h264"
    assert result.approved_width == 1280
    assert result.approved_height == 720
    assert result.approved_fps == 30
    assert result.approved_bitrate_kbps == 3000
    assert result.approved_latency_mode == "balanced"
    assert result.srt_url_present is True
    assert result.srt_url_valid is True
    assert result.command_build_success is True
    assert result.error is None
    assert result.failed_step is None
    assert result.stop_warning is None

    # State was updated.
    assert updated.agent_id == "agent_1"
    assert updated.last_camera_id == "cam_1"

    # Cleanup stop fired exactly once for the approved session.
    assert fake.stop_session_calls == ["sess_99"]


def test_capabilities_payload_reflects_probe() -> None:
    fake = _happy_client()
    run_streaming_check(
        _cfg(), _state(),
        client_factory=lambda cfg: fake,
        ffmpeg_probe=_happy_probe(),
    )

    sent = fake.register_agent_calls[0]
    assert sent.capabilities == {"ffmpeg": "9.0.0", "srt": True}


def test_save_progress_called_twice_on_success() -> None:
    fake = _happy_client()
    calls: list[State] = []

    def capture(state: State) -> None:
        calls.append(state)

    run_streaming_check(
        _cfg(), _state(),
        client_factory=lambda cfg: fake,
        save_progress=capture,
        ffmpeg_probe=_happy_probe(),
    )

    assert len(calls) == 2
    # First call carries agent_id only, second carries both.
    assert calls[0].agent_id == "agent_1"
    assert calls[0].last_camera_id is None
    assert calls[1].agent_id == "agent_1"
    assert calls[1].last_camera_id == "cam_1"


# ---------------------------------------------------------------------------
# Local-phase failures (no API call should fire)
# ---------------------------------------------------------------------------


def test_ffmpeg_missing_short_circuits_before_api() -> None:
    fake = _happy_client()
    probe = FfmpegProbeResult(
        available=False, version=None, srt_supported=False,
        h264_encoder=None, error="FFmpeg is not installed or not on PATH.",
    )
    result, _ = run_streaming_check(
        _cfg(), _state(),
        client_factory=lambda cfg: fake,
        ffmpeg_probe=probe,
    )

    assert result.success is False
    assert result.failed_step == "ffmpeg"
    assert "FFmpeg" in (result.error or "")
    # No API call fired.
    assert fake.register_agent_calls == []
    assert fake.start_session_calls == []
    assert fake.stop_session_calls == []


def test_srt_unsupported_short_circuits_before_api() -> None:
    fake = _happy_client()
    probe = FfmpegProbeResult(
        available=True, version="8.1.1", srt_supported=False,
        h264_encoder="libx264", error=None,
    )
    result, _ = run_streaming_check(
        _cfg(), _state(),
        client_factory=lambda cfg: fake,
        ffmpeg_probe=probe,
    )

    assert result.success is False
    assert result.failed_step == "srt_support"
    assert "libsrt" in (result.error or "")
    assert fake.register_agent_calls == []


def test_no_h264_encoder_short_circuits_before_api() -> None:
    fake = _happy_client()
    probe = FfmpegProbeResult(
        available=True, version="8.1.1", srt_supported=True,
        h264_encoder=None, error=None,
    )
    result, _ = run_streaming_check(
        _cfg(), _state(),
        client_factory=lambda cfg: fake,
        ffmpeg_probe=probe,
    )

    assert result.success is False
    assert result.failed_step == "h264_encoder"
    assert "H264" in (result.error or "")
    assert fake.register_agent_calls == []


# ---------------------------------------------------------------------------
# API-phase failures
# ---------------------------------------------------------------------------


def test_register_agent_failure_surfaces_step_and_keeps_probe_fields() -> None:
    fake = FakeUpstreamApiClient(agent_raises=ApiAuthError("token rejected"))
    result, _ = run_streaming_check(
        _cfg(), _state(),
        client_factory=lambda cfg: fake,
        ffmpeg_probe=_happy_probe(),
    )

    assert result.success is False
    assert result.failed_step == "register_agent"
    # Probe info still populated.
    assert result.ffmpeg_available is True
    assert result.srt_supported is True
    assert result.h264_encoder == "libx264"
    # No session was created.
    assert fake.start_session_calls == []
    assert fake.stop_session_calls == []


def test_start_session_failure_no_stop_called() -> None:
    fake = FakeUpstreamApiClient(
        agent_response=_happy_agent(),
        camera_response=_happy_camera(),
        session_raises=ApiServerError("overloaded"),
    )
    result, _ = run_streaming_check(
        _cfg(), _state(),
        client_factory=lambda cfg: fake,
        ffmpeg_probe=_happy_probe(),
    )

    assert result.success is False
    assert result.failed_step == "start_stream_session"
    # Stop wasn't called — no session to release.
    assert fake.stop_session_calls == []


def test_missing_srt_url_fails_with_srt_url_parse_step_and_stops_session() -> None:
    fake = FakeUpstreamApiClient(
        agent_response=_happy_agent(),
        camera_response=_happy_camera(),
        session_response=_happy_session(srt_url=None),
    )
    result, _ = run_streaming_check(
        _cfg(), _state(),
        client_factory=lambda cfg: fake,
        ffmpeg_probe=_happy_probe(),
    )

    assert result.success is False
    assert result.failed_step == "srt_url_parse"
    assert result.srt_url_present is False
    # Stop was called for cleanup even though the URL was missing.
    assert fake.stop_session_calls == ["sess_99"]


def test_invalid_srt_url_fails_and_cleanup_stop_is_called() -> None:
    fake = FakeUpstreamApiClient(
        agent_response=_happy_agent(),
        camera_response=_happy_camera(),
        session_response=_happy_session(srt_url="rtsp://wrong-scheme:1234/"),
    )
    result, _ = run_streaming_check(
        _cfg(), _state(),
        client_factory=lambda cfg: fake,
        ffmpeg_probe=_happy_probe(),
    )

    assert result.success is False
    assert result.failed_step == "srt_url_parse"
    assert result.srt_url_present is True
    assert result.srt_url_valid is False
    assert fake.stop_session_calls == ["sess_99"]


def test_command_build_failure_falls_through_and_cleans_up() -> None:
    # An approved config with an unsupported codec triggers the
    # command builder to raise.
    bad_approved = {
        "width": 1280, "height": 720, "fps": 30,
        "codec": "vp9", "bitrate_kbps": 3000, "latency_mode": "balanced",
    }
    fake = FakeUpstreamApiClient(
        agent_response=_happy_agent(),
        camera_response=_happy_camera(),
        session_response=StreamSessionResponse(
            session=Session(
                id="sess_99",
                state="approved",
                srt_url=SECRET_SRT_URL,
                requested=RequestedStreamConfig(
                    width=1280, height=720, fps=30, codec="h264",
                    bitrate_kbps=3000, latency_mode="balanced",
                ),
                approved=ApprovedStreamConfig(**bad_approved),
            ),
            srt_url=SECRET_SRT_URL,
        ),
    )
    result, _ = run_streaming_check(
        _cfg(), _state(),
        client_factory=lambda cfg: fake,
        ffmpeg_probe=_happy_probe(),
    )

    assert result.success is False
    assert result.failed_step == "command_build"
    assert result.srt_url_valid is True
    assert result.command_build_success is False
    assert fake.stop_session_calls == ["sess_99"]


# ---------------------------------------------------------------------------
# Cleanup-stop is best-effort
# ---------------------------------------------------------------------------


def test_stop_failure_after_success_becomes_warning_not_failure() -> None:
    fake = FakeUpstreamApiClient(
        agent_response=_happy_agent(),
        camera_response=_happy_camera(),
        session_response=_happy_session(),
        stop_raises=ApiServerError("backend stop overloaded"),
    )
    result, _ = run_streaming_check(
        _cfg(), _state(),
        client_factory=lambda cfg: fake,
        ffmpeg_probe=_happy_probe(),
    )

    assert result.success is True
    assert result.stop_warning is not None
    assert "overloaded" in result.stop_warning


# ---------------------------------------------------------------------------
# Leak guards
# ---------------------------------------------------------------------------


def test_streaming_check_does_not_log_srt_url_or_session_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = _happy_client()
    run_streaming_check(
        _cfg(), _state(),
        client_factory=lambda cfg: fake,
        ffmpeg_probe=_happy_probe(),
    )

    for record in caplog.records:
        msg = record.getMessage()
        assert SECRET_SRT_URL not in msg
        assert SECRET_SESSION_TOKEN not in msg
        assert SECRET_STREAMID not in msg


def test_result_object_contains_no_sensitive_strings() -> None:
    fake = _happy_client()
    result, _ = run_streaming_check(
        _cfg(), _state(),
        client_factory=lambda cfg: fake,
        ffmpeg_probe=_happy_probe(),
    )

    text = repr(result)
    assert SECRET_SRT_URL not in text
    assert SECRET_SESSION_TOKEN not in text
    assert SECRET_STREAMID not in text
