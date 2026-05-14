"""Top-level runtime for ``upstream run``.

:func:`run_agent` orchestrates the live streaming path with automatic
recovery on transient failures: FFmpeg probe → camera open → API
client open → recovery loop → outer cleanup. The recovery loop runs
one streaming attempt at a time via the private :func:`_run_attempt`
helper, which handles the per-session lifecycle: session start →
FFmpeg start → heartbeat start → frame loop → attempt-scoped cleanup
(heartbeat → FFmpeg → session). The end-to-end shutdown ordering is
heartbeat → FFmpeg → camera → session — unchanged from Milestone 6.

Milestone 7 — failure handling and reconnect:

* **Recoverable stream failures** (FFmpeg mid-stream exit / broken
  pipe, API connection / 5xx / 429 / contract errors, generic
  ``StreamingRuntimeError``, ``SrtUrlError``): the attempt is torn
  down and a fresh attempt is started after an interruptible backoff
  sleep. A new stream session is requested every attempt — the agent
  never reuses an SRT URL or session token across attempts.
* **Recoverable camera failures** (``CameraReadError`` mid-stream,
  ``CameraOpenError`` during reopen): the camera is closed and
  reopened with backoff, then a fresh stream attempt starts.
* **Fatal failures** (auth/token/validation errors, FFmpeg start /
  not-found / SRT-unsupported, command-build, config errors):
  cleanup runs and the exception propagates to the CLI.
* **Initial camera open failure** stays fatal — only mid-stream
  camera loss triggers the reopen loop.
* **Ctrl+C** is interruptible at every step: the frame loop, the
  backoff sleep, and the camera reopen backoff all wake immediately.
* **``max_attempts``** is a hidden test knob — pass it to bound the
  recovery loop so tests don't have to wait out a real outage. In
  production it is ``None`` (unbounded retries).

Sensitive-data discipline (unchanged from M5/M6):

* The raw SRT URL stays in a single local variable inside
  :func:`_run_attempt` for the lifetime of one attempt; it is dropped
  before the attempt-cleanup block runs.
* The FFmpeg command list (which contains the URL) is passed straight
  into :class:`FfmpegEncoderProcess` alongside a redacted copy that is
  the only argv the wrapper exposes via ``__repr__`` or error messages.
* Heartbeat payloads carry no SRT URL, streamid, or session token —
  only IDs and runtime telemetry.
* Event-callback messages never quote URLs, tokens, or streamids.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from upstream_agent.api.client import UpstreamApiClient
from upstream_agent.api.schemas import (
    AgentRegisterRequest,
    ApprovedStreamConfig,
    CameraRegisterRequest,
    RequestedStreamConfig,
    StreamSessionRequest,
    StreamSessionResponse,
)
from upstream_agent.config import ApiConfig, CameraConfig, Config
from upstream_agent.diagnostics import api as api_module
from upstream_agent.diagnostics.api import (
    _agent_name_from,
    _camera_device_path,
    _camera_external_id,
    _safe_os,
    _safe_python_version,
)
from upstream_agent.diagnostics.ffmpeg_check import FfmpegProbeResult, probe_ffmpeg
from upstream_agent.encoder.command_builder import build_ffmpeg_command, redact_command
from upstream_agent.encoder.ffmpeg import FfmpegEncoderProcess
from upstream_agent.errors import (
    ApiAuthError,
    ApiConnectionError,
    ApiContractError,
    ApiError,
    ApiRateLimitError,
    ApiServerError,
    ApiTokenExpiredError,
    ApiTokenRevokedError,
    ApiValidationError,
    CameraOpenError,
    CameraReadError,
    CommandBuildError,
    FfmpegExitError,
    FfmpegH264UnsupportedError,
    FfmpegNotFoundError,
    FfmpegRuntimeError,
    FfmpegSrtUnsupportedError,
    FfmpegStartError,
    FfmpegWriteError,
    SrtUrlError,
    StreamingRuntimeError,
)
from upstream_agent.heartbeat.reporter import HeartbeatReporter
from upstream_agent.runtime.backoff import BackoffPolicy
from upstream_agent.runtime.stats import RuntimeStats
from upstream_agent.sources.base import VideoSource
from upstream_agent.sources.webcam import WebcamSource
from upstream_agent.version import __version__

# Hard-coded camera/stream defaults sent to the backend — kept in sync
# with diagnostics/api.py so the supervisor and the API check send the
# same shape. The agent does not yet expose these as config fields.
_DEFAULT_CODEC: str = "h264"
_DEFAULT_BITRATE_KBPS: int = 3000

# Number of consecutive successful frame reads required to declare
# camera recovery. The live smoke test showed that one read is not
# enough — V4L2 drivers (and a few others) can return one stale
# buffered frame after ``open()`` and then refuse further reads
# until the device fully warms up. Three reads is the smallest
# value that reliably distinguishes "lucky buffered frame" from
# "device is producing live frames again."
_CAMERA_RECOVERY_VALIDATION_FRAMES: int = 3

# How long to wait after a successful ``open()`` before starting the
# validation reads. Some USB / Continuity cameras need a brief settle
# period before they produce frames; reading immediately and failing
# fast wastes a backoff cycle. The wait is interruptible by
# ``shutdown``.
_CAMERA_RECOVERY_SETTLE_S: float = 1.5

# Maximum wall-clock time the validator will spend trying to collect
# ``_CAMERA_RECOVERY_VALIDATION_FRAMES`` consecutive successful reads.
# Transient validation failures inside the window are tolerated; the
# validator only gives up when the deadline hits without enough good
# frames.
_CAMERA_RECOVERY_VALIDATION_WINDOW_S: float = 5.0

# Inter-attempt cooldown inside the validation window. Picks roughly
# one in every 8 reads at 30 fps; small enough that a 5 s window
# accommodates ~50 read attempts in production, large enough that we
# don't hammer the device on a tight retry loop.
_CAMERA_RECOVERY_READ_RETRY_S: float = 0.1

# How long to wait between ``close()`` and the next ``open()`` so the
# driver has a chance to release the device fully. Real USB / V4L2
# devices fail re-open if we hammer them too quickly.
_CAMERA_RELEASE_SETTLE_S: float = 0.5

# After this many consecutive camera-recovery failures (open succeeded
# but validation never produced a steady frame stream), the supervisor
# emits a single ``camera_unavailable_hint`` event with an honest
# suggestion to restart the agent. Recovery keeps trying — some USB
# cameras come back later — but the operator gets one clear nudge.
# Calibrated to fire roughly once when the macOS Continuity Camera
# (or any AVFoundation-cached device) has clearly given up:
# 3 cycles × (≈1.5 s settle + ≤5 s window + backoff) ≈ 25–60 s.
_CAMERA_UNAVAILABLE_HINT_AFTER: int = 3

# How long to wait for the API to confirm the session's remote ingest
# state is ``"streaming"`` after FFmpeg starts. Local FFmpeg startup
# alone is not proof of remote stream availability — MediaMTX's
# ``runOnReady`` webhook flips the API state once SRT publish is live,
# and only then is the dashboard able to serve the stream. The agent
# polls the GET /v1/stream-sessions/{id} endpoint for this state.
_INGEST_CONFIRM_TIMEOUT_S: float = 15.0
_INGEST_POLL_INTERVAL_S: float = 0.5


class _IngestSkip(BaseException):
    """Internal control-flow signal: shutdown fired during ingest wait.

    Inherits from :class:`BaseException` (not :class:`Exception`) so the
    supervisor's blanket ``except Exception`` doesn't catch it as a
    recoverable failure. ``_run_attempt``'s outer ``try`` catches it
    explicitly and treats it the same as the ``shutdown`` outcome.
    """

logger = logging.getLogger(__name__)


ExitReason = Literal[
    "max_frames",
    "keyboard_interrupt",
    "ffmpeg_exited",
    "fatal_heartbeat",
    "completed",
]


CameraFactory = Callable[[CameraConfig], VideoSource]
FfmpegFactory = Callable[[list[str], list[str]], FfmpegEncoderProcess]
EventCallback = Callable[[str, str], None]
ReporterFactory = Callable[..., HeartbeatReporter]


@dataclass(frozen=True)
class RunResult:
    """Outcome of a :func:`run_agent` call.

    ``frames_written`` is the count of frames successfully piped into
    FFmpeg across all attempts that ran. ``exit_reason`` is the
    categorical reason the recovery loop ended; a fatal error path
    propagates the exception rather than setting ``exit_reason``.
    """

    frames_written: int
    exit_reason: ExitReason


# ---------------------------------------------------------------------------
# Default factories
# ---------------------------------------------------------------------------


def _default_camera_factory(cfg: CameraConfig) -> VideoSource:
    return WebcamSource(cfg)


def _default_ffmpeg_factory(
    command: list[str], redacted_command: list[str]
) -> FfmpegEncoderProcess:
    return FfmpegEncoderProcess(command, redacted_command)


def _default_reporter_factory(
    *,
    client: UpstreamApiClient,
    agent_id: str,
    camera_id: str | None,
    session_id: str | None,
    agent_version: str,
    approved: ApprovedStreamConfig,
    stats: RuntimeStats,
) -> HeartbeatReporter:
    return HeartbeatReporter(
        client=client,
        agent_id=agent_id,
        camera_id=camera_id,
        session_id=session_id,
        agent_version=agent_version,
        approved=approved,
        stats=stats,
    )


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


_AttemptKind = Literal[
    "normal",
    "fatal",
    "fatal_heartbeat",
    "recoverable_stream",
    "recoverable_camera",
    "shutdown",
]


@dataclass(frozen=True)
class _AttemptOutcome:
    """Internal — describes how :func:`_run_attempt` exited.

    ``error`` carries the original exception (for re-raise on a fatal
    classification or when ``max_attempts`` is exhausted). ``error_message``
    is the safe, URL-free string the supervisor stores on
    ``RuntimeStats`` and surfaces to ``on_event``.
    """

    frames_written: int
    kind: _AttemptKind
    exit_reason_hint: ExitReason
    error: BaseException | None
    error_message: str | None


# Exception types classified as recoverable_stream — the attempt is
# torn down and a fresh session is requested after a backoff sleep.
_RECOVERABLE_STREAM_EXC: tuple[type[BaseException], ...] = (
    FfmpegExitError,
    FfmpegWriteError,
    ApiConnectionError,
    ApiServerError,
    ApiRateLimitError,
    ApiContractError,
    SrtUrlError,
)


def _classify(exc: BaseException) -> _AttemptKind:
    """Pick the right outcome kind for an exception raised inside an attempt.

    The order matters — ``ApiTokenRevokedError`` is a subclass of
    ``ApiAuthError`` so the most-specific fatal types are matched first.
    Anything not listed here is treated as fatal so we never silently
    swallow programmer bugs.
    """

    if isinstance(
        exc,
        (
            ApiTokenRevokedError,
            ApiTokenExpiredError,
            ApiAuthError,
            ApiValidationError,
            FfmpegNotFoundError,
            FfmpegSrtUnsupportedError,
            FfmpegH264UnsupportedError,
            FfmpegStartError,
            CommandBuildError,
        ),
    ):
        return "fatal"
    if isinstance(exc, CameraReadError):
        return "recoverable_camera"
    if isinstance(exc, _RECOVERABLE_STREAM_EXC):
        return "recoverable_stream"
    if isinstance(exc, StreamingRuntimeError):
        # Generic streaming errors are recoverable unless they were a
        # more specific subclass matched above.
        return "recoverable_stream"
    if isinstance(exc, FfmpegRuntimeError):
        # Any other FFmpeg-runtime subclass — treat as recoverable to
        # be conservative; the per-attempt cleanup tears down the
        # process either way.
        return "recoverable_stream"
    return "fatal"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _safe_message(exc: BaseException) -> str:
    """Return a leak-free human-readable string for an exception."""

    return f"{exc.__class__.__name__}: {exc}"


def _maybe_resize(frame: Any, target_width: int, target_height: int) -> Any:
    """If ``frame`` is not (target_height, target_width, 3), resize it."""

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

    ascontig = getattr(resized, "tobytes", None)
    if ascontig is None:
        return resized
    try:
        import numpy as np  # noqa: PLC0415

        if not resized.flags["C_CONTIGUOUS"]:
            return np.ascontiguousarray(resized)
    except Exception:  # pragma: no cover - numpy is a cv2 dep, but stay safe
        pass
    return resized


def _frame_to_bytes(frame: Any) -> bytes:
    if isinstance(frame, (bytes, bytearray)):
        return bytes(frame)
    tobytes = getattr(frame, "tobytes", None)
    if tobytes is not None:
        return tobytes()
    raise TypeError(
        f"Frame type {type(frame).__name__} cannot be converted to bytes."
    )


# ---------------------------------------------------------------------------
# Registration / session helpers (typed-error propagation, no folding)
# ---------------------------------------------------------------------------


def _register_if_needed(
    *,
    cfg: Config,
    state: Any,
    client: UpstreamApiClient,
    capabilities: dict[str, bool | str],
    save_progress: Callable[[Any], None] | None,
) -> Any:
    """Idempotently register agent + camera; return updated state.

    Lets typed ``ApiError`` subclasses propagate so the supervisor's
    error classifier can decide recoverable vs fatal. The diagnostics
    helper :func:`upstream_agent.diagnostics.api._obtain_stream_approval`
    still exists for ``upstream check --api`` (where the orchestrator
    deliberately folds errors into a result object).
    """

    working_state = state

    if getattr(working_state, "agent_id", None) is None:
        agent_req = AgentRegisterRequest(
            name=_agent_name_from(working_state.device_id),
            hostname=None,
            version=__version__,
            os=_safe_os(),
            python_version=_safe_python_version(),
            capabilities=capabilities,
        )
        agent_resp = client.register_agent(agent_req)
        working_state = working_state.model_copy(
            update={"agent_id": agent_resp.agent.id}
        )
        if save_progress is not None:
            save_progress(working_state)

    if getattr(working_state, "last_camera_id", None) is None:
        camera_req = CameraRegisterRequest(
            agent_id=working_state.agent_id,
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
        camera_resp = client.register_camera(camera_req)
        working_state = working_state.model_copy(
            update={"last_camera_id": camera_resp.camera.id}
        )
        if save_progress is not None:
            save_progress(working_state)

    return working_state


def _start_session(
    *, cfg: Config, camera_id: str, client: UpstreamApiClient
) -> StreamSessionResponse:
    """Request a fresh stream session for ``camera_id``."""

    session_req = StreamSessionRequest(
        camera_id=camera_id,
        requested=RequestedStreamConfig(
            width=cfg.camera.requested_width,
            height=cfg.camera.requested_height,
            fps=cfg.camera.requested_fps,
            codec=_DEFAULT_CODEC,
            bitrate_kbps=_DEFAULT_BITRATE_KBPS,
            latency_mode=cfg.stream.latency_mode,
        ),
    )
    return client.start_stream_session(session_req)


# ---------------------------------------------------------------------------
# Camera reopen with backoff
# ---------------------------------------------------------------------------


def _reopen_camera_with_backoff(
    *,
    cam_factory: CameraFactory,
    camera_cfg: CameraConfig,
    backoff: BackoffPolicy,
    shutdown: threading.Event,
    on_event: EventCallback | None,
    validation_frames: int = _CAMERA_RECOVERY_VALIDATION_FRAMES,
    settle_s: float = _CAMERA_RECOVERY_SETTLE_S,
    window_s: float = _CAMERA_RECOVERY_VALIDATION_WINDOW_S,
    read_retry_s: float = _CAMERA_RECOVERY_READ_RETRY_S,
    release_settle_s: float = _CAMERA_RELEASE_SETTLE_S,
    unavailable_hint_after: int = _CAMERA_UNAVAILABLE_HINT_AFTER,
) -> VideoSource | None:
    """Try to reopen the camera, sleeping between attempts.

    Returns the opened :class:`VideoSource` on success, or ``None`` if
    ``shutdown`` was set (or a ``KeyboardInterrupt`` arrived) before
    the camera came back.

    "Recovered" means **all** of:

    1. :meth:`VideoSource.open` succeeds.
    2. The device gets a short settle period (``settle_s`` seconds)
       to start producing frames. Some USB / Continuity / V4L2
       devices return success from ``open()`` before any data is
       available on the pipe.
    3. The validator collects ``validation_frames`` consecutive
       successful reads inside a ``window_s`` time window. Failed
       reads inside the window are logged but do not fail the whole
       reopen attempt; they reset the consecutive-success counter
       and the validator keeps trying until the window expires or
       enough good frames are collected.

    The triple gate is necessary because the live M7 smoke showed
    cases where a single-frame validator (with no settle) accepted
    one stale buffered frame, the supervisor declared recovery,
    started a new session + FFmpeg, and ``read_frame`` then failed
    on the very next call — tight-looping the recovery cycle until
    operators hit Ctrl+C.

    ``CameraOpenError`` and ``CameraReadError`` here are both treated
    as recoverable — the helper closes the camera and continues the
    backoff loop. The same shared :class:`BackoffPolicy` is used
    across attempts so the delay grows geometrically (up to the
    configured cap) for as long as we keep failing.

    Only ``CameraDependencyMissingError`` /
    ``UnsupportedCameraSourceError`` would escape, and those are fatal
    config issues raised before we ever streamed.
    """

    attempt = 0
    hint_emitted = False
    while True:
        if shutdown.is_set():
            return None

        attempt += 1
        if (
            not hint_emitted
            and unavailable_hint_after > 0
            and attempt > unavailable_hint_after
        ):
            hint_emitted = True
            _emit(
                on_event,
                "camera_unavailable_hint",
                (
                    "Camera device has opened but failed to produce "
                    f"frames for {unavailable_hint_after} consecutive "
                    "recovery cycles. On macOS / Continuity Camera "
                    "this often requires restarting the agent. "
                    "The agent will keep retrying."
                ),
            )
        delay = backoff.next_delay()
        _emit(
            on_event,
            "reconnect_wait",
            f"Waiting {delay:.1f}s before camera reopen attempt #{attempt}",
        )
        # KeyboardInterrupt during the sleep propagates to ``run_agent``'s
        # outer cleanup. The shutdown event is the cooperative signal.
        if backoff.sleep(delay, shutdown):
            return None  # interrupted by shutdown event

        _emit(on_event, "camera_reopen_attempt", f"Reopening camera (attempt #{attempt})")
        cam = cam_factory(camera_cfg)
        # The outer ``try/except BaseException`` guarantees the camera
        # is closed if anything raises out of this block — including
        # ``KeyboardInterrupt`` from the validator's read calls.
        validated = False
        try:
            try:
                cam.open()
            except CameraOpenError as exc:
                _emit(
                    on_event,
                    "camera_reopen_failed",
                    f"Camera reopen attempt #{attempt} failed (open): "
                    f"{_safe_message(exc)}",
                )
                # Fall through to ``finally`` which closes cam, then
                # the outer loop continues after the release-settle.
                opened_failed = True
            else:
                opened_failed = False

            if opened_failed:
                # Continue path handled below the ``try`` so the
                # ``finally`` block runs first.
                pass
            else:
                # Camera opened. Let the device settle before reading.
                _emit(
                    on_event,
                    "camera_settle",
                    f"Camera opened; waiting {settle_s:.1f}s for device to settle",
                )
                if shutdown.wait(settle_s):
                    return None  # finally closes cam

                # Validate frames inside a bounded window. Transient
                # failures inside the window reset the streak but
                # don't fail the attempt immediately.
                if _validate_camera_reads(
                    cam=cam,
                    target=validation_frames,
                    window_s=window_s,
                    retry_s=read_retry_s,
                    attempt=attempt,
                    shutdown=shutdown,
                    on_event=on_event,
                ):
                    validated = True
                    _emit(
                        on_event,
                        "camera_validated",
                        f"Camera recovery validated after "
                        f"{validation_frames} good frame(s)",
                    )
                    return cam  # ownership transferred; finally is a no-op

                # Validator failed — fall through to finally close,
                # then loop continues.
        finally:
            if not validated:
                _safe_close(cam)

        if shutdown.is_set():
            return None

        # Release-settle before the next reopen attempt so the
        # driver has time to fully let go of the device.
        if shutdown.wait(release_settle_s):
            return None
        continue


def _validate_camera_reads(
    *,
    cam: VideoSource,
    target: int,
    window_s: float,
    retry_s: float,
    attempt: int,
    shutdown: threading.Event,
    on_event: EventCallback | None,
) -> bool:
    """Collect ``target`` consecutive successful reads within ``window_s``.

    Returns True if the streak completes, False if the window expires
    or ``shutdown`` fires first. Transient failures inside the window
    are logged (one event each) but reset the streak — they do not
    fail the validator immediately. This tolerates the real-hardware
    case where a USB camera produces a couple of garbage reads before
    settling into a steady frame stream.
    """

    deadline = time.monotonic() + window_s
    consecutive_good = 0
    reads_done = 0
    transient_failures = 0

    while consecutive_good < target:
        if shutdown.is_set():
            return False
        if time.monotonic() >= deadline:
            _emit(
                on_event,
                "camera_validation_timeout",
                f"Camera validation timed out after {window_s:.1f}s "
                f"(attempt #{attempt}, {consecutive_good}/{target} good, "
                f"{transient_failures} transient failure(s)); will retry reopen",
            )
            return False

        reads_done += 1
        try:
            ok, frame = cam.read_frame()
        except CameraReadError as exc:
            transient_failures += 1
            consecutive_good = 0
            _emit(
                on_event,
                "camera_validation_read_failed",
                f"Camera validation read failed (attempt #{attempt}, "
                f"read #{reads_done}): {_safe_message(exc)}; retrying within window",
            )
            if shutdown.wait(retry_s):
                return False
            continue
        except Exception as exc:  # pragma: no cover - defensive
            transient_failures += 1
            consecutive_good = 0
            _emit(
                on_event,
                "camera_validation_read_failed",
                f"Camera validation read failed unexpectedly (attempt #{attempt}, "
                f"read #{reads_done}): {_safe_message(exc)}; retrying within window",
            )
            if shutdown.wait(retry_s):
                return False
            continue

        if not ok or frame is None:
            transient_failures += 1
            consecutive_good = 0
            _emit(
                on_event,
                "camera_validation_read_failed",
                f"Camera validation read returned no frame (attempt #{attempt}, "
                f"read #{reads_done}); retrying within window",
            )
            if shutdown.wait(retry_s):
                return False
            continue

        consecutive_good += 1

    return True


# ---------------------------------------------------------------------------
# Remote ingest confirmation (Issue 1 fix)
# ---------------------------------------------------------------------------


def _wait_for_remote_ingest(
    *,
    client: UpstreamApiClient,
    session_id: str,
    cam: VideoSource,
    ffmpeg: FfmpegEncoderProcess,
    stats: RuntimeStats,
    shutdown: threading.Event,
    target_w: int,
    target_h: int,
    on_event: EventCallback | None,
    timeout_s: float = _INGEST_CONFIRM_TIMEOUT_S,
    poll_interval_s: float = _INGEST_POLL_INTERVAL_S,
) -> tuple[int, bool]:
    """Pump frames into FFmpeg while polling the API for ``state="streaming"``.

    Local FFmpeg startup is not proof that the dashboard can see
    frames — MediaMTX has to receive the SRT publish and fire the
    ``runOnReady`` webhook before the API flips the session state
    from ``approved`` to ``streaming``. This helper writes frames as
    fast as the camera produces them (the SRT publish needs traffic
    to confirm) while periodically polling the API for the state
    flip.

    Returns ``(frames_written_during_prewarm, confirmed)``.

    * ``confirmed=True`` — API reports ``state="streaming"``. The
      caller starts the heartbeat reporter and the main frame loop.
    * ``confirmed=False`` — either ``shutdown`` fired or the
      ``timeout_s`` deadline elapsed. The caller should check
      ``shutdown.is_set()`` to disambiguate; on a real timeout it
      should raise a recoverable error so a fresh session is
      requested.

    Raises:

    * ``FfmpegExitError`` if FFmpeg dies during pre-warm — same
      classification as the main loop.
    * ``CameraReadError`` if the camera fails during pre-warm —
      same classification.
    * ``ApiTokenRevokedError`` / ``ApiTokenExpiredError`` /
      ``ApiAuthError`` / ``ApiValidationError`` from the polling
      call — propagate to the fatal classifier.
    * ``StreamingRuntimeError`` if the API reports a terminal
      state (``ended`` / ``expired`` / ``failed``) before the
      timeout — recoverable, caller will request a new session.

    Transient API errors (``ApiConnectionError`` / ``ApiServerError``
    / ``ApiRateLimitError`` / ``ApiContractError``) during polling
    log a warning event and continue polling; the overall
    ``timeout_s`` still applies so a persistent outage eventually
    surfaces as a recoverable timeout.
    """

    deadline = time.monotonic() + timeout_s
    next_poll_at = time.monotonic()  # poll on the first iteration
    frames = 0
    poll_attempts = 0

    while True:
        if shutdown.is_set():
            return frames, False
        if time.monotonic() >= deadline:
            return frames, False

        ok, frame = cam.read_frame()
        if not ok or frame is None:
            # Camera EOS during pre-warm — let the loop time out
            # rather than spinning at full speed.
            if shutdown.wait(0.05):
                return frames, False
            continue

        sized = _maybe_resize(frame, target_w, target_h)
        ffmpeg.write_frame(_frame_to_bytes(sized))
        frames += 1
        stats.record_frame()

        ffmpeg_exit = ffmpeg.poll()
        if ffmpeg_exit is not None:
            snippet = ""
            read_snippet = getattr(ffmpeg, "read_stderr_snippet", None)
            if callable(read_snippet):
                try:
                    snippet = read_snippet()
                except Exception:  # pragma: no cover - defensive
                    snippet = ""
            exit_msg = (
                f"FFmpeg exited during pre-warm with code {ffmpeg_exit}."
            )
            if snippet:
                exit_msg = f"{exit_msg} stderr: {snippet}"
            raise FfmpegExitError(exit_msg)

        if time.monotonic() < next_poll_at:
            continue

        poll_attempts += 1
        next_poll_at = time.monotonic() + poll_interval_s
        try:
            session = client.get_stream_session(session_id)
        except (
            ApiConnectionError,
            ApiServerError,
            ApiRateLimitError,
            ApiContractError,
        ) as exc:
            _emit(
                on_event,
                "ingest_poll_transient_error",
                f"Transient API error during ingest wait "
                f"(poll #{poll_attempts}): {_safe_message(exc)}",
            )
            continue

        state = session.state
        if state == "streaming":
            return frames, True
        if state in ("ended", "expired", "failed"):
            raise StreamingRuntimeError(
                f"Stream session entered terminal state {state!r} before "
                "remote ingest confirmation; will request a new session."
            )
        # state in ("approved", None, ...) — keep polling.


def _safe_close(cam: VideoSource | None) -> None:
    """Best-effort ``cam.close()``; never raises."""

    if cam is None:
        return
    try:
        cam.close()
    except Exception:  # pragma: no cover - best effort
        logger.exception("camera.close raised; continuing.")


# ---------------------------------------------------------------------------
# Per-attempt runtime
# ---------------------------------------------------------------------------


def _run_attempt(
    *,
    cfg: Config,
    state: Any,
    cam: VideoSource,
    client: UpstreamApiClient,
    stats: RuntimeStats,
    shutdown: threading.Event,
    on_event: EventCallback | None,
    ff_factory: FfmpegFactory,
    hb_factory: ReporterFactory,
    capabilities: dict[str, bool | str],
    probe: FfmpegProbeResult,
    save_progress: Callable[[Any], None] | None,
    max_frames: int | None,
    ingest_timeout_s: float = _INGEST_CONFIRM_TIMEOUT_S,
    ingest_poll_interval_s: float = _INGEST_POLL_INTERVAL_S,
) -> tuple[_AttemptOutcome, Any]:
    """Run one streaming attempt and tear it down. Never raises.

    Per-attempt cleanup ordering (always runs): heartbeat reporter
    stop → FFmpeg stop → API session stop. The camera and API client
    are owned by :func:`run_agent` and live across attempts.
    """

    working_state = state
    ffmpeg: FfmpegEncoderProcess | None = None
    reporter: HeartbeatReporter | None = None
    session_id: str | None = None
    frames_written = 0
    normal_exit_reason: ExitReason = "completed"
    outcome_kind: _AttemptKind = "normal"
    outcome_error: BaseException | None = None
    outcome_message: str | None = None

    try:
        # ---- registration (idempotent; only the first attempt pays the cost) ----
        working_state = _register_if_needed(
            cfg=cfg,
            state=working_state,
            client=client,
            capabilities=capabilities,
            save_progress=save_progress,
        )
        camera_id = working_state.last_camera_id
        if camera_id is None:
            # _register_if_needed only returns with last_camera_id set
            # if registration succeeded; this would be a programmer bug.
            raise StreamingRuntimeError(
                "camera_id is unexpectedly None after registration."
            )

        # ---- request a fresh stream session ----
        session_resp = _start_session(cfg=cfg, camera_id=camera_id, client=client)
        session_id = session_resp.session.id
        approved = session_resp.session.approved
        if approved is None:
            raise StreamingRuntimeError(
                "API approved the session but returned no approved config."
            )

        srt_url: str | None = session_resp.srt_url or session_resp.session.srt_url
        if not srt_url:
            raise StreamingRuntimeError(
                "API approved the session but no SRT URL was returned."
            )

        _emit(on_event, "api_approved", "API session approved")
        _emit(
            on_event,
            "approved_config",
            (
                f"Approved config: {approved.codec}, "
                f"{approved.width}x{approved.height} @ {approved.fps} FPS, "
                f"{approved.bitrate_kbps} kbps, {approved.latency_mode}"
            ),
        )

        # ---- build the FFmpeg command (raw stays local) ----
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

        # ---- start FFmpeg ----
        ffmpeg = ff_factory(cmd, redacted)
        ffmpeg.start()
        stats.set_ffmpeg_running(True)
        _emit(on_event, "ffmpeg_started", "FFmpeg started")

        # Drop the raw URL eagerly. cmd retains a reference internally,
        # but the local string-typed alias is the one most likely to
        # show up in tracebacks.
        del srt_url, cmd

        target_w = approved.width
        target_h = approved.height

        # ---- wait for the API to report state="streaming" ----
        # Local FFmpeg startup is NOT proof of dashboard playback.
        # MediaMTX flips the API's session state to ``"streaming"``
        # via its ``runOnReady`` webhook only when initial SRT
        # ingest succeeds. That is the strongest local-process
        # signal the agent has — but it is **only** a signal of
        # *initial* ingest, not ongoing remote playback. No code
        # path here verifies that the dashboard can actually see
        # frames; see docs/architecture.md for the full caveat.
        _emit(
            on_event,
            "waiting_for_ingest",
            "FFmpeg started; waiting for API to report session streaming",
        )
        prewarm_frames, confirmed = _wait_for_remote_ingest(
            client=client,
            session_id=session_id,
            cam=cam,
            ffmpeg=ffmpeg,
            stats=stats,
            shutdown=shutdown,
            target_w=target_w,
            target_h=target_h,
            on_event=on_event,
            timeout_s=ingest_timeout_s,
            poll_interval_s=ingest_poll_interval_s,
        )
        frames_written += prewarm_frames

        if not confirmed:
            if shutdown.is_set():
                outcome_kind = "shutdown"
                normal_exit_reason = "keyboard_interrupt"
                # Skip heartbeat + main loop; finally will clean up.
                raise _IngestSkip()
            # Real timeout — raise a recoverable error so the outer
            # loop tears down this attempt and requests a new session.
            raise StreamingRuntimeError(
                f"API did not report session streaming within "
                f"{ingest_timeout_s:.1f}s (remote_ingest_timeout); "
                "will request a new session."
            )

        _emit(
            on_event,
            "ingest_confirmed",
            "API reports session streaming "
            "(MediaMTX confirmed initial ingest). "
            "Dashboard playback is not directly verified.",
        )

        # ---- start the heartbeat reporter ----
        agent_id = getattr(working_state, "agent_id", None)
        if agent_id:
            reporter = hb_factory(
                client=client,
                agent_id=agent_id,
                camera_id=session_resp.session.camera_id,
                session_id=session_id,
                agent_version=__version__,
                approved=approved,
                stats=stats,
            )
            reporter.start()
            _emit(on_event, "heartbeat_started", "Heartbeat reporting started")
        else:
            logger.warning(
                "agent_id missing from state; heartbeat reporter not started."
            )

        # ---- frame loop ----
        _emit(
            on_event,
            "streaming",
            "Streaming started (frames flowing to streaming server).\n"
            "Press Ctrl+C to stop.",
        )
        while True:
            if shutdown.is_set():
                outcome_kind = "shutdown"
                normal_exit_reason = "keyboard_interrupt"
                break

            ok, frame = cam.read_frame()
            if not ok or frame is None:
                # Some sources may return (False, None) at EOS; treat
                # that as a normal end-of-stream rather than an error.
                normal_exit_reason = "completed"
                break

            sized = _maybe_resize(frame, target_w, target_h)
            ffmpeg.write_frame(_frame_to_bytes(sized))
            frames_written += 1
            stats.record_frame()

            ffmpeg_exit = ffmpeg.poll()
            if ffmpeg_exit is not None:
                snippet = ""
                read_snippet = getattr(ffmpeg, "read_stderr_snippet", None)
                if callable(read_snippet):
                    try:
                        snippet = read_snippet()
                    except Exception:  # pragma: no cover - defensive
                        snippet = ""
                exit_msg = f"FFmpeg exited mid-stream with code {ffmpeg_exit}."
                if snippet:
                    exit_msg = f"{exit_msg} stderr: {snippet}"
                raise FfmpegExitError(exit_msg)

            if reporter is not None and reporter.fatal_reason() is not None:
                outcome_kind = "fatal_heartbeat"
                normal_exit_reason = "fatal_heartbeat"
                outcome_message = (
                    f"Heartbeat reported fatal error: {reporter.fatal_reason()}"
                )
                _emit(on_event, "heartbeat_fatal", outcome_message)
                break

            if max_frames is not None and frames_written >= max_frames:
                normal_exit_reason = "max_frames"
                break

    except _IngestSkip:
        # Shutdown fired during ingest wait. outcome_kind +
        # normal_exit_reason were set before raising; just fall
        # through to the cleanup ``finally``.
        pass

    except Exception as exc:
        kind = _classify(exc)
        outcome_kind = kind
        outcome_error = exc
        outcome_message = _safe_message(exc)
        if kind == "fatal":
            logger.error("Fatal error during streaming attempt: %s", outcome_message)
        else:
            logger.warning(
                "Recoverable %s during streaming attempt: %s",
                kind,
                outcome_message,
            )

    finally:
        # Attempt-scoped cleanup, fixed order. None of these raise.
        if reporter is not None:
            try:
                reporter.stop(timeout=2.0)
            except Exception:  # pragma: no cover - stop should not raise
                logger.exception("reporter.stop raised; continuing cleanup.")
            _emit(on_event, "heartbeat_stopped", "Heartbeat reporting stopped")

        if ffmpeg is not None:
            try:
                ffmpeg.stop(timeout_seconds=5.0)
            except Exception:  # pragma: no cover
                logger.exception("ffmpeg.stop raised; continuing cleanup.")
            stats.set_ffmpeg_running(False)
            _emit(on_event, "ffmpeg_stopped", "FFmpeg stopped")

        if session_id is not None:
            warning = _safe_stop_session(client, session_id)
            if warning is None:
                _emit(on_event, "session_stopped", "Stream session stopped")
            else:
                _emit(
                    on_event,
                    "session_stop_warning",
                    f"Warning: failed to stop stream session: {warning}",
                )

    return (
        _AttemptOutcome(
            frames_written=frames_written,
            kind=outcome_kind,
            exit_reason_hint=normal_exit_reason,
            error=outcome_error,
            error_message=outcome_message,
        ),
        working_state,
    )


# ---------------------------------------------------------------------------
# Outer runtime
# ---------------------------------------------------------------------------


def run_agent(  # noqa: PLR0912, PLR0915  (linear flow with explicit cleanup)
    cfg: Config,
    state: Any,
    *,
    client_factory: Callable[[ApiConfig], UpstreamApiClient] | None = None,
    camera_factory: CameraFactory | None = None,
    ffmpeg_factory: FfmpegFactory | None = None,
    reporter_factory: ReporterFactory | None = None,
    ffmpeg_probe: FfmpegProbeResult | None = None,
    save_progress: Callable[[Any], None] | None = None,
    max_frames: int | None = None,
    max_attempts: int | None = None,
    backoff: BackoffPolicy | None = None,
    ingest_timeout_s: float = _INGEST_CONFIRM_TIMEOUT_S,
    ingest_poll_interval_s: float = _INGEST_POLL_INTERVAL_S,
    camera_settle_s: float = _CAMERA_RECOVERY_SETTLE_S,
    camera_validation_window_s: float = _CAMERA_RECOVERY_VALIDATION_WINDOW_S,
    camera_validation_frames: int = _CAMERA_RECOVERY_VALIDATION_FRAMES,
    camera_read_retry_s: float = _CAMERA_RECOVERY_READ_RETRY_S,
    camera_release_settle_s: float = _CAMERA_RELEASE_SETTLE_S,
    camera_unavailable_hint_after: int = _CAMERA_UNAVAILABLE_HINT_AFTER,
    on_event: EventCallback | None = None,
) -> tuple[RunResult, Any]:
    """Run the live streaming loop with automatic recovery on transient failures.

    The CLI's ``upstream run`` calls this. Tests inject the factory
    hooks, ``max_frames``, and ``max_attempts`` to drive a deterministic
    short loop without touching real subprocesses, cameras, or HTTP.

    ``max_attempts`` caps the total number of streaming attempts. The
    default ``None`` means "retry forever until success or Ctrl+C".
    Setting ``max_attempts=1`` reproduces the pre-M7 behavior: a single
    attempt, with any failure propagating to the caller.

    ``backoff`` overrides the default :class:`BackoffPolicy`. Tests use
    this to skip wall-clock sleeps.

    Cleanup order, regardless of how the function exits, is heartbeat →
    FFmpeg → camera → API session. The function re-raises any fatal
    in-flight exception after cleanup runs.
    """

    api_factory = client_factory or api_module._default_factory
    cam_factory = camera_factory or _default_camera_factory
    ff_factory = ffmpeg_factory or _default_ffmpeg_factory
    hb_factory = reporter_factory or _default_reporter_factory
    backoff_policy = backoff or BackoffPolicy()

    # Phase 1: FFmpeg probe. Failures here are always fatal — re-raise
    # before we open the camera or the API client.
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

    cam: VideoSource | None = None
    client: UpstreamApiClient | None = None
    client_ctx_active = False
    stats = RuntimeStats()
    shutdown = threading.Event()
    total_frames = 0
    working_state = state
    exit_reason: ExitReason = "completed"
    last_recoverable_error: BaseException | None = None

    try:
        # Phase 2: open camera. Initial open failure stays fatal —
        # see the M7 spec / plan decision.
        cam = cam_factory(cfg.camera)
        cam.open()
        stats.set_camera_connected(True)
        _emit(
            on_event,
            "camera_opened",
            f"Camera opened: {cfg.camera.source_type} {cfg.camera.source}",
        )

        # Phase 3: open API client (shared across attempts).
        client = api_factory(cfg.api)
        client.__enter__()
        client_ctx_active = True

        # Phase 4: recovery loop.
        attempts_started = 0
        while True:
            if shutdown.is_set():
                exit_reason = "keyboard_interrupt"
                break

            attempts_started += 1

            # KeyboardInterrupt propagates out of _run_attempt; the
            # outer ``except KeyboardInterrupt`` below runs full
            # cleanup and re-raises.
            outcome, working_state = _run_attempt(
                cfg=cfg,
                state=working_state,
                cam=cam,
                client=client,
                stats=stats,
                shutdown=shutdown,
                on_event=on_event,
                ff_factory=ff_factory,
                hb_factory=hb_factory,
                capabilities=capabilities,
                probe=probe,
                save_progress=save_progress,
                max_frames=max_frames,
                ingest_timeout_s=ingest_timeout_s,
                ingest_poll_interval_s=ingest_poll_interval_s,
            )

            total_frames += outcome.frames_written

            if outcome.kind == "normal":
                exit_reason = outcome.exit_reason_hint
                break
            if outcome.kind == "fatal_heartbeat":
                exit_reason = "fatal_heartbeat"
                break
            if outcome.kind == "shutdown":
                exit_reason = "keyboard_interrupt"
                break
            if outcome.kind == "fatal":
                if outcome.error is not None:
                    raise outcome.error
                # Defensive fall-through; classifier should always
                # have an exception attached.
                raise StreamingRuntimeError(  # pragma: no cover
                    outcome.error_message or "fatal error"
                )

            # Recoverable — either stream or camera.
            last_recoverable_error = outcome.error
            if outcome.error_message:
                stats.record_error(outcome.error_message)

            _emit(
                on_event,
                "reconnecting",
                (
                    "Recoverable failure; cleanup complete, will retry. "
                    f"{outcome.error_message or ''}"
                ).rstrip(),
            )

            # ``max_attempts`` honours pre-M7 semantics: a cap of 1
            # means "one attempt only — re-raise on failure".
            if (
                max_attempts is not None
                and attempts_started >= max_attempts
                and last_recoverable_error is not None
            ):
                _emit(
                    on_event,
                    "reconnect_exhausted",
                    f"Giving up after {attempts_started} attempt(s)",
                )
                raise last_recoverable_error

            if outcome.kind == "recoverable_camera":
                stats.set_camera_connected(False)
                _emit(
                    on_event,
                    "camera_lost",
                    outcome.error_message or "Camera disconnected",
                )
                _safe_close(cam)
                reopened = _reopen_camera_with_backoff(
                    cam_factory=cam_factory,
                    camera_cfg=cfg.camera,
                    backoff=backoff_policy,
                    shutdown=shutdown,
                    on_event=on_event,
                    validation_frames=camera_validation_frames,
                    settle_s=camera_settle_s,
                    window_s=camera_validation_window_s,
                    read_retry_s=camera_read_retry_s,
                    release_settle_s=camera_release_settle_s,
                    unavailable_hint_after=camera_unavailable_hint_after,
                )
                if reopened is None:
                    # Shutdown during reopen.
                    cam = None
                    exit_reason = "keyboard_interrupt"
                    break
                cam = reopened
                stats.set_camera_connected(True)
                _emit(on_event, "camera_reopened", "Camera reopened")
                # Note: backoff is *not* reset here. If the camera keeps
                # cycling between "open succeeded" and "read failed mid-
                # stream", we want successive recovery cycles to back
                # off more aggressively rather than restart at attempt
                # #1 every few seconds. The 30 s cap bounds the wait.
            else:
                # recoverable_stream — sleep before the next attempt.
                # KeyboardInterrupt during the sleep propagates to the
                # outer ``except`` so outer cleanup runs and the CLI
                # sees the interrupt.
                delay = backoff_policy.next_delay()
                _emit(
                    on_event,
                    "reconnect_wait",
                    f"Waiting {delay:.1f}s before retry (next attempt #{attempts_started + 1})",
                )
                interrupted = backoff_policy.sleep(delay, shutdown)
                if interrupted:
                    exit_reason = "keyboard_interrupt"
                    break

    except KeyboardInterrupt:
        shutdown.set()
        exit_reason = "keyboard_interrupt"
        stats.record_error("KeyboardInterrupt")
        _outer_cleanup(cam, client, client_ctx_active, stats, on_event)
        raise

    except Exception as exc:
        stats.record_error(_safe_message(exc))
        _outer_cleanup(cam, client, client_ctx_active, stats, on_event)
        raise

    _outer_cleanup(cam, client, client_ctx_active, stats, on_event)
    return RunResult(frames_written=total_frames, exit_reason=exit_reason), working_state


def _outer_cleanup(
    cam: VideoSource | None,
    client: UpstreamApiClient | None,
    client_ctx_active: bool,
    stats: RuntimeStats,
    on_event: EventCallback | None,
) -> None:
    """Release the camera and close the API client. Never raises.

    By the time this runs, :func:`_run_attempt`'s ``finally`` block
    has already stopped the heartbeat, FFmpeg, and the API session
    for the most recent attempt. The end-to-end teardown order is
    therefore heartbeat → FFmpeg → camera → session, even though the
    last two are emitted here.
    """

    if cam is not None:
        try:
            cam.close()
        except Exception:  # pragma: no cover
            logger.exception("camera.close raised; continuing cleanup.")
        _emit(on_event, "camera_released", "Camera released")

    if client is not None and client_ctx_active:
        try:
            client.__exit__(None, None, None)
        except Exception:  # pragma: no cover
            logger.exception("client __exit__ raised; ignoring.")
