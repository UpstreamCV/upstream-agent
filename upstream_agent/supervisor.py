"""Top-level runtime for ``upstream run``.

:func:`run_agent` orchestrates the live streaming path: FFmpeg probe →
camera open → API session approval → command build → FFmpeg start →
frame loop → cleanup. Every cleanup path (success, FFmpeg start
failure, mid-stream FFmpeg exit, camera read failure, Ctrl+C) stops
the FFmpeg subprocess, releases the camera, and stops the API
session, in that order.

This is the *Milestone 5 prep* runtime. Real end-to-end smoke against
MediaMTX is still gated on the API server returning an SRT URL whose
host/port reaches the running MediaMTX. To unblock local testing in
the meantime, the supervisor honours an env-only dev override
(:data:`OVERRIDE_ENV_NAME`); see ``docs/troubleshooting.md``.

Sensitive-data discipline:

* The raw SRT URL stays in a single local variable inside
  :func:`run_agent`. It never appears on :class:`RunResult`, in
  :func:`on_event` messages, or in log records.
* The FFmpeg command list (which contains the URL) is passed
  straight into :class:`FfmpegEncoderProcess`, alongside a redacted
  copy that is the only argv the wrapper exposes via ``__repr__``
  or error messages.
* The dev override warning is a fixed string that contains no URL.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from upstream_agent.api.client import UpstreamApiClient
from upstream_agent.config import ApiConfig, CameraConfig, Config
from upstream_agent.diagnostics import api as api_module
from upstream_agent.diagnostics.api import _obtain_stream_approval
from upstream_agent.diagnostics.ffmpeg_check import FfmpegProbeResult, probe_ffmpeg
from upstream_agent.encoder.command_builder import build_ffmpeg_command, redact_command
from upstream_agent.encoder.ffmpeg import FfmpegEncoderProcess
from upstream_agent.errors import (
    ApiError,
    FfmpegExitError,
    FfmpegH264UnsupportedError,
    FfmpegNotFoundError,
    FfmpegSrtUnsupportedError,
)
from upstream_agent.sources.base import VideoSource
from upstream_agent.sources.webcam import WebcamSource
from upstream_agent.transport.srt import parse_srt_url

logger = logging.getLogger(__name__)


# Dev override: see docs/troubleshooting.md. Temporary; will be
# removed once `upstream-api-server` returns a correct local/dev
# ingest host/port.
OVERRIDE_ENV_NAME: str = "UPSTREAM_DEV_SRT_URL_OVERRIDE"
_OVERRIDE_WARNING: str = (
    "UPSTREAM_DEV_SRT_URL_OVERRIDE is set; substituting dev SRT URL for the "
    "API-issued URL. The real API session is still started and stopped."
)


ExitReason = Literal[
    "max_frames",
    "keyboard_interrupt",
    "ffmpeg_exited",
    "completed",
]


CameraFactory = Callable[[CameraConfig], VideoSource]
FfmpegFactory = Callable[[list[str], list[str]], FfmpegEncoderProcess]
EventCallback = Callable[[str, str], None]


@dataclass(frozen=True)
class RunResult:
    """Outcome of a :func:`run_agent` call.

    ``frames_written`` is the count of frames successfully piped into
    FFmpeg before the loop exited. ``exit_reason`` is the categorical
    reason the loop ended; an error path (FFmpeg start failure,
    camera read failure, etc.) propagates the exception rather than
    setting ``exit_reason``.
    """

    frames_written: int
    exit_reason: ExitReason


def _default_camera_factory(cfg: CameraConfig) -> VideoSource:
    return WebcamSource(cfg)


def _default_ffmpeg_factory(
    command: list[str], redacted_command: list[str]
) -> FfmpegEncoderProcess:
    return FfmpegEncoderProcess(command, redacted_command)


def _capabilities_from(probe: FfmpegProbeResult) -> dict[str, bool | str]:
    return {
        "ffmpeg": probe.version if probe.version else False,
        "srt": bool(probe.srt_supported),
    }


def _safe_stop_session(client: UpstreamApiClient, session_id: str | None) -> str | None:
    """Best-effort ``stop_stream_session`` that never raises."""

    if session_id is None:
        return None
    try:
        client.stop_stream_session(session_id)
    except ApiError as exc:
        return str(exc)
    except Exception as exc:  # pragma: no cover - defensive
        return f"{exc.__class__.__name__}: {exc}"
    return None


def _emit(on_event: EventCallback | None, stage: str, message: str) -> None:
    if on_event is None:
        return
    try:
        on_event(stage, message)
    except Exception:  # pragma: no cover - never let the CLI hook break us
        logger.exception("on_event callback raised; continuing.")


def _maybe_resize(frame: Any, target_width: int, target_height: int) -> Any:
    """If ``frame`` is not (target_height, target_width, 3), resize it.

    Imports cv2 lazily — only invoked when a resize is actually
    needed, so machines without OpenCV still pass the no-resize fast
    path (which shouldn't happen in practice since :class:`WebcamSource`
    requires cv2 anyway, but the supervisor stays correct even with
    other future sources).
    """

    shape = getattr(frame, "shape", None)
    if shape is not None and len(shape) >= 2:
        height, width = shape[0], shape[1]
        if height == target_height and width == target_width:
            return frame

    # Lazy cv2 import. Reuse WebcamSource's helper so a missing OpenCV
    # install becomes the same typed error users already know.
    from upstream_agent.sources.webcam import _import_cv2  # noqa: PLC0415

    cv2 = _import_cv2()
    resized = cv2.resize(frame, (target_width, target_height))

    # ``cv2.resize`` typically returns a contiguous ndarray, but some
    # cv2 builds / dtypes don't. Force contiguity so ``tobytes`` is
    # correct.
    ascontig = getattr(resized, "tobytes", None)
    if ascontig is None:
        return resized
    # Only import numpy if available; if not, trust tobytes.
    try:
        import numpy as np  # noqa: PLC0415

        if not resized.flags["C_CONTIGUOUS"]:
            return np.ascontiguousarray(resized)
    except Exception:  # pragma: no cover - numpy is a cv2 dep, but stay safe
        pass
    return resized


def _frame_to_bytes(frame: Any) -> bytes:
    """Convert a frame to raw bytes for FFmpeg's rawvideo stdin.

    Accepts anything that exposes ``tobytes()`` (numpy ndarray) or
    is already ``bytes``/``bytearray``.
    """

    if isinstance(frame, (bytes, bytearray)):
        return bytes(frame)
    tobytes = getattr(frame, "tobytes", None)
    if tobytes is not None:
        return tobytes()
    raise TypeError(
        f"Frame type {type(frame).__name__} cannot be converted to bytes."
    )


def _apply_dev_override(api_srt_url: str | None) -> str | None:
    """Honour ``UPSTREAM_DEV_SRT_URL_OVERRIDE``; return the URL to use.

    If the override is unset/empty, returns ``api_srt_url`` unchanged.
    If the override is set, validates it via :func:`parse_srt_url`
    (which raises :class:`SrtUrlError` cleanly on a bad value, never
    echoing the URL), logs the fixed warning, and returns the
    override.
    """

    override = os.environ.get(OVERRIDE_ENV_NAME, "")
    if not override:
        return api_srt_url
    # Validates scheme/host/port; raises SrtUrlError on bad input. The
    # exception message is generic and contains no URL value.
    parse_srt_url(override)
    logger.warning(_OVERRIDE_WARNING)
    return override


def run_agent(  # noqa: PLR0912, PLR0915  (linear flow with explicit cleanup)
    cfg: Config,
    state: Any,
    *,
    client_factory: Callable[[ApiConfig], UpstreamApiClient] | None = None,
    camera_factory: CameraFactory | None = None,
    ffmpeg_factory: FfmpegFactory | None = None,
    ffmpeg_probe: FfmpegProbeResult | None = None,
    save_progress: Callable[[Any], None] | None = None,
    max_frames: int | None = None,
    on_event: EventCallback | None = None,
) -> tuple[RunResult, Any]:
    """Run the live streaming loop. Never returns while streaming.

    The CLI's ``upstream run`` calls this. Tests inject the four
    factory hooks and ``max_frames`` to drive a deterministic short
    loop without touching real subprocesses, cameras, or HTTP.

    Cleanup is performed in this order regardless of how the function
    exits: FFmpeg → camera → API session. The function re-raises any
    in-flight exception after cleanup runs.
    """

    api_factory = client_factory or api_module._default_factory
    cam_factory = camera_factory or _default_camera_factory
    ff_factory = ffmpeg_factory or _default_ffmpeg_factory

    # Phase 1: FFmpeg probe.
    probe = ffmpeg_probe if ffmpeg_probe is not None else probe_ffmpeg()
    if not probe.available:
        raise FfmpegNotFoundError(
            probe.error or "FFmpeg is not installed or not on PATH."
        )
    if not probe.srt_supported:
        raise FfmpegSrtUnsupportedError(
            "FFmpeg is installed, but this build does not support SRT. "
            "Install an FFmpeg build with libsrt enabled."
        )
    if probe.h264_encoder is None:
        raise FfmpegH264UnsupportedError(
            "FFmpeg is installed but no usable H264 encoder is available."
        )
    capabilities = _capabilities_from(probe)

    # State containers for cleanup. These are set as we progress; the
    # finally block uses None checks to know what to tear down.
    cam: VideoSource | None = None
    ffmpeg: FfmpegEncoderProcess | None = None
    client: UpstreamApiClient | None = None
    session_id: str | None = None
    frames_written: int = 0
    exit_reason: ExitReason = "completed"
    working_state = state
    client_ctx_active = False

    try:
        # Phase 2: open camera.
        cam = cam_factory(cfg.camera)
        cam.open()
        _emit(
            on_event,
            "camera_opened",
            f"Camera opened: {cfg.camera.source_type} {cfg.camera.source}",
        )

        # Phase 3: API client + session approval.
        client = api_factory(cfg.api)
        client.__enter__()
        client_ctx_active = True

        outcome, working_state = _obtain_stream_approval(
            cfg, working_state, client, capabilities, save_progress
        )
        if outcome.failed_step is not None or outcome.session_response is None:
            # Re-raise with the same shape the diagnostics use: a typed
            # ApiError isn't preserved here (only the string was), so
            # surface a generic StreamingRuntimeError? Better: surface
            # the original ApiError shape by re-raising from outcome.error.
            from upstream_agent.errors import StreamingRuntimeError  # noqa: PLC0415

            raise StreamingRuntimeError(
                f"API approval failed at {outcome.failed_step}: {outcome.error}"
            )

        session_resp = outcome.session_response
        session_id = session_resp.session.id
        approved = session_resp.session.approved
        if approved is None:
            from upstream_agent.errors import StreamingRuntimeError  # noqa: PLC0415

            raise StreamingRuntimeError(
                "API approved the session but returned no approved config."
            )

        api_srt_url: str | None = (
            session_resp.srt_url or session_resp.session.srt_url
        )
        srt_url = _apply_dev_override(api_srt_url)
        if not srt_url:
            from upstream_agent.errors import StreamingRuntimeError  # noqa: PLC0415

            raise StreamingRuntimeError(
                "API approved the session but no SRT URL was returned."
            )

        _emit(
            on_event,
            "api_approved",
            "API session approved",
        )
        _emit(
            on_event,
            "approved_config",
            (
                f"Approved config: {approved.codec}, "
                f"{approved.width}x{approved.height} @ {approved.fps} FPS, "
                f"{approved.bitrate_kbps} kbps, {approved.latency_mode}"
            ),
        )

        # Phase 4: command build (raw + redacted forms stay local).
        cmd = build_ffmpeg_command(
            width=approved.width,
            height=approved.height,
            fps=approved.fps,
            bitrate_kbps=approved.bitrate_kbps,
            codec=approved.codec,
            encoder=probe.h264_encoder,
            latency_mode=approved.latency_mode,
            srt_url=srt_url,
        )
        redacted = redact_command(cmd, srt_url)

        # Phase 5: FFmpeg start.
        ffmpeg = ff_factory(cmd, redacted)
        ffmpeg.start()
        _emit(on_event, "ffmpeg_started", "FFmpeg started")

        # Phase 6: frame loop.
        _emit(
            on_event,
            "streaming",
            "Streaming started\nPress Ctrl+C to stop.",
        )
        target_w = approved.width
        target_h = approved.height
        while True:
            ok, frame = cam.read_frame()
            if not ok or frame is None:
                # WebcamSource raises CameraReadError on failure; some
                # other future source might return (False, None). Treat
                # that as end-of-stream, not an error.
                exit_reason = "completed"
                break

            sized = _maybe_resize(frame, target_w, target_h)
            ffmpeg.write_frame(_frame_to_bytes(sized))
            frames_written += 1

            ffmpeg_exit = ffmpeg.poll()
            if ffmpeg_exit is not None:
                raise FfmpegExitError(
                    f"FFmpeg exited mid-stream with code {ffmpeg_exit}."
                )

            if max_frames is not None and frames_written >= max_frames:
                exit_reason = "max_frames"
                break

    except KeyboardInterrupt:
        exit_reason = "keyboard_interrupt"
        _emit(on_event, "shutdown_requested", "Stopping stream...")
        # Run cleanup, then re-raise so the CLI sees the interrupt.
        _cleanup(ffmpeg, cam, client, session_id, client_ctx_active, on_event)
        raise

    except FfmpegExitError:
        exit_reason = "ffmpeg_exited"
        _cleanup(ffmpeg, cam, client, session_id, client_ctx_active, on_event)
        raise

    except Exception:
        _cleanup(ffmpeg, cam, client, session_id, client_ctx_active, on_event)
        raise

    # Normal exit (break out of the loop): run cleanup once.
    _cleanup(ffmpeg, cam, client, session_id, client_ctx_active, on_event)
    return RunResult(frames_written=frames_written, exit_reason=exit_reason), working_state


def _cleanup(
    ffmpeg: FfmpegEncoderProcess | None,
    cam: VideoSource | None,
    client: UpstreamApiClient | None,
    session_id: str | None,
    client_ctx_active: bool,
    on_event: EventCallback | None,
) -> None:
    """Tear down in FFmpeg → camera → session order. Never raises."""

    if ffmpeg is not None:
        try:
            ffmpeg.stop(timeout_seconds=5.0)
        except Exception:  # pragma: no cover - stop should not raise
            logger.exception("ffmpeg.stop raised; continuing cleanup.")
        _emit(on_event, "ffmpeg_stopped", "FFmpeg stopped")

    if cam is not None:
        try:
            cam.close()
        except Exception:  # pragma: no cover
            logger.exception("camera.close raised; continuing cleanup.")
        _emit(on_event, "camera_released", "Camera released")

    if client is not None and session_id is not None:
        warning = _safe_stop_session(client, session_id)
        if warning is None:
            _emit(on_event, "session_stopped", "Stream session stopped")
        else:
            _emit(
                on_event,
                "session_stop_warning",
                f"Warning: failed to stop stream session: {warning}",
            )

    if client is not None and client_ctx_active:
        try:
            client.__exit__(None, None, None)
        except Exception:  # pragma: no cover
            logger.exception("client __exit__ raised; ignoring.")
