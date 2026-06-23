"""Collector HTTP receiver: validation, spooling, healthz/status, localhost
binding, and the optional inbound token — driven against a real
ThreadingHTTPServer on an ephemeral loopback port (sync, no async framework)."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx

from upstream_agent.collector.receiver import ObservationReceiver
from upstream_agent.collector.spool import Spool
from upstream_agent.collector.status import CollectorCounters, CollectorStatus

_SAFE_STATUS = CollectorStatus(
    running=True,
    local_endpoint="http://127.0.0.1:0/v1/observations",
    spooled_batches=3,
    spooled_observations=120,
    uploaded_batches=42,
    uploaded_observations=8000,
    dropped_batches=1,
    dropped_observations=50,
    last_upload_at="2026-06-22T18:05:20Z",
    last_error="cloud_unreachable",
    cloud_reachable=False,
)

_VALID_BODY = {
    "client_batch_id": "batch_123",
    "source": "edge-box-4",
    "observations": [
        {
            "type": "event_observed",
            "external_camera_key": "dock-12",
            "observed_at": "2026-06-22T18:05:12Z",
            "payload": {"event_name": "line_crossing", "confidence": 0.72},
        }
    ],
}


def _spool(tmp_path: Path) -> Spool:
    return Spool(
        tmp_path / "spool",
        max_observations=50_000,
        max_bytes=100_000_000,
        counters=CollectorCounters(),
    )


@contextmanager
def _receiver(
    spool: Spool,
    *,
    inbound_token: str | None = None,
    status: CollectorStatus = _SAFE_STATUS,
) -> Iterator[tuple[str, ObservationReceiver]]:
    receiver = ObservationReceiver(
        host="127.0.0.1",
        port=0,
        spool=spool,
        inbound_token=inbound_token,
        max_batch_size=500,
        status_provider=lambda: status,
    )
    thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{receiver.host}:{receiver.port}", receiver
    finally:
        receiver.shutdown()
        thread.join(timeout=5)


def test_valid_post_is_accepted_and_spooled(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    with _receiver(spool) as (base, _):
        r = httpx.post(f"{base}/v1/observations", json=_VALID_BODY, timeout=5)
    assert r.status_code == 202
    body = r.json()
    assert body["accepted"] is True
    assert body["queued"] == 1
    assert spool.counts() == (1, 1)


def test_invalid_json_returns_422(tmp_path: Path) -> None:
    with _receiver(_spool(tmp_path)) as (base, _):
        r = httpx.post(
            f"{base}/v1/observations",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_missing_required_fields_returns_422(tmp_path: Path) -> None:
    with _receiver(_spool(tmp_path)) as (base, _):
        r = httpx.post(f"{base}/v1/observations", json={"source": "x"}, timeout=5)
    assert r.status_code == 422


def test_empty_observations_returns_422(tmp_path: Path) -> None:
    body = dict(_VALID_BODY, observations=[])
    with _receiver(_spool(tmp_path)) as (base, _):
        r = httpx.post(f"{base}/v1/observations", json=body, timeout=5)
    assert r.status_code == 422


def test_healthz_is_200(tmp_path: Path) -> None:
    with _receiver(_spool(tmp_path)) as (base, _):
        r = httpx.get(f"{base}/healthz", timeout=5)
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_status_returns_safe_counters_without_secrets(tmp_path: Path) -> None:
    with _receiver(_spool(tmp_path)) as (base, _):
        r = httpx.get(f"{base}/status", timeout=5)
    assert r.status_code == 200
    body = r.json()
    assert body["spooled_observations"] == 120
    assert body["cloud_reachable"] is False
    raw = json.dumps(body)
    assert "ups_" not in raw  # no API token
    assert "token" not in raw.lower()  # no inbound token field
    assert "authorization" not in raw.lower()


def test_binding_defaults_to_localhost(tmp_path: Path) -> None:
    with _receiver(_spool(tmp_path)) as (_, receiver):
        assert receiver.host == "127.0.0.1"


def test_inbound_token_rejects_missing_and_wrong(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    with _receiver(spool, inbound_token="s3cret") as (base, _):
        missing = httpx.post(f"{base}/v1/observations", json=_VALID_BODY, timeout=5)
        wrong = httpx.post(
            f"{base}/v1/observations",
            json=_VALID_BODY,
            headers={"X-Upstream-Collector-Token": "nope"},
            timeout=5,
        )
        ok = httpx.post(
            f"{base}/v1/observations",
            json=_VALID_BODY,
            headers={"X-Upstream-Collector-Token": "s3cret"},
            timeout=5,
        )
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert ok.status_code == 202
    assert spool.counts() == (1, 1)  # only the authorized POST spooled


def test_unknown_path_404(tmp_path: Path) -> None:
    with _receiver(_spool(tmp_path)) as (base, _):
        r = httpx.get(f"{base}/nope", timeout=5)
    assert r.status_code == 404
