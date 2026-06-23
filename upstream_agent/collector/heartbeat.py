"""Cloud-visible collector heartbeat (Upstream Reliability M1c).

A low-frequency daemon thread that periodically enqueues a safe
``collector_heartbeat`` observation into the spool, so it rides the existing
flusher → ``POST /v1/reliability/observations`` path and the dashboard can show
true collector health (the browser can't reach the collector's localhost
``/status``).

Safety:
- Payload is **counters only** — version + running + cloud-reachable + spool/
  upload/drop counts + last-upload time + last-error CODE. Never a token, never
  a raw observation payload, never a camera/SRT URL.
- Time-based (not event-based) so it can't recursively trigger itself.
- A distinct ``collector-heartbeat:<seq>`` client_batch_id keeps it idempotent
  and separable from customer batches (the cloud excludes it from the
  source/activity views).
- Fail-open: a build/spool error is logged and swallowed; the receiver and
  flusher are never affected.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from upstream_agent.collector.spool import Spool
from upstream_agent.collector.status import CollectorCounters
from upstream_agent.version import __version__

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


class HeartbeatSender:
    """Periodically spools a ``collector_heartbeat`` observation batch."""

    def __init__(
        self,
        *,
        spool: Spool,
        counters: CollectorCounters,
        source: str,
        interval_seconds: float,
        shutdown: threading.Event,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._spool = spool
        self._counters = counters
        self._source = source
        self._interval = interval_seconds
        self._shutdown = shutdown
        self._clock = clock if clock is not None else _utc_now

    def build(self, seq: int) -> dict[str, Any]:
        """Build one heartbeat batch (JSON-safe, counters only)."""

        snap = self._counters.snapshot()
        spooled_batches, spooled_observations = self._spool.counts()
        payload: dict[str, Any] = {
            "version": __version__,
            "running": True,
            "cloud_reachable": snap["cloud_reachable"],
            "spooled_batches": spooled_batches,
            "spooled_observations": spooled_observations,
            "uploaded_batches": snap["uploaded_batches"],
            "uploaded_observations": snap["uploaded_observations"],
            "dropped_batches": snap["dropped_batches"],
            "dropped_observations": snap["dropped_observations"],
            "last_upload_at": snap["last_upload_at"],
            "last_error_code": snap["last_error"],
        }
        return {
            "client_batch_id": f"collector-heartbeat:{seq}",
            "source": self._source,
            "observations": [
                {
                    "type": "collector_heartbeat",
                    "observed_at": self._clock().isoformat(),
                    "payload": payload,
                }
            ],
        }

    def send_once(self, seq: int) -> None:
        """Build + spool one heartbeat. Never raises (fail-open)."""

        try:
            self._spool.append(self.build(seq))
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("collector heartbeat failed: %s", type(exc).__name__)

    def run(self) -> None:
        """Thread target: spool a heartbeat every interval until shutdown.

        Sleeps first so a just-started collector doesn't immediately emit before
        anything has flowed; ``stop_event.wait`` wakes instantly on shutdown.
        """

        seq = 0
        while not self._shutdown.is_set():
            if self._shutdown.wait(self._interval):
                return  # shutdown during the interval
            seq += 1
            self.send_once(seq)
