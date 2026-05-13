"""Streaming preflight diagnostic.

:func:`run_streaming_check` is the orchestrator behind
``upstream check --streaming``. It answers a single question — *is
this machine ready to stream video later?* — without actually
streaming. The phases run in this order:

1. **FFmpeg probe.** Calls :func:`probe_ffmpeg` (or uses an injected
   result) to learn the FFmpeg version, SRT-protocol support, and
   the first available H264 encoder.
2. **SRT check.** If FFmpeg does not list ``srt`` as a protocol, the
   check fails with an install hint.
3. **H264 check.** If no recognised H264 encoder is present, the
   check fails.
4. **API approval.** Calls the shared
   :func:`_obtain_stream_approval` helper from
   :mod:`upstream_agent.diagnostics.api`. The capabilities sent to
   ``register_agent`` are computed from the FFmpeg probe so the
   backend gets an honest picture of this machine.
5. **SRT URL parse.** :func:`parse_srt_url` validates the approved
   SRT URL. The raw URL stays in a local variable inside this
   function and is dropped before returning. Only safe metadata
   (``srt_url_valid: bool``) crosses out.
6. **Command build.** :func:`build_ffmpeg_command` constructs the
   argv list M5 will execute. The list itself is discarded after
   construction — M4 only verifies it builds without error.
7. **Stop session.** Best-effort cleanup so the check is repeatable.

The function never raises. All failures are folded into the
:class:`StreamingCheckResult`. The raw SRT URL, the built command,
the session token, and the streamid never appear on the result, in
logs, or in any test artifact.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from upstream_agent.api.client import UpstreamApiClient
from upstream_agent.config import ApiConfig, Config
from upstream_agent.diagnostics import api as api_module
from upstream_agent.diagnostics.api import (
    StreamApprovalOutcome,
    _obtain_stream_approval,
)
from upstream_agent.diagnostics.ffmpeg_check import FfmpegProbeResult, probe_ffmpeg
from upstream_agent.encoder.command_builder import build_ffmpeg_command
from upstream_agent.errors import ApiError, CommandBuildError, SrtUrlError
from upstream_agent.state import State
from upstream_agent.transport.srt import parse_srt_url

StreamingFailedStep = Literal[
    "ffmpeg",
    "srt_support",
    "h264_encoder",
    "register_agent",
    "register_camera",
    "start_stream_session",
    "srt_url_parse",
    "command_build",
]


@dataclass(frozen=True)
class StreamingCheckResult:
    """Structured outcome of a streaming preflight.

    Every field is safe to log or display. The raw SRT URL, the
    built FFmpeg command, the streamid, and the session token are
    deliberately absent — they are constructed and dropped inside
    :func:`run_streaming_check`.
    """

    success: bool
    # FFmpeg probe
    ffmpeg_available: bool
    ffmpeg_version: str | None
    srt_supported: bool
    h264_encoder: str | None
    # API approval (mirrors ApiCheckResult)
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
    srt_url_valid: bool
    command_build_success: bool
    # Failure context
    error: str | None
    failed_step: StreamingFailedStep | None
    stop_warning: str | None


def _capabilities_from(probe: FfmpegProbeResult) -> dict[str, bool | str]:
    return {
        "ffmpeg": probe.version if probe.version else False,
        "srt": bool(probe.srt_supported),
    }


def _ffmpeg_unavailable_message(probe: FfmpegProbeResult) -> str:
    if probe.error:
        return probe.error
    return "FFmpeg is not installed or not on PATH."


def _result_after_local_failure(
    *,
    probe: FfmpegProbeResult,
    base_url: str,
    failed_step: StreamingFailedStep,
    error: str,
) -> StreamingCheckResult:
    """Build a failure result for a phase that runs before any API call."""

    del base_url  # not surfaced on the result, but kept symmetrical for future use
    return StreamingCheckResult(
        success=False,
        ffmpeg_available=probe.available,
        ffmpeg_version=probe.version,
        srt_supported=probe.srt_supported,
        h264_encoder=probe.h264_encoder,
        agent_id=None,
        camera_id=None,
        session_id=None,
        approved_codec=None,
        approved_width=None,
        approved_height=None,
        approved_fps=None,
        approved_bitrate_kbps=None,
        approved_latency_mode=None,
        srt_url_present=False,
        srt_url_valid=False,
        command_build_success=False,
        error=error,
        failed_step=failed_step,
        stop_warning=None,
    )


def _result_after_api_failure(
    *,
    probe: FfmpegProbeResult,
    outcome: StreamApprovalOutcome,
) -> StreamingCheckResult:
    """Build a failure result for an API-layer failure."""

    assert outcome.failed_step is not None
    return StreamingCheckResult(
        success=False,
        ffmpeg_available=probe.available,
        ffmpeg_version=probe.version,
        srt_supported=probe.srt_supported,
        h264_encoder=probe.h264_encoder,
        agent_id=outcome.agent_id,
        camera_id=outcome.camera_id,
        session_id=None,
        approved_codec=None,
        approved_width=None,
        approved_height=None,
        approved_fps=None,
        approved_bitrate_kbps=None,
        approved_latency_mode=None,
        srt_url_present=False,
        srt_url_valid=False,
        command_build_success=False,
        error=outcome.error,
        failed_step=outcome.failed_step,
        stop_warning=None,
    )


def run_streaming_check(  # noqa: PLR0912, PLR0915  (clear linear flow)
    cfg: Config,
    state: State,
    *,
    client_factory: Callable[[ApiConfig], UpstreamApiClient] | None = None,
    save_progress: Callable[[State], None] | None = None,
    ffmpeg_probe: FfmpegProbeResult | None = None,
) -> tuple[StreamingCheckResult, State]:
    """Run the eight-phase streaming preflight. Never raises.

    ``ffmpeg_probe`` lets tests (and any caller that already probed)
    skip the subprocess. ``client_factory`` and ``save_progress``
    match :func:`run_api_check`.
    """

    # Reference api_module._default_factory at call time so tests that
    # monkeypatch the factory on the api module pick up correctly here.
    factory = client_factory or api_module._default_factory
    probe = ffmpeg_probe if ffmpeg_probe is not None else probe_ffmpeg()

    # Phase 1: FFmpeg available?
    if not probe.available:
        return (
            _result_after_local_failure(
                probe=probe,
                base_url=cfg.api.base_url,
                failed_step="ffmpeg",
                error=_ffmpeg_unavailable_message(probe),
            ),
            state,
        )

    # Phase 2: SRT support?
    if not probe.srt_supported:
        return (
            _result_after_local_failure(
                probe=probe,
                base_url=cfg.api.base_url,
                failed_step="srt_support",
                error=(
                    "FFmpeg is installed, but this build does not support SRT. "
                    "Install an FFmpeg build with libsrt enabled."
                ),
            ),
            state,
        )

    # Phase 3: H264 encoder?
    if probe.h264_encoder is None:
        return (
            _result_after_local_failure(
                probe=probe,
                base_url=cfg.api.base_url,
                failed_step="h264_encoder",
                error=(
                    "FFmpeg is installed but no usable H264 encoder is available. "
                    "Install an FFmpeg build with libx264 or a platform H264 encoder."
                ),
            ),
            state,
        )

    capabilities = _capabilities_from(probe)
    working_state = state

    # Phases 4–6 share an open client (the API context manager).
    with factory(cfg.api) as client:
        outcome, working_state = _obtain_stream_approval(
            cfg, working_state, client, capabilities, save_progress
        )

        if outcome.failed_step is not None or outcome.session_response is None:
            return _result_after_api_failure(probe=probe, outcome=outcome), working_state

        session_resp = outcome.session_response
        session_id = session_resp.session.id
        approved = session_resp.session.approved
        # The raw SRT URL lives only in this local variable.
        raw_srt_url: str | None = (
            session_resp.srt_url or session_resp.session.srt_url
        )
        srt_url_present = bool(raw_srt_url)

        # Phase 5: SRT URL parse.
        if not raw_srt_url:
            error_msg = "Stream session was approved but no SRT URL was returned."
            stop_warning = _safe_stop(client, session_id)
            return (
                StreamingCheckResult(
                    success=False,
                    ffmpeg_available=True,
                    ffmpeg_version=probe.version,
                    srt_supported=True,
                    h264_encoder=probe.h264_encoder,
                    agent_id=outcome.agent_id,
                    camera_id=outcome.camera_id,
                    session_id=session_id,
                    approved_codec=approved.codec if approved else None,
                    approved_width=approved.width if approved else None,
                    approved_height=approved.height if approved else None,
                    approved_fps=approved.fps if approved else None,
                    approved_bitrate_kbps=approved.bitrate_kbps if approved else None,
                    approved_latency_mode=approved.latency_mode if approved else None,
                    srt_url_present=False,
                    srt_url_valid=False,
                    command_build_success=False,
                    error=error_msg,
                    failed_step="srt_url_parse",
                    stop_warning=stop_warning,
                ),
                working_state,
            )

        try:
            parse_srt_url(raw_srt_url)
        except SrtUrlError as exc:
            stop_warning = _safe_stop(client, session_id)
            return (
                StreamingCheckResult(
                    success=False,
                    ffmpeg_available=True,
                    ffmpeg_version=probe.version,
                    srt_supported=True,
                    h264_encoder=probe.h264_encoder,
                    agent_id=outcome.agent_id,
                    camera_id=outcome.camera_id,
                    session_id=session_id,
                    approved_codec=approved.codec if approved else None,
                    approved_width=approved.width if approved else None,
                    approved_height=approved.height if approved else None,
                    approved_fps=approved.fps if approved else None,
                    approved_bitrate_kbps=approved.bitrate_kbps if approved else None,
                    approved_latency_mode=approved.latency_mode if approved else None,
                    srt_url_present=True,
                    srt_url_valid=False,
                    command_build_success=False,
                    error=f"Approved SRT URL is invalid: {exc}",
                    failed_step="srt_url_parse",
                    stop_warning=stop_warning,
                ),
                working_state,
            )

        # Phase 6: command build. The list is discarded right after
        # construction — M4 only proves it builds without error.
        command_build_success = False
        command_error: str | None = None
        if approved is None:
            command_error = (
                "Server returned no approved config; cannot build FFmpeg command."
            )
        else:
            try:
                _ = build_ffmpeg_command(
                    width=approved.width,
                    height=approved.height,
                    fps=approved.fps,
                    bitrate_kbps=approved.bitrate_kbps,
                    codec=approved.codec,
                    encoder=probe.h264_encoder,
                    latency_mode=approved.latency_mode,
                    srt_url=raw_srt_url,
                )
                command_build_success = True
            except CommandBuildError as exc:
                command_error = str(exc)

        if not command_build_success:
            stop_warning = _safe_stop(client, session_id)
            return (
                StreamingCheckResult(
                    success=False,
                    ffmpeg_available=True,
                    ffmpeg_version=probe.version,
                    srt_supported=True,
                    h264_encoder=probe.h264_encoder,
                    agent_id=outcome.agent_id,
                    camera_id=outcome.camera_id,
                    session_id=session_id,
                    approved_codec=approved.codec if approved else None,
                    approved_width=approved.width if approved else None,
                    approved_height=approved.height if approved else None,
                    approved_fps=approved.fps if approved else None,
                    approved_bitrate_kbps=approved.bitrate_kbps if approved else None,
                    approved_latency_mode=approved.latency_mode if approved else None,
                    srt_url_present=True,
                    srt_url_valid=True,
                    command_build_success=False,
                    error=command_error,
                    failed_step="command_build",
                    stop_warning=stop_warning,
                ),
                working_state,
            )

        # Phase 7: best-effort stop.
        stop_warning = _safe_stop(client, session_id)

    # Drop the local SRT URL reference explicitly. (The local variable
    # falls out of scope at function return anyway, but being explicit
    # documents the intent.)
    raw_srt_url = None
    del raw_srt_url

    return (
        StreamingCheckResult(
            success=True,
            ffmpeg_available=True,
            ffmpeg_version=probe.version,
            srt_supported=True,
            h264_encoder=probe.h264_encoder,
            agent_id=outcome.agent_id,
            camera_id=outcome.camera_id,
            session_id=session_id,
            approved_codec=approved.codec if approved else None,
            approved_width=approved.width if approved else None,
            approved_height=approved.height if approved else None,
            approved_fps=approved.fps if approved else None,
            approved_bitrate_kbps=approved.bitrate_kbps if approved else None,
            approved_latency_mode=approved.latency_mode if approved else None,
            srt_url_present=srt_url_present,
            srt_url_valid=True,
            command_build_success=True,
            error=None,
            failed_step=None,
            stop_warning=stop_warning,
        ),
        working_state,
    )


def _safe_stop(client: UpstreamApiClient, session_id: str) -> str | None:
    """Stop a session and fold any :class:`ApiError` into a warning string.

    Mirrors the behaviour in :func:`run_api_check`: the stop is
    best-effort and a failure here never flips ``success`` to False.
    """

    try:
        client.stop_stream_session(session_id)
    except ApiError as exc:
        return str(exc)
    return None
