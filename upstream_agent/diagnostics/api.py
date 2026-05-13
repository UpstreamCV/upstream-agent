"""API-check diagnostic.

:func:`run_api_check` walks the confirmed backend flow:

1. ``client.register_agent`` — sends a non-sensitive generated agent
   name (``upstream-agent-<short_device_id>``), the package version,
   OS, Python version, and a small ``capabilities`` map. Hostname is
   **not** sent.
2. ``client.register_camera`` — sends the camera fields from config
   plus stable ``external_id``/``device_path`` derived from the
   source. Idempotent on ``(agent_id, external_id)``.
3. ``client.start_stream_session`` — confirms the server will approve
   a stream for this agent + camera.
4. ``client.stop_stream_session`` — cleanup. The check is a
   diagnostic, not a streaming launch: leaving the session open
   would block the next run with ``camera_already_streaming``.
   Failures here are **non-fatal** — registration and session
   approval already proved the path works. The error message goes
   into ``result.stop_warning``; ``result.success`` stays ``True``.

After each step that updates :class:`State`, the orchestrator calls
the optional ``save_progress`` callback so partial progress survives
a later failure (the agent never re-registers what already succeeded
on retry).

The function never raises. :class:`~upstream_agent.errors.ApiError`
subclasses from steps 1–3 are folded into ``result.error`` /
``result.failed_step``; an :class:`ApiError` from step 4 is folded
into ``result.stop_warning``.

**Sensitive-data handling:** the orchestrator reads
``session_resp.srt_url`` and ``session_resp.session.srt_url`` only to
set a boolean. The raw URL, ``streamid``, and ``session_token``
**never** appear on the result object, in logs, or in any test
artifact. The session_token JWT is treated as opaque bytes — not
parsed.
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
    RequestedStreamConfig,
    StreamSessionRequest,
)
from upstream_agent.config import ApiConfig, CameraConfig, Config
from upstream_agent.errors import ApiError
from upstream_agent.state import State
from upstream_agent.version import __version__

FailedStep = Literal["register_agent", "register_camera", "start_stream_session"]

# Hard-coded camera/stream defaults sent to the backend. The agent
# does not yet expose these as config fields; future milestones can
# add `camera.codec` / `camera.bitrate_kbps` overrides without
# changing the client.
_DEFAULT_CODEC: str = "h264"
_DEFAULT_BITRATE_KBPS: int = 3000

# Honest static capabilities. We have neither FFmpeg nor SRT
# transport implemented yet (M3 is registration-only). Update when
# those features actually land in a later milestone.
_AGENT_CAPABILITIES: dict[str, bool] = {"ffmpeg": False, "srt": False}


@dataclass(frozen=True)
class ApiCheckResult:
    """Structured outcome of an API probe.

    Successful runs carry the three IDs and the six approved-config
    fields. Failed runs (steps 1–3) carry the failing step name plus
    a human-readable error string. The SRT URL itself is **never** on
    this object — only the boolean ``srt_url_present``.

    ``stop_warning`` is the only field on a *successful* result that
    can carry a non-``None`` value: it holds the human error string
    from a non-fatal cleanup-stop failure. ``success`` stays ``True``
    in that case because registration and session approval already
    succeeded.
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
    approved_bitrate_kbps: int | None
    approved_latency_mode: str | None
    srt_url_present: bool
    error: str | None
    failed_step: FailedStep | None
    stop_warning: str | None


def _default_factory(cfg: ApiConfig) -> UpstreamApiClient:
    return UpstreamApiClient.from_config(cfg)


def _safe_os() -> str:
    """``platform.system()`` lowercased; falls back to ``unknown``."""

    try:
        value = platform_module.system()
    except Exception:
        return "unknown"
    return value.lower() if value else "unknown"


def _safe_python_version() -> str:
    try:
        return platform_module.python_version()
    except Exception:
        return "unknown"


def _agent_name_from(device_id: str) -> str:
    """Build a stable, non-sensitive agent name.

    Uses the first 8 hex characters of ``device_id`` (a UUID), with
    dashes stripped. Stable across runs; reveals nothing about the
    host machine.
    """

    short = device_id.replace("-", "")[:8] or "unknown"
    return f"upstream-agent-{short}"


def _camera_external_id(camera_cfg: CameraConfig) -> str:
    """Stable per-device idempotency key for the camera-register call.

    For a webcam at index 0 this is ``webcam-0``; running ``upstream
    check --api`` twice yields the same external_id, so the second
    call updates the existing camera row rather than creating a new
    one.
    """

    return f"{camera_cfg.source_type}-{camera_cfg.source}"


def _camera_device_path(camera_cfg: CameraConfig) -> str:
    """Stable, non-OS-specific device path string.

    For a webcam at index 0 this is ``webcam:0`` rather than the
    Linux-specific ``/dev/video0``. The backend uses this as a label.
    """

    return f"{camera_cfg.source_type}:{camera_cfg.source}"


def run_api_check(
    cfg: Config,
    state: State,
    *,
    client_factory: Callable[[ApiConfig], UpstreamApiClient] | None = None,
    save_progress: Callable[[State], None] | None = None,
) -> tuple[ApiCheckResult, State]:
    """Probe the API and return a structured result plus updated state.

    Never raises :class:`ApiError`; folds failures into
    ``result.error`` and ``result.failed_step``. Other exceptions
    (programmer bugs, KeyboardInterrupt, etc.) propagate.

    ``client_factory`` lets tests inject a fake client; ``save_progress``
    lets the CLI persist partial state immediately after each
    successful step so a later failure doesn't lose work.
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
                approved_bitrate_kbps=None,
                approved_latency_mode=None,
                srt_url_present=False,
                error=str(exc),
                failed_step=step,
                stop_warning=None,
            ),
            working_state,
        )

    with factory(cfg.api) as client:
        # Step 1: register agent.
        agent_req = AgentRegisterRequest(
            name=_agent_name_from(working_state.device_id),
            hostname=None,
            version=__version__,
            os=_safe_os(),
            python_version=_safe_python_version(),
            capabilities=_AGENT_CAPABILITIES,
        )
        try:
            agent_resp = client.register_agent(agent_req)
        except ApiError as exc:
            return _failure("register_agent", exc)

        working_state = working_state.model_copy(
            update={"agent_id": agent_resp.agent.id}
        )
        if save_progress is not None:
            save_progress(working_state)

        # Step 2: register camera.
        camera_req = CameraRegisterRequest(
            agent_id=agent_resp.agent.id,
            name=cfg.camera.name,
            external_id=_camera_external_id(cfg.camera),
            device_path=_camera_device_path(cfg.camera),
            default_width=cfg.camera.requested_width,
            default_height=cfg.camera.requested_height,
            default_fps=cfg.camera.requested_fps,
            default_codec=_DEFAULT_CODEC,
            default_bitrate_kbps=_DEFAULT_BITRATE_KBPS,
            default_latency_mode=cfg.stream.latency_mode,
            metadata={},
        )
        try:
            camera_resp = client.register_camera(camera_req)
        except ApiError as exc:
            return _failure("register_camera", exc)

        working_state = working_state.model_copy(
            update={"last_camera_id": camera_resp.camera.id}
        )
        if save_progress is not None:
            save_progress(working_state)

        # Step 3: start stream session.
        session_req = StreamSessionRequest(
            camera_id=camera_resp.camera.id,
            requested=RequestedStreamConfig(
                width=cfg.camera.requested_width,
                height=cfg.camera.requested_height,
                fps=cfg.camera.requested_fps,
                codec=_DEFAULT_CODEC,
                bitrate_kbps=_DEFAULT_BITRATE_KBPS,
                latency_mode=cfg.stream.latency_mode,
            ),
        )
        try:
            session_resp = client.start_stream_session(session_req)
        except ApiError as exc:
            return _failure("start_stream_session", exc)

        # Step 4 (cleanup): release the session so a subsequent
        # check (or any other start) is not blocked by
        # `camera_already_streaming`. Best-effort — registration and
        # session approval already succeeded; a stop failure is a
        # warning, not a check failure.
        stop_warning: str | None = None
        try:
            client.stop_stream_session(session_resp.session.id)
        except ApiError as exc:
            stop_warning = str(exc)

    approved = session_resp.session.approved
    # srt_url may appear either at the top level or inside `session`;
    # either presence counts. The orchestrator never stores the value.
    srt_url_present = bool(session_resp.srt_url or session_resp.session.srt_url)

    return (
        ApiCheckResult(
            success=True,
            base_url=cfg.api.base_url,
            agent_id=agent_resp.agent.id,
            camera_id=camera_resp.camera.id,
            session_id=session_resp.session.id,
            approved_codec=approved.codec if approved else None,
            approved_width=approved.width if approved else None,
            approved_height=approved.height if approved else None,
            approved_fps=approved.fps if approved else None,
            approved_bitrate_kbps=approved.bitrate_kbps if approved else None,
            approved_latency_mode=approved.latency_mode if approved else None,
            srt_url_present=srt_url_present,
            error=None,
            failed_step=None,
            stop_warning=stop_warning,
        ),
        working_state,
    )
