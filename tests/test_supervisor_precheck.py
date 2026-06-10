"""Supervisor limits pre-check: bootstrap fetch, clamped session requests,
the requested-vs-approved decision line, and blocked-by-limits reporting."""

from __future__ import annotations

from typing import Any

import pytest

from tests.test_supervisor import (  # type: ignore[import-not-found]
    FakeApiClient,
    FakeCamera,
    _cfg,
    _collect_events,
    _happy_probe,
    _make_factories,
    _state,
)
from upstream_agent.api.schemas import (
    BootstrapLimits,
    BootstrapResponse,
    Heartbeat,
    HeartbeatBatchResponse,
    StreamSessionRequest,
)
from upstream_agent.errors import (
    ApiConnectionError,
    ApiValidationError,
    PreflightError,
)
from upstream_agent.supervisor import run_agent


class PrecheckApiClient(FakeApiClient):
    """FakeApiClient with controllable bootstrap + heartbeat recording."""

    def __init__(
        self,
        *,
        limits: BootstrapLimits | None = None,
        bootstrap_raises: Exception | None = None,
        start_session_raises: Exception | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._limits = limits
        self._bootstrap_raises = bootstrap_raises
        self._start_session_raises = start_session_raises
        self.heartbeats_sent: list[Heartbeat] = []
        self.heartbeat_raises: Exception | None = None

    def get_bootstrap(self) -> BootstrapResponse:
        if self._bootstrap_raises is not None:
            raise self._bootstrap_raises
        return BootstrapResponse(limits=self._limits)

    def start_stream_session(self, req: StreamSessionRequest) -> Any:
        if self._start_session_raises is not None:
            self.start_session_calls.append(req)
            raise self._start_session_raises
        return super().start_stream_session(req)

    def send_heartbeat(self, *, agent_id: str, heartbeat: Heartbeat) -> HeartbeatBatchResponse:
        if self.heartbeat_raises is not None:
            raise self.heartbeat_raises
        self.heartbeats_sent.append(heartbeat)
        return HeartbeatBatchResponse(accepted=1)


def _tight_limits() -> BootstrapLimits:
    return BootstrapLimits.model_validate(
        {
            "max_fps": 15,
            "max_width": 640,
            "max_height": 360,
            "max_bitrate_kbps": 1500,
            "allowed_codecs": ["h264"],
            "allowed_stream_types": ["webcam", "rtsp"],
        }
    )


def _run(
    client: PrecheckApiClient,
    *,
    frame_shape: tuple[int, int] = (720, 1280),
    **kwargs: Any,
) -> tuple[Any, list[tuple[str, str]]]:
    # Frames matching the approved size avoid a real cv2.resize on FakeFrame.
    cam = FakeCamera(_cfg().camera, frame_shape=frame_shape)
    factories = _make_factories(client=client, cam=cam)
    events, on_event = _collect_events()
    result, _state_out = run_agent(
        _cfg(),
        _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        ffmpeg_probe=_happy_probe(),
        on_event=on_event,
        max_frames=2,
        **kwargs,
    )
    return result, events


def test_run_agent_downshifts_requested_config_to_limits() -> None:
    client = PrecheckApiClient(limits=_tight_limits())
    _result, events = _run(client)

    # Config (_cfg) asks for 1280x720@30; account caps at 640x360@15/1500.
    assert len(client.start_session_calls) == 1
    requested = client.start_session_calls[0].requested
    assert (requested.width, requested.height) == (640, 360)
    assert requested.fps == 15
    assert requested.bitrate_kbps == 1500

    # Camera registration also uses the clamped defaults.
    camera_req = client.register_camera_calls[0]
    assert (camera_req.default_width, camera_req.default_height) == (640, 360)
    assert camera_req.source_type == "webcam"

    stages = [stage for stage, _ in events]
    assert "limits_precheck" in stages
    precheck_msg = next(msg for stage, msg in events if stage == "limits_precheck")
    assert "resolution 1280x720 -> 640x360" in precheck_msg


def test_run_agent_emits_requested_vs_approved_decision_line() -> None:
    # The fake returns a fixed approved config; mirror the clamped values
    # the server would sign off on.
    client = PrecheckApiClient(
        limits=_tight_limits(),
        approved_width=640,
        approved_height=360,
        approved_fps=15,
        approved_bitrate_kbps=1500,
    )
    _result, events = _run(client, frame_shape=(360, 640))

    decision = next(msg for stage, msg in events if stage == "config_decision")
    assert "Requested: 1280x720 @ 30fps" in decision
    assert "Approved: 640x360 @ 15fps" in decision
    assert "using server-approved config" in decision


def test_run_agent_proceeds_when_bootstrap_unavailable() -> None:
    client = PrecheckApiClient(
        bootstrap_raises=ApiValidationError("GET /v1/agents/bootstrap -> 404")
    )
    result, events = _run(client)

    assert result.frames_written == 2
    stages = [stage for stage, _ in events]
    assert "bootstrap_unavailable" in stages
    # Pre-check skipped -> the raw local config goes out unchanged.
    requested = client.start_session_calls[0].requested
    assert (requested.width, requested.height, requested.fps) == (1280, 720, 30)


def test_bootstrap_connection_error_does_not_kill_the_run() -> None:
    client = PrecheckApiClient(bootstrap_raises=ApiConnectionError("no route"))
    result, _events = _run(client)
    assert result.frames_written == 2


def test_disallowed_stream_type_fails_preflight_before_any_session() -> None:
    limits = BootstrapLimits.model_validate({"allowed_stream_types": ["rtsp"]})
    client = PrecheckApiClient(limits=limits)
    factories = _make_factories(client=client)
    with pytest.raises(PreflightError) as exc:
        run_agent(
            _cfg(),
            _state(),
            camera_factory=factories["camera_factory"],
            client_factory=factories["client_factory"],
            ffmpeg_factory=factories["ffmpeg_factory"],
            ffmpeg_probe=_happy_probe(),
            max_frames=2,
        )
    assert "source type 'webcam'" in str(exc.value)
    assert client.start_session_calls == []


def test_server_limit_rejection_emits_message_and_heartbeat() -> None:
    rejection = ApiValidationError(
        "POST /v1/stream-sessions/start -> 403 Forbidden, "
        "code=limit_exceeded.max_concurrent_sessions: Concurrent session "
        "limit reached: 1 of 1 active."
    )
    client = PrecheckApiClient(limits=_tight_limits(), start_session_raises=rejection)
    factories = _make_factories(client=client)
    events, on_event = _collect_events()

    with pytest.raises(ApiValidationError):
        run_agent(
            _cfg(),
            _state(),
            camera_factory=factories["camera_factory"],
            client_factory=factories["client_factory"],
            ffmpeg_factory=factories["ffmpeg_factory"],
            ffmpeg_probe=_happy_probe(),
            on_event=on_event,
            max_frames=2,
        )

    rejected = next(msg for stage, msg in events if stage == "limits_rejected")
    assert "account limits" in rejected
    assert "limit_exceeded.max_concurrent_sessions" in rejected

    # One best-effort blocked-by-limits heartbeat went out.
    assert len(client.heartbeats_sent) == 1
    hb = client.heartbeats_sent[0]
    assert hb.status == "error"
    assert hb.last_error is not None and hb.last_error.startswith("blocked by limits:")


def test_blocked_heartbeat_failure_is_swallowed() -> None:
    rejection = ApiValidationError(
        "POST /v1/stream-sessions/start -> 403, code=limit_exceeded.max_cameras: nope"
    )
    client = PrecheckApiClient(limits=_tight_limits(), start_session_raises=rejection)
    client.heartbeat_raises = ApiConnectionError("heartbeat endpoint down")
    factories = _make_factories(client=client)

    # The original limit rejection (not the heartbeat failure) propagates.
    with pytest.raises(ApiValidationError) as exc:
        run_agent(
            _cfg(),
            _state(),
            camera_factory=factories["camera_factory"],
            client_factory=factories["client_factory"],
            ffmpeg_factory=factories["ffmpeg_factory"],
            ffmpeg_probe=_happy_probe(),
            max_frames=2,
        )
    assert "limit_exceeded.max_cameras" in str(exc.value)
