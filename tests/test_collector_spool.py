"""Collector spool: atomic write, oldest-first drain, corrupt-file quarantine,
bounded drop-oldest overflow, and the no-secrets guarantee."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from upstream_agent.collector.spool import Spool
from upstream_agent.collector.status import CollectorCounters


def _batch(client_batch_id: str, *, observations: int = 1) -> dict[str, Any]:
    return {
        "client_batch_id": client_batch_id,
        "source": "edge-box-4",
        "observations": [
            {
                "type": "event_observed",
                "observed_at": "2026-06-22T18:05:12Z",
                "payload": {"event_name": "line_crossing", "n": i},
                "external_camera_key": "dock-12",
            }
            for i in range(observations)
        ],
    }


def _spool(
    tmp_path: Path, *, max_observations: int = 50_000, max_bytes: int = 100_000_000
) -> Spool:
    return Spool(
        tmp_path / "spool",
        max_observations=max_observations,
        max_bytes=max_bytes,
        counters=CollectorCounters(),
    )


def test_append_writes_a_json_file_and_no_tmp_remains(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    spool.append(_batch("b1", observations=2))
    files = list((tmp_path / "spool").glob("*.json"))
    assert len(files) == 1
    assert list((tmp_path / "spool").glob("*.tmp")) == []  # tmp replaced atomically
    assert spool.counts() == (1, 2)


def test_oldest_first_drain_order(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    spool.append(_batch("first"))
    spool.append(_batch("second"))
    spool.append(_batch("third"))

    e1 = spool.oldest()
    assert e1 is not None and e1.batch["client_batch_id"] == "first"
    spool.remove(e1)
    e2 = spool.oldest()
    assert e2 is not None and e2.batch["client_batch_id"] == "second"


def test_corrupt_file_is_quarantined_not_lost(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    spool.append(_batch("good"))
    # Drop a corrupt file that sorts before the good one.
    corrupt = tmp_path / "spool" / "00000000000000000000-000000-00001.json"
    corrupt.write_text("{not json", encoding="utf-8")

    entry = spool.oldest()
    assert entry is not None
    assert entry.batch["client_batch_id"] == "good"  # skipped past the corrupt one
    quarantined = list((tmp_path / "spool").glob("*.corrupt-*"))
    assert len(quarantined) == 1  # preserved, not deleted


def test_overflow_drops_oldest_and_counts(tmp_path: Path) -> None:
    counters = CollectorCounters()
    spool = Spool(
        tmp_path / "spool",
        max_observations=5,
        max_bytes=100_000_000,
        counters=counters,
    )
    spool.append(_batch("b1", observations=3))
    spool.append(_batch("b2", observations=3))  # 3+3 > 5 -> drop b1

    batches, observations = spool.counts()
    assert batches == 1
    assert observations == 3
    snap = counters.snapshot()
    assert snap["dropped_batches"] == 1
    assert snap["dropped_observations"] == 3
    remaining = spool.oldest()
    assert remaining is not None and remaining.batch["client_batch_id"] == "b2"


def test_overflow_by_bytes_drops_oldest(tmp_path: Path) -> None:
    counters = CollectorCounters()
    # Tiny byte budget so the second append evicts the first.
    spool = Spool(
        tmp_path / "spool",
        max_observations=50_000,
        max_bytes=400,
        counters=counters,
    )
    spool.append(_batch("b1", observations=2))
    spool.append(_batch("b2", observations=2))
    assert spool.counts()[0] == 1  # only the newest survived
    assert counters.snapshot()["dropped_batches"] >= 1


def test_spool_files_contain_no_auth_material(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    spool.append(_batch("b1", observations=2))
    for path in (tmp_path / "spool").glob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert "ups_" not in text
        assert "token" not in text.lower()
        assert "authorization" not in text.lower()


def test_remove_updates_counts(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    spool.append(_batch("b1", observations=4))
    entry = spool.oldest()
    assert entry is not None
    spool.remove(entry)
    assert spool.counts() == (0, 0)


def test_reindex_recovers_counts_on_restart(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    spool.append(_batch("b1", observations=2))
    spool.append(_batch("b2", observations=3))
    # A fresh Spool over the same dir recovers queued counts from filenames.
    reopened = _spool(tmp_path)
    assert reopened.counts() == (2, 5)
