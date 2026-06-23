"""Thread-safe collector counters and a safe status snapshot.

:class:`CollectorCounters` is mutated from both the receiver and flusher
threads, so every read/write holds a lock. :class:`CollectorStatus` is the
immutable, secret-free snapshot rendered by ``GET /status`` and the
interactive shell — it never carries tokens, camera/SRT URLs, or raw
exception strings (``last_error`` is a stable code, not a message).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass(frozen=True)
class CollectorStatus:
    """Point-in-time, secret-free view of the collector."""

    running: bool
    local_endpoint: str | None
    spooled_batches: int
    spooled_observations: int
    uploaded_batches: int
    uploaded_observations: int
    dropped_batches: int
    dropped_observations: int
    last_upload_at: str | None
    last_error: str | None
    cloud_reachable: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CollectorCounters:
    """Cumulative upload/drop counters + last-upload/last-error state.

    ``last_error`` is always a stable code string (e.g. ``cloud_unreachable``,
    ``server_error``, ``auth_failed``) — never a raw exception message, which
    could embed a secret-bearing URL.
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock if clock is not None else _utc_now
        self._lock = threading.Lock()
        self._uploaded_batches = 0
        self._uploaded_observations = 0
        self._dropped_batches = 0
        self._dropped_observations = 0
        self._last_upload_at: datetime | None = None
        self._last_error: str | None = None
        # Optimistic until the first failure; flips back to True on success.
        self._cloud_reachable = True
        self._auth_failed = False

    def record_upload(self, *, observations: int) -> None:
        with self._lock:
            self._uploaded_batches += 1
            self._uploaded_observations += observations
            self._last_upload_at = self._clock()
            self._last_error = None
            self._cloud_reachable = True
            self._auth_failed = False

    def record_drop(self, *, observations: int) -> None:
        with self._lock:
            self._dropped_batches += 1
            self._dropped_observations += observations

    def record_transient_failure(self, code: str) -> None:
        with self._lock:
            self._last_error = code
            self._cloud_reachable = False

    def record_auth_failure(self, code: str) -> None:
        with self._lock:
            self._last_error = code
            self._cloud_reachable = False
            self._auth_failed = True

    @property
    def auth_failed(self) -> bool:
        with self._lock:
            return self._auth_failed

    def snapshot(self) -> dict[str, Any]:
        """The cumulative fields, for composing a :class:`CollectorStatus`."""

        with self._lock:
            return {
                "uploaded_batches": self._uploaded_batches,
                "uploaded_observations": self._uploaded_observations,
                "dropped_batches": self._dropped_batches,
                "dropped_observations": self._dropped_observations,
                "last_upload_at": (
                    self._last_upload_at.isoformat() if self._last_upload_at is not None else None
                ),
                "last_error": self._last_error,
                "cloud_reachable": self._cloud_reachable,
            }
