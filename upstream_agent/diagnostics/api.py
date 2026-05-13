"""API-check diagnostic.

:func:`run_api_check` walks the three-step backend flow:

1. ``client.register_agent`` — uses local ``device_id`` and agent
   version.
2. ``client.register_camera`` — uses the just-returned ``agent_id``
   plus camera fields from config.
3. ``client.start_stream_session`` — confirms the server will approve
   a stream for this agent + camera.

After each step that updates :class:`State`, the orchestrator calls the
optional ``save_progress`` callback so partial progress survives a
later failure (the agent never needs to re-register on retry).

The function never raises. :class:`~upstream_agent.errors.ApiError`
subclasses are folded into a structured :class:`ApiCheckResult`. The
CLI translates the result to stdout / stderr / exit code.

**SRT URL handling:** the orchestrator drops the URL after extracting
``srt_url_present = bool(...)``. The full URL never appears on the
result object, in logs, or in any test artefact.
"""

from __future__ import annotations

import platform as platform_module
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from upstream_agent.api.client import UpstreamApiClient
from upstream_agent.api.schemas import (
    AgentRegisterRequest,
    CameraRegisterRequest,
    StreamSessionRequest,
)
from upstream_agent.config import ApiConfig, Config
from upstream_agent.errors import ApiError
from upstream_agent.state import State
from upstream_agent.version import __version__

FailedStep = Literal["register_agent", "register_camera", "start_stream_session"]


@dataclass(frozen=True)
class ApiCheckResult:
    """Structured outcome of an API probe.

    Successful runs carry the three IDs and the approved-config
    summary. Failed runs carry the failing step name plus a
    human-readable error string. The SRT URL itself is **never** on
    this object — only the boolean ``srt_url_present``.
    """

    success: bool
    base_url: str
    agent_id: str | None
    camera_id: str | None
    session_id: str | None
    approved_codec: str | None
    approved_width: int | None
    approved_height: int | None
    approved_fps: int | None
    srt_url_present: bool
    error: str | None
    failed_step: FailedStep | None


def _default_factory(cfg: ApiConfig) -> UpstreamApiClient:
    return UpstreamApiClient.from_config(cfg)


def _safe_platform() -> str | None:
    try:
        value = platform_module.system()
    except Exception:
        return None
    return value or None


def run_api_check(
    cfg: Config,
    state: State,
    *,
    client_factory: Callable[[ApiConfig], UpstreamApiClient] | None = None,
    save_progress: Callable[[State], None] | None = None,
) -> tuple[ApiCheckResult, State]:
    """Probe the API and return a structured result plus the updated state.

    Never raises :class:`ApiError`; folds failures into ``result.error``
    and ``result.failed_step``. Other exceptions (programmer bugs,
    KeyboardInterrupt, etc.) propagate.

    ``client_factory`` lets tests inject a fake client; ``save_progress``
    lets the CLI persist partial state immediately after each successful
    step so a later failure doesn't lose work.
    """

    factory = client_factory or _default_factory
    working_state = state

    def _failure(step: FailedStep, exc: ApiError) -> tuple[ApiCheckResult, State]:
        return (
            ApiCheckResult(
                success=False,
                base_url=cfg.api.base_url,
                agent_id=working_state.agent_id,
                camera_id=working_state.last_camera_id,
                session_id=None,
                approved_codec=None,
                approved_width=None,
                approved_height=None,
                approved_fps=None,
                srt_url_present=False,
                error=str(exc),
                failed_step=step,
            ),
            working_state,
        )

    with factory(cfg.api) as client:
        # Step 1: register agent.
        agent_req = AgentRegisterRequest(
            device_id=working_state.device_id,
            agent_version=__version__,
            platform=_safe_platform(),
        )
        try:
            agent_resp = client.register_agent(agent_req)
        except ApiError as exc:
            return _failure("register_agent", exc)

        working_state = working_state.model_copy(
            update={"agent_id": agent_resp.agent_id}
        )
        if save_progress is not None:
            save_progress(working_state)

        # Step 2: register camera.
        camera_req = CameraRegisterRequest(
            agent_id=agent_resp.agent_id,
            name=cfg.camera.name,
            source_type=cfg.camera.source_type,
            source=cfg.camera.source,
            requested_width=cfg.camera.requested_width,
            requested_height=cfg.camera.requested_height,
            requested_fps=cfg.camera.requested_fps,
        )
        try:
            camera_resp = client.register_camera(camera_req)
        except ApiError as exc:
            return _failure("register_camera", exc)

        working_state = working_state.model_copy(
            update={"last_camera_id": camera_resp.camera_id}
        )
        if save_progress is not None:
            save_progress(working_state)

        # Step 3: stream session.
        session_req = StreamSessionRequest(
            agent_id=agent_resp.agent_id,
            camera_id=camera_resp.camera_id,
            source_type=cfg.camera.source_type,
            requested_width=cfg.camera.requested_width,
            requested_height=cfg.camera.requested_height,
            requested_fps=cfg.camera.requested_fps,
        )
        try:
            session_resp = client.start_stream_session(session_req)
        except ApiError as exc:
            return _failure("start_stream_session", exc)

    approved = session_resp.approved_config
    srt_url_present = bool(approved.srt_url)
    # NB: approved.srt_url is intentionally not stored anywhere past
    # this point. It is dropped along with the response object.

    return (
        ApiCheckResult(
            success=True,
            base_url=cfg.api.base_url,
            agent_id=agent_resp.agent_id,
            camera_id=camera_resp.camera_id,
            session_id=session_resp.session_id,
            approved_codec=approved.codec,
            approved_width=approved.width,
            approved_height=approved.height,
            approved_fps=approved.fps,
            srt_url_present=srt_url_present,
            error=None,
            failed_step=None,
        ),
        working_state,
    )
