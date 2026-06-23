"""Background flush loop that uploads spooled batches to Upstream Cloud.

Mirrors :class:`~upstream_agent.heartbeat.reporter.HeartbeatReporter`'s daemon
pattern: one thread, a shared shutdown :class:`threading.Event`, and a single
:class:`~upstream_agent.runtime.backoff.BackoffPolicy` instance owned by the
loop (never shared across threads). It drains the spool oldest-first, deleting
each batch only after a successful upload.

Fail-open behavior:

* **Transient** (connection error, 5xx, 429, rejection, bad response): keep the
  batch spooled, back off, retry next cycle.
* **Fatal auth** (revoked/expired/forbidden token): record the failure safely
  and stop the upload loop — the receiver keeps accepting locally so data isn't
  lost while the operator fixes the token.
* **Unexpected**: logged safely; the thread exits without taking the agent down.

The loop logs only typed-error strings (which already omit bodies and tokens);
it never logs observation payloads.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from pydantic import ValidationError

from upstream_agent.api.client import UpstreamApiClient
from upstream_agent.api.schemas import ObservationBatchRequest
from upstream_agent.collector.spool import Spool
from upstream_agent.collector.status import CollectorCounters
from upstream_agent.errors import (
    ApiAuthError,
    ApiConnectionError,
    ApiError,
    ApiRateLimitError,
    ApiServerError,
    ApiValidationError,
)
from upstream_agent.runtime.backoff import BackoffPolicy

logger = logging.getLogger(__name__)

_ClientFactory = Callable[[str, str], UpstreamApiClient]
_BackoffFactory = Callable[[], BackoffPolicy]

# Drain outcomes.
_EMPTY = "empty"
_PROGRESS = "progress"
_TRANSIENT = "transient"
_FATAL = "fatal"


def _transient_code(exc: ApiError) -> str:
    if isinstance(exc, ApiConnectionError):
        return "cloud_unreachable"
    if isinstance(exc, ApiServerError):
        return "server_error"
    if isinstance(exc, ApiRateLimitError):
        return "rate_limited"
    if isinstance(exc, ApiValidationError):
        return "rejected"
    return "upload_error"


class Flusher:
    """Uploads spooled batches on a daemon thread until ``shutdown`` is set."""

    def __init__(
        self,
        *,
        spool: Spool,
        counters: CollectorCounters,
        client_factory: _ClientFactory,
        base_url: str,
        token: str,
        flush_interval_seconds: float,
        shutdown: threading.Event,
        backoff_factory: _BackoffFactory | None = None,
    ) -> None:
        self._spool = spool
        self._counters = counters
        self._client_factory = client_factory
        self._base_url = base_url
        self._token = token
        self._flush_interval = flush_interval_seconds
        self._shutdown = shutdown
        self._backoff_factory = backoff_factory or (lambda: BackoffPolicy())

    def run(self) -> None:
        """Thread target. Never raises — an unexpected error is logged and the
        thread exits cleanly, leaving the receiver running."""

        backoff = self._backoff_factory()
        try:
            with self._client_factory(self._base_url, self._token) as client:
                self._loop(client, backoff)
        except Exception:  # pragma: no cover - defensive last resort
            logger.exception("collector flusher crashed; uploads paused")

    def _loop(self, client: UpstreamApiClient, backoff: BackoffPolicy) -> None:
        while not self._shutdown.is_set():
            outcome = self._drain(client)
            if outcome == _FATAL:
                return
            if outcome == _TRANSIENT:
                if backoff.sleep(backoff.next_delay(), self._shutdown):
                    return  # shutdown during backoff
                continue
            # Empty or made progress: healthy cadence.
            backoff.reset()
            if self._shutdown.wait(self._flush_interval):
                return

    def _drain(self, client: UpstreamApiClient) -> str:
        """Upload oldest-first until the spool is empty or a failure occurs."""

        uploaded_any = False
        while not self._shutdown.is_set():
            entry = self._spool.oldest()
            if entry is None:
                return _PROGRESS if uploaded_any else _EMPTY

            try:
                req = ObservationBatchRequest.model_validate(entry.batch)
            except ValidationError:
                # A spooled batch we can never upload (shouldn't happen — the
                # receiver validated it). Drop it rather than wedge the queue.
                logger.warning("collector dropping an unparseable spooled batch")
                self._spool.remove(entry)
                self._counters.record_drop(observations=entry.observation_count)
                continue

            try:
                client.upload_observations(req)
            except ApiAuthError as exc:
                # Revoked / expired / forbidden — retrying won't help. Pause
                # uploads; the receiver keeps buffering locally.
                logger.error(
                    "collector upload auth failure (%s); pausing uploads",
                    type(exc).__name__,
                )
                self._counters.record_auth_failure("auth_failed")
                return _FATAL
            except ApiError as exc:
                self._counters.record_transient_failure(_transient_code(exc))
                logger.warning(
                    "collector upload failed (%s); keeping batch spooled",
                    type(exc).__name__,
                )
                return _TRANSIENT

            self._counters.record_upload(observations=entry.observation_count)
            self._spool.remove(entry)
            uploaded_any = True
        return _PROGRESS if uploaded_any else _EMPTY
