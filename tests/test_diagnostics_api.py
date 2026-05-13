"""Tests for :func:`upstream_agent.diagnostics.api.run_api_check`.

A `FakeUpstreamApiClient` lets us drive each three-step scenario
precisely without any HTTP involvement.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

import pytest

from upstream_agent.api.schemas import (
    AgentRegisterRequest,
    AgentRegisterResponse,
    ApprovedStreamConfig,
    CameraRegisterRequest,
    CameraRegisterResponse,
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
    ) -> None:
        self.agent_response = agent_response
        self.agent_raises = agent_raises
        self.camera_response = camera_response
        self.camera_raises = camera_raises
        self.session_response = session_response
        self.session_raises = session_raises

        self.enter_count = 0
        self.exit_count = 0
        self.register_agent_calls: list[AgentRegisterRequest] = []
        self.register_camera_calls: list[CameraRegisterRequest] = []
        self.start_session_calls: list[StreamSessionRequest] = []

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


def _cfg() -> Config:
    return Config(
        api=ApiConfig(base_url="http://api.test.local", token="real_token_value"),
        camera=CameraConfig(
            name="Front Door",
            source_type="webcam",
            source=0,
            requested_width=1280,
            requested_height=720,
            requested_fps=30,
        ),
        stream=StreamConfig(),
        logging=LoggingConfig(),
    )


def _state(agent_id: str | None = None, last_camera_id: str | None = None) -> State:
    return State(
        agent_id=agent_id,
        device_id="device-uuid-abc",
        last_camera_id=last_camera_id,
    )


def _happy_session() -> StreamSessionResponse:
    return StreamSessionResponse(
        session_id="sess_99",
        approved_config=ApprovedStreamConfig(
            codec="h264",
            width=1280,
            height=720,
            fps=30,
            bitrate=2_500_000,
            srt_url=SECRET_SRT_URL,
            expires_in=3600,
        ),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_success_and_calls_save_progress_twice() -> None:
    fake = FakeUpstreamApiClient(
        agent_response=AgentRegisterResponse(agent_id="agent_1"),
        camera_response=CameraRegisterResponse(camera_id="cam_1"),
        session_response=_happy_session(),
    )
    progress_states: list[State] = []

    def save_progress(s: State) -> None:
        progress_states.append(s)

    result, final_state = run_api_check(
        _cfg(),
        _state(),
        client_factory=lambda cfg: fake,
        save_progress=save_progress,
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
    assert result.srt_url_present is True
    assert result.error is None
    assert result.failed_step is None

    # State carries both IDs.
    assert final_state.agent_id == "agent_1"
    assert final_state.last_camera_id == "cam_1"

    # save_progress called exactly twice: after agent, after camera.
    assert len(progress_states) == 2
    assert progress_states[0].agent_id == "agent_1"
    assert progress_states[0].last_camera_id is None
    assert progress_states[1].agent_id == "agent_1"
    assert progress_states[1].last_camera_id == "cam_1"


def test_happy_path_closes_client() -> None:
    fake = FakeUpstreamApiClient(
        agent_response=AgentRegisterResponse(agent_id="agent_1"),
        camera_response=CameraRegisterResponse(camera_id="cam_1"),
        session_response=_happy_session(),
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
    assert result.session_id is None
    assert result.srt_url_present is False

    assert final_state.agent_id is None
    assert final_state.last_camera_id is None
    assert progress_states == []
    # Camera and session must not have been called.
    assert fake.register_camera_calls == []
    assert fake.start_session_calls == []
    # Client still closed.
    assert fake.exit_count == 1


def test_register_camera_failure_persists_agent_id_only() -> None:
    fake = FakeUpstreamApiClient(
        agent_response=AgentRegisterResponse(agent_id="agent_42"),
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
    assert result.agent_id == "agent_42"
    assert result.camera_id is None

    assert final_state.agent_id == "agent_42"
    assert final_state.last_camera_id is None
    # save_progress fired once (after agent register).
    assert len(progress_states) == 1
    assert progress_states[0].agent_id == "agent_42"
    # Session not attempted.
    assert fake.start_session_calls == []


def test_start_stream_session_failure_persists_both_ids() -> None:
    fake = FakeUpstreamApiClient(
        agent_response=AgentRegisterResponse(agent_id="agent_x"),
        camera_response=CameraRegisterResponse(camera_id="cam_x"),
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
    assert result.agent_id == "agent_x"
    assert result.camera_id == "cam_x"
    assert result.session_id is None
    assert result.srt_url_present is False

    assert final_state.agent_id == "agent_x"
    assert final_state.last_camera_id == "cam_x"
    assert len(progress_states) == 2


# ---------------------------------------------------------------------------
# SRT URL leak guard
# ---------------------------------------------------------------------------


def test_srt_url_does_not_appear_in_result_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeUpstreamApiClient(
        agent_response=AgentRegisterResponse(agent_id="agent_1"),
        camera_response=CameraRegisterResponse(camera_id="cam_1"),
        session_response=_happy_session(),
    )

    with caplog.at_level(logging.DEBUG):
        result, _ = run_api_check(_cfg(), _state(), client_factory=lambda cfg: fake)

    # 1. SRT URL must not be in any field of the result dataclass.
    blob = str(dataclasses.asdict(result))
    assert SECRET_SRT_URL not in blob

    # 2. SRT URL must not have been logged anywhere.
    for record in caplog.records:
        assert SECRET_SRT_URL not in record.getMessage()


# ---------------------------------------------------------------------------
# Request payload sanity
# ---------------------------------------------------------------------------


def test_agent_register_payload_carries_device_id_and_version() -> None:
    fake = FakeUpstreamApiClient(
        agent_response=AgentRegisterResponse(agent_id="agent_1"),
        camera_response=CameraRegisterResponse(camera_id="cam_1"),
        session_response=_happy_session(),
    )

    run_api_check(
        _cfg(),
        _state(),
        client_factory=lambda cfg: fake,
    )

    assert len(fake.register_agent_calls) == 1
    sent = fake.register_agent_calls[0]
    assert sent.device_id == "device-uuid-abc"
    assert sent.agent_version  # non-empty


def test_camera_register_payload_uses_agent_id_and_camera_fields() -> None:
    fake = FakeUpstreamApiClient(
        agent_response=AgentRegisterResponse(agent_id="agent_NEW"),
        camera_response=CameraRegisterResponse(camera_id="cam_1"),
        session_response=_happy_session(),
    )

    run_api_check(_cfg(), _state(), client_factory=lambda cfg: fake)

    assert len(fake.register_camera_calls) == 1
    sent = fake.register_camera_calls[0]
    assert sent.agent_id == "agent_NEW"
    assert sent.name == "Front Door"
    assert sent.source_type == "webcam"
    assert sent.source == 0
    assert sent.requested_width == 1280
