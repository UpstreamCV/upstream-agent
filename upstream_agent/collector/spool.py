"""Bounded, durable, secret-free local spool for observation batches.

One JSON file per batch, mirroring :mod:`upstream_agent.state`'s durability
idioms: write a sibling ``.tmp`` then :func:`os.replace` into place, and
quarantine a corrupt file (``.corrupt-<ts>``) rather than deleting it. Files
are drained oldest-first (filenames are time-ordered), so the queue is FIFO.

Spool files contain observation data only — never API tokens, the inbound
collector token, or any other secret.

Overflow is bounded by both a max observation count and a max byte size; when
appending would exceed either bound the spool drops the oldest batches first
(incrementing the drop counters) so the collector fails open — it never blocks
or rejects a freshly-received batch just because the queue is full.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from upstream_agent.collector.status import CollectorCounters

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpoolEntry:
    """One spooled batch, ready to upload."""

    path: Path
    batch: dict[str, Any]
    observation_count: int


@dataclass(frozen=True)
class AppendResult:
    queued_observations: int
    dropped_batches: int


def _observations_in(batch: dict[str, Any]) -> int:
    observations = batch.get("observations")
    return len(observations) if isinstance(observations, list) else 0


class Spool:
    """Directory-backed FIFO queue of observation batches."""

    def __init__(
        self,
        directory: Path,
        *,
        max_observations: int,
        max_bytes: int,
        counters: CollectorCounters,
    ) -> None:
        self._dir = directory
        self._max_observations = max_observations
        self._max_bytes = max_bytes
        self._counters = counters
        self._lock = threading.Lock()
        self._seq = 0
        self._dir.mkdir(parents=True, exist_ok=True)
        self._total_observations = 0
        self._total_bytes = 0
        self._reindex()

    # ------------------------------------------------------------------
    # Filename helpers (time-ordered so a lexical sort is chronological).
    # ------------------------------------------------------------------

    def _make_name(self, observations: int) -> str:
        self._seq += 1
        return f"{time.time_ns():020d}-{self._seq:06d}-{observations:05d}.json"

    @staticmethod
    def _observations_from_name(path: Path) -> int:
        # "<ts>-<seq>-<obs>.json"
        try:
            return int(path.stem.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            return 0

    def _files(self) -> list[Path]:
        return sorted(self._dir.glob("*.json"))

    def _reindex(self) -> None:
        total_obs = 0
        total_bytes = 0
        for path in self._files():
            try:
                total_bytes += path.stat().st_size
            except OSError:
                continue
            total_obs += self._observations_from_name(path)
        self._total_observations = total_obs
        self._total_bytes = total_bytes

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append(self, batch: dict[str, Any]) -> AppendResult:
        """Persist ``batch`` to the spool, dropping oldest batches if needed.

        Always accepts the new batch (fail-open): if the spool is at capacity,
        the oldest batches are dropped first. Returns how many observations
        were queued and how many batches were dropped to make room.
        """

        payload = json.dumps(batch, separators=(",", ":")).encode("utf-8")
        observations = _observations_in(batch)
        with self._lock:
            dropped = self._evict_for(
                incoming_observations=observations, incoming_bytes=len(payload)
            )
            name = self._make_name(observations)
            tmp = self._dir / (name + ".tmp")
            final = self._dir / name
            tmp.write_bytes(payload)
            tmp.replace(final)
            self._total_observations += observations
            self._total_bytes += len(payload)
        return AppendResult(queued_observations=observations, dropped_batches=dropped)

    def _evict_for(self, *, incoming_observations: int, incoming_bytes: int) -> int:
        """Drop oldest batches until the incoming one fits, or the spool is
        empty. Caller holds the lock. Returns the number dropped."""

        dropped = 0
        files = self._files()
        idx = 0
        while files and idx < len(files):
            obs_fits = self._total_observations + incoming_observations <= self._max_observations
            bytes_fits = self._total_bytes + incoming_bytes <= self._max_bytes
            if obs_fits and bytes_fits:
                break
            oldest = files[idx]
            idx += 1
            obs = self._observations_from_name(oldest)
            try:
                size = oldest.stat().st_size
            except OSError:
                size = 0
            try:
                oldest.unlink()
            except OSError:
                continue
            self._total_observations = max(0, self._total_observations - obs)
            self._total_bytes = max(0, self._total_bytes - size)
            self._counters.record_drop(observations=obs)
            dropped += 1
            logger.warning("collector spool full; dropped oldest batch (%d observations)", obs)
        return dropped

    def oldest(self) -> SpoolEntry | None:
        """Return the oldest readable batch without removing it.

        Corrupt files are quarantined (renamed ``.corrupt-<ts>``) and skipped,
        so a single bad file never wedges the drain.
        """

        with self._lock:
            for path in self._files():
                try:
                    raw = path.read_text(encoding="utf-8")
                    batch = json.loads(raw)
                except (OSError, ValueError) as exc:
                    self._quarantine(path, exc)
                    continue
                if not isinstance(batch, dict):
                    self._quarantine(path, ValueError("not a JSON object"))
                    continue
                return SpoolEntry(
                    path=path,
                    batch=batch,
                    observation_count=self._observations_from_name(path),
                )
        return None

    def remove(self, entry: SpoolEntry) -> None:
        """Delete a successfully-uploaded batch and update totals."""

        with self._lock:
            try:
                size = entry.path.stat().st_size
            except OSError:
                size = 0
            try:
                entry.path.unlink()
            except OSError:
                return
            self._total_observations = max(0, self._total_observations - entry.observation_count)
            self._total_bytes = max(0, self._total_bytes - size)

    def counts(self) -> tuple[int, int]:
        """``(batch_count, observation_count)`` currently queued."""

        with self._lock:
            return len(self._files()), self._total_observations

    def _quarantine(self, path: Path, exc: Exception) -> None:
        backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            path.rename(backup)
        except OSError:
            return
        with contextlib.suppress(OSError):
            self._total_bytes = max(0, self._total_bytes - backup.stat().st_size)
        self._total_observations = max(
            0, self._total_observations - self._observations_from_name(path)
        )
        logger.warning(
            "collector spool file %s was corrupt (%s); quarantined to %s",
            path.name,
            exc.__class__.__name__,
            backup.name,
        )
