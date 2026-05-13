"""Tests for :func:`upstream_agent.diagnostics.api.run_api_check`.

A `FakeUpstreamApiClient` drives each three-step scenario precisely,
with no HTTP involvement. Confirms the orchestrator builds the right
M3.2 request payloads (generated agent name, no hostname, lowercased
OS, `webcam-0` external_id, h264 / 3000 kbps defaults) and parses
the new nested response shapes (`agent.id`, `camera.id`,
`session.id`, `session.approved.*`).
"""

from __future__ import annotations

import dataclasses
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
from upstream_agent.diagnostics.api import ApiCheckResult, run_api_check
from upstream_agent.errors import ApiAuthError, ApiServerError, ApiValidationError
from upstream_agent.state import State

SECRET_SRT_URL = "srt://stream.test.local:1935/live/SUPER_SECRET_STREAM_TOKEN"
SECRET_SESSION_TOKEN = "eyJhbGciOiJIUzI1NiI.SECRET_JWT_PAYLOAD.signature"
DEVICE_ID = "device-uuid-abcdefgh-1111-2222-3333-444444444444"


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
        stop_response: StopStreamSessionResponse | None = None,
        stop_raises: Exception | None = None,
    ) -> None:
        self.agent_response = agent_response
        self.agent_raises = agent_raises
        self.camera_response = camera_response
        self.camera_raises = camera_raises
        self.session_response = session_response
        self.session_raises = session_raises
        self.stop_response = stop_response
        self.stop_raises = stop_raises

        self.enter_count = 0
        self.exit_count = 0
        self.register_agent_calls: list[AgentRegisterRequest] = []
        self.register_camera_calls: list[CameraRegisterRequest] = []
        self.start_session_calls: list[StreamSessionRequest] = []
        self.stop_session_calls: list[str] = []

    def __enter__(self) -> FakeUpstreamApiClient:
        self.enter_count += 1
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.exit_count += 1

    def register_agent(
        self, req: AgentRegisterRequest
    ) -> AgentRegisterResponse:
        self.register_agent_calls.append(req)
        if self.agent_raises is not None:
            raise self.agent_raises
        assert self.agent_response is not None
        return self.agent_response

    def register_camera(
        self, req: CameraRegisterRequest
    ) -> CameraRegisterResponse:
        self.register_camera_calls.append(req)
        if self.camera_raises is not None:
            raise self.camera_raises
        assert self.camera_response is not None
        return self.camera_response

    def start_stream_session(
        self, req: StreamSessionRequest
    ) -> StreamSessionResponse:
        self.start_session_calls.append(req)
        if self.session_raises is not None:
            raise self.session_raises
        assert self.session_response is not None
        return self.session_response

    def stop_stream_session(self, session_id: str) -> StopStreamSessionResponse:
        self.stop_session_calls.append(session_id)
        if self.stop_raises is not None:
            raise self.stop_raises
        return self.stop_response or StopStreamSessionResponse()


def _cfg(latency_mode: str = "balanced") -> Config:
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
        stream=StreamConfig(latency_mode=latency_mode),
        logging=LoggingConfig(),
    )


def _state(agent_id: str | None = None, last_camera_id: str | None = None) -> State:
    return State(
        agent_id=agent_id,
        device_id=DEVICE_ID,
        last_camera_id=last_camera_id,
    )


def _happy_agent_resp() -> AgentRegisterResponse:
    return AgentRegisterResponse(
        agent=Agent(
            id="agent_1",
            account_id="acct_1",
            name="upstream-agent-deviceuu",
            status="active",
        ),
        server_time="2026-05-13T02:00:00Z",
    )


def _happy_camera_resp() -> CameraRegisterResponse:
    return CameraRegisterResponse(
        camera=Camera(
            id="cam_1",
            agent_id="agent_1",
            name="Front Door",
            external_id="webcam-0",
            status="active",
        )
    )


def _happy_session_resp(approved_kwargs: dict | None = None) -> StreamSessionResponse:
    base_approved = {
        "width": 1280,
        "height": 720,
        "fps": 30,
        "codec": "h264",
        "bitrate_kbps": 3000,
        "latency_mode": "balanced",
    }
    if approved_kwargs:
        base_approved.update(approved_kwargs)
    return StreamSessionResponse(
        session=Session(
            id="sess_99",
            agent_id="agent_1",
            camera_id="cam_1",
            state="approved",
            srt_url=SECRET_SRT_URL,
            streamid="publish:secret",
            expires_at="2026-05-13T06:00:00Z",
            requested=RequestedStreamConfig(**base_approved),
            approved=ApprovedStreamConfig(**base_approved),
        ),
        srt_url=SECRET_SRT_URL,
        session_token=SECRET_SESSION_TOKEN,
        expires_at="2026-05-13T06:00:00Z",
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_success_and_calls_save_progress_twice() -> None:
    fake = FakeUpstreamApiClient(
        agent_response=_happy_agent_resp(),
        camera_response=_happy_camera_resp(),
        session_response=_happy_session_resp(),
    )
    progress_states: list[State] = []

    result, final_state = run_api_check(
        _cfg(),
        _state(),
        client_factory=lambda cfg: fake,
        save_progress=progress_states.append,
    )

    assert isinstance(result, ApiCheckResult)
    assert result.success is True
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
    assert result.error is None
    assert result.failed_step is None
    assert result.stop_warning is None

    assert final_state.agent_id == "agent_1"
    assert final_state.last_camera_id == "cam_1"

    assert len(progress_states) == 2
    assert progress_states[0].agent_id == "agent_1"
    assert progress_states[0].last_camera_id is None
    assert progress_states[1].agent_id == "agent_1"
    assert progress_states[1].last_camera_id == "cam_1"

    # Cleanup stop must have been called with the just-approved session id.
    assert fake.stop_session_calls == ["sess_99"]


def test_happy_path_closes_client() -> None:
    fake = FakeUpstreamApiClient(
        agent_response=_happy_agent_resp(),
        camera_response=_happy_camera_resp(),
        session_response=_happy_session_resp(),
    )
    run_api_check(_cfg(), _state(), client_factory=lambda cfg: fake)
    assert fake.enter_count == 1
    assert fake.exit_count == 1


# ---------------------------------------------------------------------------
# Step failures
# ---------------------------------------------------------------------------


def test_register_agent_failure_returns_no_state_change() -> None:
    fake = FakeUpstreamApiClient(
        agent_raises=ApiAuthError("API token was rejected.")
    )
    progress_states: list[State] = []

    result, final_state = run_api_check(
        _cfg(),
        _state(),
        client_factory=lambda cfg: fake,
        save_progress=progress_states.append,
    )

    assert result.success is False
    assert result.failed_step == "register_agent"
    assert "rejected" in result.error
    assert result.agent_id is None
    assert result.camera_id is None
    assert result.srt_url_present is False

    assert final_state.agent_id is None
    assert final_state.last_camera_id is None
    assert progress_states == []
    assert fake.register_camera_calls == []
    assert fake.start_session_calls == []
    assert fake.exit_count == 1


def test_register_camera_failure_persists_agent_id_only() -> None:
    fake = FakeUpstreamApiClient(
        agent_response=_happy_agent_resp(),
        camera_raises=ApiValidationError("camera name already exists"),
    )
    progress_states: list[State] = []

    result, final_state = run_api_check(
        _cfg(),
        _state(),
        client_factory=lambda cfg: fake,
        save_progress=progress_states.append,
    )

    assert result.success is False
    assert result.failed_step == "register_camera"
    assert result.agent_id == "agent_1"
    assert result.camera_id is None

    assert final_state.agent_id == "agent_1"
    assert final_state.last_camera_id is None
    assert len(progress_states) == 1
    assert progress_states[0].agent_id == "agent_1"
    assert fake.start_session_calls == []


def test_start_stream_session_failure_persists_both_ids() -> None:
    fake = FakeUpstreamApiClient(
        agent_response=_happy_agent_resp(),
        camera_response=_happy_camera_resp(),
        session_raises=ApiServerError("backend overloaded"),
    )
    progress_states: list[State] = []

    result, final_state = run_api_check(
        _cfg(),
        _state(),
        client_factory=lambda cfg: fake,
        save_progress=progress_states.append,
    )

    assert result.success is False
    assert result.failed_step == "start_stream_session"
    assert result.agent_id == "agent_1"
    assert result.camera_id == "cam_1"
    assert result.session_id is None
    assert result.srt_url_present is False
    assert result.stop_warning is None

    assert final_state.agent_id == "agent_1"
    assert final_state.last_camera_id == "cam_1"
    assert len(progress_states) == 2
    # Stop must NOT have been called — there's no session to stop.
    assert fake.stop_session_calls == []


# ---------------------------------------------------------------------------
# Stop-cleanup behavior
# ---------------------------------------------------------------------------


def test_stop_failure_is_non_fatal_and_records_warning() -> None:
    """If stop fails after a successful start, the *check* still
    succeeds. The error string lands on ``result.stop_warning`` for
    the CLI to surface."""

    fake = FakeUpstreamApiClient(
        agent_response=_happy_agent_resp(),
        camera_response=_happy_camera_resp(),
        session_response=_happy_session_resp(),
        stop_raises=ApiServerError("backend stop overloaded"),
    )

    result, final_state = run_api_check(
        _cfg(), _state(), client_factory=lambda cfg: fake
    )

    # Registration / session approval all succeeded — overall success.
    assert result.success is True
    assert result.failed_step is None
    assert result.error is None
    assert result.session_id == "sess_99"
    assert result.srt_url_present is True
    # Stop attempt was made.
    assert fake.stop_session_calls == ["sess_99"]
    # And the warning surfaces the error string for the CLI.
    assert result.stop_warning is not None
    assert "overloaded" in result.stop_warning
    # State still carries both IDs.
    assert final_state.agent_id == "agent_1"
    assert final_state.last_camera_id == "cam_1"


def test_stop_is_called_with_the_returned_session_id() -> None:
    """If the server returns a different session_id than the
    happy-path default, the orchestrator stops *that* one."""

    fake = FakeUpstreamApiClient(
        agent_response=_happy_agent_resp(),
        camera_response=_happy_camera_resp(),
        session_response=StreamSessionResponse(
            session=Session(
                id="sess_DIFFERENT",
                state="approved",
                srt_url=SECRET_SRT_URL,
                requested=RequestedStreamConfig(
                    width=1280, height=720, fps=30,
                    codec="h264", bitrate_kbps=3000, latency_mode="balanced",
                ),
                approved=ApprovedStreamConfig(
                    width=1280, height=720, fps=30,
                    codec="h264", bitrate_kbps=3000, latency_mode="balanced",
                ),
            ),
            srt_url=SECRET_SRT_URL,
            session_token=SECRET_SESSION_TOKEN,
        ),
    )

    run_api_check(_cfg(), _state(), client_factory=lambda cfg: fake)
    assert fake.stop_session_calls == ["sess_DIFFERENT"]


# ---------------------------------------------------------------------------
# Sensitive-data leak guards
# ---------------------------------------------------------------------------


def test_srt_url_and_session_token_do_not_appear_in_result_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeUpstreamApiClient(
        agent_response=_happy_agent_resp(),
        camera_response=_happy_camera_resp(),
        session_response=_happy_session_resp(),
    )

    with caplog.at_level(logging.DEBUG):
        result, _ = run_api_check(
            _cfg(), _state(), client_factory=lambda cfg: fake
        )

    blob = str(dataclasses.asdict(result))
    assert SECRET_SRT_URL not in blob
    assert SECRET_SESSION_TOKEN not in blob

    for record in caplog.records:
        msg = record.getMessage()
        assert SECRET_SRT_URL not in msg
        assert SECRET_SESSION_TOKEN not in msg


# ---------------------------------------------------------------------------
# Request payload sanity (M3.2 confirmed shapes)
# ---------------------------------------------------------------------------


def test_agent_register_payload_uses_generated_name_no_hostname() -> None:
    fake = FakeUpstreamApiClient(
        agent_response=_happy_agent_resp(),
        camera_response=_happy_camera_resp(),
        session_response=_happy_session_resp(),
    )

    # Inject capabilities so this test does not depend on the local
    # FFmpeg install state.
    fixed_caps: dict[str, bool | str] = {"ffmpeg": False, "srt": False}
    run_api_check(
        _cfg(),
        _state(),
        client_factory=lambda cfg: fake,
        capabilities=fixed_caps,
    )

    assert len(fake.register_agent_calls) == 1
    sent = fake.register_agent_calls[0]
    # Generated from the first 8 hex chars of device_id (no dashes).
    expected_name_prefix = "upstream-agent-"
    assert sent.name.startswith(expected_name_prefix)
    assert len(sent.name) == len(expected_name_prefix) + 8
    # Stable across runs (same device_id → same name).
    fake2 = FakeUpstreamApiClient(
        agent_response=_happy_agent_resp(),
        camera_response=_happy_camera_resp(),
        session_response=_happy_session_resp(),
    )
    run_api_check(
        _cfg(),
        _state(),
        client_factory=lambda cfg: fake2,
        capabilities=fixed_caps,
    )
    assert fake2.register_agent_calls[0].name == sent.name

    # Hostname must not be set.
    assert sent.hostname is None

    # OS is lowercased.
    assert sent.os == sent.os.lower()
    assert sent.os != ""

    # Capabilities mirror the injected dict — this proves the kwarg
    # actually wires through to the request payload.
    assert sent.capabilities == fixed_caps


def test_agent_register_capabilities_default_to_probed_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no capabilities kwarg is given, the FFmpeg probe drives the payload."""

    from upstream_agent.diagnostics import api as api_module
    from upstream_agent.diagnostics.ffmpeg_check import FfmpegProbeResult

    stub = FfmpegProbeResult(
        available=True,
        version="9.9.9",
        srt_supported=True,
        h264_encoder="libx264",
        error=None,
    )
    monkeypatch.setattr(api_module, "probe_ffmpeg", lambda: stub)

    fake = FakeUpstreamApiClient(
        agent_response=_happy_agent_resp(),
        camera_response=_happy_camera_resp(),
        session_response=_happy_session_resp(),
    )
    run_api_check(_cfg(), _state(), client_factory=lambda cfg: fake)

    sent = fake.register_agent_calls[0]
    assert sent.capabilities == {"ffmpeg": "9.9.9", "srt": True}


def test_camera_register_payload_uses_stable_external_id_and_device_path() -> None:
    fake = FakeUpstreamApiClient(
        agent_response=_happy_agent_resp(),
        camera_response=_happy_camera_resp(),
        session_response=_happy_session_resp(),
    )

    run_api_check(_cfg(), _state(), client_factory=lambda cfg: fake)

    assert len(fake.register_camera_calls) == 1
    sent = fake.register_camera_calls[0]
    assert sent.agent_id == "agent_1"
    assert sent.name == "Front Door"
    assert sent.external_id == "webcam-0"
    assert sent.device_path == "webcam:0"
    assert sent.default_width == 1280
    assert sent.default_codec == "h264"
    assert sent.default_bitrate_kbps == 3000
    assert sent.default_latency_mode == "balanced"
    assert sent.metadata == {}


def test_stream_session_payload_uses_nested_requested() -> None:
    fake = FakeUpstreamApiClient(
        agent_response=_happy_agent_resp(),
        camera_response=_happy_camera_resp(),
        session_response=_happy_session_resp(),
    )

    run_api_check(_cfg(latency_mode="low"), _state(), client_factory=lambda cfg: fake)

    assert len(fake.start_session_calls) == 1
    sent = fake.start_session_calls[0]
    assert sent.camera_id == "cam_1"
    assert sent.requested.codec == "h264"
    assert sent.requested.bitrate_kbps == 3000
    assert sent.requested.latency_mode == "low"
