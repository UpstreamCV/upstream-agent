"""Collector flusher: oldest-first drain, delete-on-success, keep-on-failure,
retry, backoff usage, shutdown interruption, and fatal-auth pause — all driven
synchronously through the loop internals with fake clients/backoff (no threads,
no network)."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from upstream_agent.api.schemas import ObservationBatchResponse
from upstream_agent.collector.flusher import Flusher
from upstream_agent.collector.spool import Spool
from upstream_agent.collector.status import CollectorCounters
from upstream_agent.errors import ApiServerError, ApiTokenRevokedError


def _batch(client_batch_id: str, *, observations: int = 1) -> dict[str, Any]:
    return {
        "client_batch_id": client_batch_id,
        "source": "edge-box-4",
        "observations": [
            {
                "type": "event_observed",
                "observed_at": "2026-06-22T18:05:12Z",
                "payload": {"n": i},
                "external_camera_key": "dock-12",
            }
            for i in range(observations)
        ],
    }


class FakeClient:
    """Returns success unless an exception is scripted for the call."""

    def __init__(self, *, errors: list[Exception | None] | None = None) -> None:
        self._errors = list(errors or [])
        self.uploads: list[Any] = []

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def upload_observations(self, req: Any) -> ObservationBatchResponse:
        self.uploads.append(req)
        if self._errors:
            exc = self._errors.pop(0)
            if exc is not None:
                raise exc
        return ObservationBatchResponse(
            accepted=len(req.observations),
            batch_id="b",
            server_time=datetime(2026, 6, 22, 18, 5, 20, tzinfo=timezone.utc),
        )


class FakeBackoff:
    def __init__(self) -> None:
        self.next_calls = 0
        self.sleep_calls = 0
        self.resets = 0

    def next_delay(self) -> float:
        self.next_calls += 1
        return 0.0

    def sleep(self, delay: float, shutdown: threading.Event) -> bool:
        self.sleep_calls += 1
        return True  # report "interrupted" so the loop returns deterministically

    def reset(self) -> None:
        self.resets += 1


def _spool(tmp_path: Path, counters: CollectorCounters) -> Spool:
    return Spool(
        tmp_path / "spool",
        max_observations=50_000,
        max_bytes=100_000_000,
        counters=counters,
    )


def _flusher(spool: Spool, counters: CollectorCounters) -> Flusher:
    return Flusher(
        spool=spool,
        counters=counters,
        client_factory=lambda _b, _t: FakeClient(),
        base_url="http://api.test",
        token="ups_test_x",
        flush_interval_seconds=0.01,
        shutdown=threading.Event(),
    )


def test_successful_upload_deletes_spool_file(tmp_path: Path) -> None:
    counters = CollectorCounters()
    spool = _spool(tmp_path, counters)
    spool.append(_batch("b1", observations=2))
    flusher = _flusher(spool, counters)

    outcome = flusher._drain(FakeClient())
    assert outcome == "progress"
    assert spool.counts() == (0, 0)
    snap = counters.snapshot()
    assert snap["uploaded_batches"] == 1
    assert snap["uploaded_observations"] == 2
    assert snap["cloud_reachable"] is True


def test_failed_upload_keeps_spool_file(tmp_path: Path) -> None:
    counters = CollectorCounters()
    spool = _spool(tmp_path, counters)
    spool.append(_batch("b1"))
    flusher = _flusher(spool, counters)

    outcome = flusher._drain(FakeClient(errors=[ApiServerError("503")]))
    assert outcome == "transient"
    assert spool.counts() == (1, 1)  # kept for retry
    snap = counters.snapshot()
    assert snap["cloud_reachable"] is False
    assert snap["last_error"] == "server_error"
    assert snap["uploaded_batches"] == 0


def test_retry_after_failure_succeeds(tmp_path: Path) -> None:
    counters = CollectorCounters()
    spool = _spool(tmp_path, counters)
    spool.append(_batch("b1"))
    flusher = _flusher(spool, counters)

    flusher._drain(FakeClient(errors=[ApiServerError("503")]))  # first attempt fails
    assert spool.counts() == (1, 1)
    flusher._drain(FakeClient())  # retry succeeds
    assert spool.counts() == (0, 0)
    assert counters.snapshot()["uploaded_batches"] == 1


def test_fatal_auth_pauses_and_keeps_data(tmp_path: Path) -> None:
    counters = CollectorCounters()
    spool = _spool(tmp_path, counters)
    spool.append(_batch("b1"))
    flusher = _flusher(spool, counters)

    outcome = flusher._drain(FakeClient(errors=[ApiTokenRevokedError("revoked")]))
    assert outcome == "fatal"
    assert spool.counts() == (1, 1)  # data preserved
    snap = counters.snapshot()
    assert snap["last_error"] == "auth_failed"
    assert counters.auth_failed is True


def test_loop_uses_backoff_on_transient(tmp_path: Path) -> None:
    counters = CollectorCounters()
    spool = _spool(tmp_path, counters)
    spool.append(_batch("b1"))
    flusher = _flusher(spool, counters)
    backoff = FakeBackoff()

    flusher._loop(FakeClient(errors=[ApiServerError("503")]), backoff)
    assert backoff.sleep_calls == 1  # backed off after the transient failure
    assert backoff.next_calls == 1


def test_loop_returns_on_fatal_without_backoff(tmp_path: Path) -> None:
    counters = CollectorCounters()
    spool = _spool(tmp_path, counters)
    spool.append(_batch("b1"))
    flusher = _flusher(spool, counters)
    backoff = FakeBackoff()

    flusher._loop(FakeClient(errors=[ApiTokenRevokedError("revoked")]), backoff)
    assert backoff.sleep_calls == 0  # paused immediately, no retry backoff


def test_loop_returns_immediately_when_shutdown_set(tmp_path: Path) -> None:
    counters = CollectorCounters()
    spool = _spool(tmp_path, counters)
    spool.append(_batch("b1"))
    flusher = _flusher(spool, counters)
    flusher._shutdown.set()
    client = FakeClient()

    flusher._loop(client, FakeBackoff())
    assert client.uploads == []  # never uploaded; shutdown short-circuits


def test_run_never_raises_even_if_client_factory_fails(tmp_path: Path) -> None:
    counters = CollectorCounters()
    spool = _spool(tmp_path, counters)

    def _boom(_b: str, _t: str) -> Any:
        raise RuntimeError("factory boom")

    flusher = Flusher(
        spool=spool,
        counters=counters,
        client_factory=_boom,
        base_url="x",
        token="y",
        flush_interval_seconds=0.01,
        shutdown=threading.Event(),
    )
    flusher.run()  # must not raise
