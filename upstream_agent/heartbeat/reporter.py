"""Background heartbeat thread that reports runtime health to the API.

The supervisor builds a :class:`HeartbeatReporter` after FFmpeg has
started and frames are flowing, then calls :meth:`start`. The reporter
spawns one daemon :class:`threading.Thread` whose loop:

1. Snapshots :class:`~upstream_agent.runtime.stats.RuntimeStats`.
2. Builds a :class:`~upstream_agent.api.schemas.Heartbeat` from the
   snapshot, the approved stream config, and the agent version.
3. POSTs it via :meth:`UpstreamApiClient.send_heartbeat`.
4. Sleeps on ``stop_event`` for the configured interval — so calling
   :meth:`stop` wakes the thread immediately rather than waiting out
   the interval.

Failure handling is intentionally minimal for Milestone 6:

* **Transient failure** (connection error, 5xx, 429, any other
  ``ApiError``): log a warning and try again next tick. No exponential
  backoff — that belongs to Milestone 7.
* **Fatal failure** (``ApiTokenRevokedError``, ``ApiTokenExpiredError``,
  ``ApiValidationError``): stop the thread and store a reason string.
  The supervisor polls :meth:`fatal_reason` after each frame; if it
  becomes non-``None``, the streaming loop exits cleanly with
  ``exit_reason="fatal_heartbeat"``.

Logging policy: the reporter only emits the typed-error string from
:class:`~upstream_agent.api.client.UpstreamApiClient`, which already
omits request/response bodies and never includes the bearer token.
Heartbeat payloads themselves carry no SRT URL, streamid, or session
token — only IDs and numeric telemetry.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone

from upstream_agent.api.client import UpstreamApiClient
from upstream_agent.api.schemas import (
    ApprovedStreamConfig,
    Heartbeat,
    HeartbeatStatus,
)
from upstream_agent.errors import (
    ApiError,
    ApiTokenExpiredError,
    ApiTokenRevokedError,
    ApiValidationError,
)
from upstream_agent.runtime.stats import RuntimeStats

logger = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_INTERVAL_S: float = 5.0
"""Heartbeat cadence. The backend does not return a next-interval
directive, so the agent picks a fixed value."""


class HeartbeatReporter:
    """Background heartbeat thread.

    The supervisor instantiates one of these after FFmpeg starts and
    calls :meth:`start` immediately. While the thread is alive the
    supervisor checks :meth:`fatal_reason` after each frame; on
    shutdown (any path) the supervisor calls :meth:`stop` before the
    other cleanup steps.

    The ``clock`` parameter exists for tests that want to drive
    ``reported_at`` deterministically. Production callers leave it
    unset and the reporter uses :func:`datetime.now`.
    """

    def __init__(
        self,
        *,
        client: UpstreamApiClient,
        agent_id: str,
        camera_id: str | None,
        session_id: str | None,
        agent_version: str,
        approved: ApprovedStreamConfig,
        stats: RuntimeStats,
        interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._agent_id = agent_id
        self._camera_id = camera_id
        self._session_id = session_id
        self._agent_version = agent_version
        self._approved = approved
        self._stats = stats
        self._interval_s = interval_s
        self._clock = clock if clock is not None else _utc_now

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._fatal_reason: str | None = None
        self._fatal_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the daemon thread. Idempotent."""

        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="heartbeat-reporter", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the thread to stop and join it.

        Logs a warning if the thread does not exit within ``timeout``
        seconds; never raises.
        """

        self._stop_event.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning(
                "Heartbeat thread did not stop within %.1fs; abandoning join.",
                timeout,
            )

    def fatal_reason(self) -> str | None:
        """Return the fatal error class name, or ``None`` if healthy."""

        with self._fatal_lock:
            return self._fatal_reason

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _set_fatal(self, reason: str) -> None:
        with self._fatal_lock:
            self._fatal_reason = reason
        self._stop_event.set()

    def _build_heartbeat(self) -> Heartbeat:
        snapshot = self._stats.snapshot()
        status: HeartbeatStatus = (
            "streaming" if snapshot.ffmpeg_running else "error"
        )
        return Heartbeat(
            camera_id=self._camera_id,
            session_id=self._session_id,
            status=status,
            fps=snapshot.fps,
            bitrate_kbps=self._approved.bitrate_kbps,
            width=self._approved.width,
            height=self._approved.height,
            codec=self._approved.codec,
            last_error=snapshot.last_error,
            agent_version=self._agent_version,
            reported_at=self._clock(),
        )

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                hb = self._build_heartbeat()
                self._client.send_heartbeat(
                    agent_id=self._agent_id, heartbeat=hb
                )
            except (
                ApiTokenRevokedError,
                ApiTokenExpiredError,
                ApiValidationError,
            ) as exc:
                logger.error(
                    "Heartbeat fatal error (%s): %s", type(exc).__name__, exc
                )
                self._set_fatal(type(exc).__name__)
                break
            except ApiError as exc:
                logger.warning("Heartbeat send failed (transient): %s", exc)
            except Exception:
                # Defensive: never let an unexpected exception kill the
                # streaming runtime. Log and try again on the next tick.
                logger.exception(
                    "Heartbeat send raised unexpected exception; continuing."
                )
            self._stop_event.wait(self._interval_s)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)
