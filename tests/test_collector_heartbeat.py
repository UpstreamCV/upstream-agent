"""Collector heartbeat: safe counters-only payload, distinct client_batch_id,
spool enqueue without blocking, fail-open, shutdown interruption, and that the
heartbeat batch is a valid upload request."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from upstream_agent.api.schemas import ObservationBatchRequest
from upstream_agent.collector.heartbeat import HeartbeatSender
from upstream_agent.collector.spool import Spool
from upstream_agent.collector.status import CollectorCounters

_NOW = datetime(2026, 6, 23, 18, 0, 0, tzinfo=timezone.utc)

# The complete set of payload keys the heartbeat is allowed to carry — counters
# and status only. Used as a regression guard against leaking anything else.
_ALLOWED_PAYLOAD_KEYS = {
    "version",
    "running",
    "cloud_reachable",
    "spooled_batches",
    "spooled_observations",
    "uploaded_batches",
    "uploaded_observations",
    "dropped_batches",
    "dropped_observations",
    "last_upload_at",
    "last_error_code",
}


def _spool(tmp_path: Path, counters: CollectorCounters) -> Spool:
    return Spool(
        tmp_path / "spool",
        max_observations=50_000,
        max_bytes=100_000_000,
        counters=counters,
    )


def _sender(spool: Spool, counters: CollectorCounters) -> HeartbeatSender:
    return HeartbeatSender(
        spool=spool,
        counters=counters,
        source="edge-box-4",
        interval_seconds=60.0,
        shutdown=threading.Event(),
        clock=lambda: _NOW,
    )


def test_build_payload_is_counters_only(tmp_path: Path) -> None:
    counters = CollectorCounters(clock=lambda: _NOW)
    counters.record_upload(observations=8000)  # uploaded_batches=1, uploaded_observations=8000
    spool = _spool(tmp_path, counters)
    batch = _sender(spool, counters).build(7)

    assert batch["client_batch_id"] == "collector-heartbeat:7"
    assert batch["source"] == "edge-box-4"
    obs = batch["observations"]
    assert len(obs) == 1
    assert obs[0]["type"] == "collector_heartbeat"
    payload = obs[0]["payload"]
    # Exactly the allowed counter keys — nothing else can sneak in.
    assert set(payload) == _ALLOWED_PAYLOAD_KEYS
    assert payload["running"] is True
    assert payload["uploaded_batches"] == 1
    assert payload["uploaded_observations"] == 8000
    assert payload["cloud_reachable"] is True


def test_build_payload_has_no_secrets(tmp_path: Path) -> None:
    counters = CollectorCounters()
    spool = _spool(tmp_path, counters)
    blob = json.dumps(_sender(spool, counters).build(1)).lower()
    for needle in ("ups_", "token", "secret", "authorization", "bearer", "srt://"):
        assert needle not in blob


def test_heartbeat_batch_is_a_valid_upload_request(tmp_path: Path) -> None:
    counters = CollectorCounters()
    spool = _spool(tmp_path, counters)
    # The flusher re-parses spooled batches into ObservationBatchRequest before
    # upload — the heartbeat must satisfy that contract.
    req = ObservationBatchRequest.model_validate(_sender(spool, counters).build(1))
    assert req.observations[0].type == "collector_heartbeat"


def test_send_once_enqueues_to_spool(tmp_path: Path) -> None:
    counters = CollectorCounters()
    spool = _spool(tmp_path, counters)
    _sender(spool, counters).send_once(1)
    assert spool.counts() == (1, 1)


def test_distinct_client_batch_id_per_seq(tmp_path: Path) -> None:
    counters = CollectorCounters()
    spool = _spool(tmp_path, counters)
    sender = _sender(spool, counters)
    assert sender.build(1)["client_batch_id"] != sender.build(2)["client_batch_id"]


def test_send_once_is_fail_open(tmp_path: Path) -> None:
    counters = CollectorCounters()

    class RaisingSpool:
        def counts(self) -> tuple[int, int]:
            return (0, 0)

        def append(self, batch: dict[str, Any]) -> None:
            raise RuntimeError("disk full")

    sender = HeartbeatSender(
        spool=RaisingSpool(),  # type: ignore[arg-type]
        counters=counters,
        source="edge-box-4",
        interval_seconds=60.0,
        shutdown=threading.Event(),
        clock=lambda: _NOW,
    )
    sender.send_once(1)  # must not raise


def test_run_returns_immediately_when_shutdown_set(tmp_path: Path) -> None:
    counters = CollectorCounters()
    spool = _spool(tmp_path, counters)
    shutdown = threading.Event()
    shutdown.set()
    sender = HeartbeatSender(
        spool=spool,
        counters=counters,
        source="edge-box-4",
        interval_seconds=60.0,
        shutdown=shutdown,
        clock=lambda: _NOW,
    )
    sender.run()
    assert spool.counts() == (0, 0)  # never emitted; shutdown short-circuits
