"""StreamWorker / WorkerRegistry: external shutdown, status mapping, stop_all."""

from __future__ import annotations

import threading
import time
from typing import Any

from upstream_agent.config import (
    ApiConfig,
    CameraConfig,
    Config,
    LoggingConfig,
    StreamConfig,
)
from upstream_agent.interactive.worker import StreamWorker, WorkerRegistry
from upstream_agent.state import State


def _cfg() -> Config:
    return Config(
        api=ApiConfig(base_url="http://api.test.local", token="ups_test_x_y"),
        camera=CameraConfig(name="Front Door", source_type="webcam", source=0),
        stream=StreamConfig(),
        logging=LoggingConfig(),
    )


def _state() -> State:
    return State(agent_id="agent_1", device_id="device-1", last_camera_id="cam_1")


def _blocking_run_fn(started: threading.Event):
    """A run_fn that emits events then blocks until shutdown fires."""

    def run(
        cfg: Any,
        state: Any,
        *,
        shutdown: threading.Event,
        on_event: Any,
        save_progress: Any = None,
        **_kwargs: Any,
    ) -> Any:
        on_event("api_approved", "API session approved")
        on_event("streaming", "Streaming started")
        started.set()
        shutdown.wait(timeout=10.0)
        return None

    return run


def test_worker_runs_in_background_and_reports_streaming_state() -> None:
    started = threading.Event()
    worker = StreamWorker(_cfg(), _state(), run_fn=_blocking_run_fn(started))
    worker.start()
    assert started.wait(timeout=5.0)
    assert worker.is_running
    assert worker.status.state == "streaming"
    assert worker.status.last_stage == "streaming"
    worker.stop()
    assert not worker.is_running
    assert worker.status.state == "stopped"


def test_worker_stop_sets_shutdown_and_joins() -> None:
    started = threading.Event()
    saw_shutdown: list[bool] = []

    def run(cfg: Any, state: Any, *, shutdown: threading.Event, **_: Any) -> None:
        started.set()
        shutdown.wait(timeout=10.0)
        saw_shutdown.append(shutdown.is_set())

    worker = StreamWorker(_cfg(), _state(), run_fn=run)
    worker.start()
    assert started.wait(timeout=5.0)
    worker.stop(timeout=5.0)
    assert saw_shutdown == [True]


def test_worker_error_is_recorded_in_status() -> None:
    def run(cfg: Any, state: Any, **_: Any) -> None:
        raise RuntimeError("camera exploded")

    worker = StreamWorker(_cfg(), _state(), run_fn=run)
    worker.start()
    deadline = time.monotonic() + 5.0
    while worker.is_running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert worker.status.state == "error"
    assert worker.status.error is not None
    assert "camera exploded" in worker.status.error


def test_worker_limits_rejected_event_marks_error() -> None:
    def run(cfg: Any, state: Any, *, on_event: Any, **_: Any) -> None:
        on_event("limits_rejected", "Server rejected: limit_exceeded.max_cameras")

    worker = StreamWorker(_cfg(), _state(), run_fn=run)
    worker.start()
    worker.stop(timeout=5.0)
    assert worker.status.error is not None
    assert "limit_exceeded.max_cameras" in worker.status.error


def test_worker_cannot_start_twice_while_running() -> None:
    started = threading.Event()
    worker = StreamWorker(_cfg(), _state(), run_fn=_blocking_run_fn(started))
    worker.start()
    assert started.wait(timeout=5.0)
    try:
        try:
            worker.start()
            raise AssertionError("expected RuntimeError")
        except RuntimeError:
            pass
    finally:
        worker.stop()


def test_registry_stop_all_stops_every_worker() -> None:
    registry = WorkerRegistry()
    events = [threading.Event(), threading.Event()]
    workers = [
        StreamWorker(_cfg(), _state(), name=f"cam-{i}", run_fn=_blocking_run_fn(ev))
        for i, ev in enumerate(events)
    ]
    for w in workers:
        registry.add(w)
        w.start()
    for ev in events:
        assert ev.wait(timeout=5.0)
    assert len(registry.running()) == 2
    registry.stop_all(timeout=5.0)
    assert registry.running() == []
    assert all(st.state == "stopped" for st in registry.statuses())
