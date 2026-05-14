"""Tests for :class:`upstream_agent.heartbeat.reporter.HeartbeatReporter`.

The reporter runs on a daemon thread, so the tests use a
``threading.Event`` to wait for the Nth call to finish rather than
relying on time.sleep. The interval is set very small (0.01s) so the
loop iterates quickly. The ``clock`` injection keeps ``reported_at``
deterministic.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

import pytest

from upstream_agent.api.schemas import (
    ApprovedStreamConfig,
    Heartbeat,
    HeartbeatBatchResponse,
)
from upstream_agent.errors import (
    ApiConnectionError,
    ApiTokenRevokedError,
    ApiValidationError,
)
from upstream_agent.heartbeat.reporter import HeartbeatReporter
from upstream_agent.runtime.stats import RuntimeStats

AGENT_ID = "agent-1"
CAMERA_ID = "cam-1"
SESSION_ID = "sess-1"
SECRET_TOKEN = "ups_test_secret_must_not_leak_AAAAAAA"
SECRET_SRT = "srt://secret.test.local:9999?streamid=secret"


def _approved() -> ApprovedStreamConfig:
    return ApprovedStreamConfig(
        width=1280, height=720, fps=30, codec="h264",
        bitrate_kbps=3000, latency_mode="balanced",
    )


def _fixed_clock() -> datetime:
    return datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)


class FakeClient:
    """Records each ``send_heartbeat`` call and optionally raises.

    Per-call behaviour is controlled via the ``responses`` list:
    each item is either a callable that runs (and may raise) or
    ``None`` for a default success.
    """

    def __init__(
        self,
        *,
        responses: list[Exception | None] | None = None,
        call_event: threading.Event | None = None,
        calls_to_signal: int = 1,
    ) -> None:
        self.calls: list[tuple[str, Heartbeat]] = []
        self._responses = list(responses or [])
        self._call_event = call_event
        self._calls_to_signal = calls_to_signal
        self._lock = threading.Lock()

    def send_heartbeat(
        self, *, agent_id: str, heartbeat: Heartbeat
    ) -> HeartbeatBatchResponse:
        with self._lock:
            self.calls.append((agent_id, heartbeat))
            n = len(self.calls)
            response = (
                self._responses.pop(0) if self._responses else None
            )
        if response is not None:
            raise response
        if self._call_event is not None and n >= self._calls_to_signal:
            self._call_event.set()
        return HeartbeatBatchResponse(accepted=1, server_time="2026-05-13T12:00:01Z")


def _make_reporter(
    *,
    client: FakeClient,
    stats: RuntimeStats | None = None,
    interval_s: float = 0.01,
    camera_id: str | None = CAMERA_ID,
    session_id: str | None = SESSION_ID,
) -> tuple[HeartbeatReporter, RuntimeStats]:
    if stats is None:
        stats = RuntimeStats()
        stats.set_ffmpeg_running(True)
    reporter = HeartbeatReporter(
        client=client,  # type: ignore[arg-type]
        agent_id=AGENT_ID,
        camera_id=camera_id,
        session_id=session_id,
        agent_version="0.1.0",
        approved=_approved(),
        stats=stats,
        interval_s=interval_s,
        clock=_fixed_clock,
    )
    return reporter, stats


# ---------------------------------------------------------------------------
# Lifecycle / steady-state
# ---------------------------------------------------------------------------


def test_reporter_sends_heartbeats_on_interval() -> None:
    done = threading.Event()
    client = FakeClient(call_event=done, calls_to_signal=3)
    reporter, _ = _make_reporter(client=client)

    reporter.start()
    try:
        assert done.wait(timeout=2.0), "reporter never reached 3 calls"
    finally:
        reporter.stop(timeout=1.0)

    assert len(client.calls) >= 3
    for agent_id, hb in client.calls:
        assert agent_id == AGENT_ID
        assert hb.status == "streaming"  # ffmpeg_running was set True
        assert hb.camera_id == CAMERA_ID
        assert hb.session_id == SESSION_ID
        assert hb.bitrate_kbps == 3000
        assert hb.codec == "h264"
        assert hb.width == 1280
        assert hb.height == 720
        assert hb.reported_at == _fixed_clock()
        assert hb.agent_version == "0.1.0"


def test_reporter_status_is_error_when_ffmpeg_not_running() -> None:
    done = threading.Event()
    client = FakeClient(call_event=done)
    stats = RuntimeStats()
    # Intentionally do NOT call stats.set_ffmpeg_running(True).
    reporter, _ = _make_reporter(client=client, stats=stats)

    reporter.start()
    try:
        assert done.wait(timeout=2.0)
    finally:
        reporter.stop(timeout=1.0)

    assert client.calls[0][1].status == "error"


def test_reporter_includes_stats_snapshot_data() -> None:
    done = threading.Event()
    client = FakeClient(call_event=done)
    stats = RuntimeStats()
    stats.set_ffmpeg_running(True)
    # Two frames 1s apart -> fps = 1.0
    stats.record_frame(monotonic=100.0)
    stats.record_frame(monotonic=101.0)
    stats.record_error("camera blip")
    reporter, _ = _make_reporter(client=client, stats=stats)

    reporter.start()
    try:
        assert done.wait(timeout=2.0)
    finally:
        reporter.stop(timeout=1.0)

    hb = client.calls[0][1]
    assert hb.fps == 1.0
    assert hb.last_error == "camera blip"


def test_reporter_stop_joins_quickly() -> None:
    client = FakeClient()
    reporter, _ = _make_reporter(client=client, interval_s=5.0)

    reporter.start()
    # The thread should be alive in its long sleep.
    import time

    time.sleep(0.05)
    start = time.monotonic()
    reporter.stop(timeout=2.0)
    elapsed = time.monotonic() - start
    # stop_event.wait wakes the thread immediately; join should be well
    # under the 5s interval.
    assert elapsed < 1.0


def test_reporter_start_is_idempotent() -> None:
    client = FakeClient()
    reporter, _ = _make_reporter(client=client)
    reporter.start()
    reporter.start()  # must not start a second thread
    reporter.stop(timeout=1.0)


def test_reporter_stop_is_safe_before_start() -> None:
    client = FakeClient()
    reporter, _ = _make_reporter(client=client)
    # Calling stop on an unstarted reporter must not raise.
    reporter.stop(timeout=0.1)


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_transient_failure_logs_warning_and_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    done = threading.Event()
    client = FakeClient(
        responses=[ApiConnectionError("network blip"), None, None],
        call_event=done,
        calls_to_signal=3,
    )
    reporter, _ = _make_reporter(client=client)

    reporter.start()
    try:
        assert done.wait(timeout=2.0)
    finally:
        reporter.stop(timeout=1.0)

    # We expect at least one warning log for the transient failure.
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "transient" in r.getMessage()
    ]
    assert warnings, "expected a transient-failure warning"
    # The reporter kept going and made at least the 3 calls.
    assert len(client.calls) >= 3
    # No fatal reason was recorded.
    assert reporter.fatal_reason() is None


def test_token_revoked_sets_fatal_reason_and_stops_thread() -> None:
    client = FakeClient(responses=[ApiTokenRevokedError("token revoked")])
    reporter, _ = _make_reporter(client=client)

    reporter.start()
    # The thread should self-terminate on the first call.
    import time

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and reporter.fatal_reason() is None:
        time.sleep(0.01)
    assert reporter.fatal_reason() == "ApiTokenRevokedError"

    reporter.stop(timeout=1.0)
    assert (
        reporter._thread is not None and not reporter._thread.is_alive()  # type: ignore[union-attr]
    )


def test_validation_error_sets_fatal_reason() -> None:
    client = FakeClient(responses=[ApiValidationError("bad payload")])
    reporter, _ = _make_reporter(client=client)

    reporter.start()
    import time

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and reporter.fatal_reason() is None:
        time.sleep(0.01)
    reporter.stop(timeout=1.0)
    assert reporter.fatal_reason() == "ApiValidationError"


def test_unexpected_exception_does_not_kill_thread(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-ApiError exception (e.g. a bug) must be logged but not
    bring down the reporter."""

    caplog.set_level(logging.ERROR)
    done = threading.Event()
    client = FakeClient(
        responses=[RuntimeError("unexpected"), None, None],
        call_event=done,
        calls_to_signal=2,
    )
    reporter, _ = _make_reporter(client=client)

    reporter.start()
    try:
        assert done.wait(timeout=2.0)
    finally:
        reporter.stop(timeout=1.0)

    assert reporter.fatal_reason() is None
    assert len(client.calls) >= 2


# ---------------------------------------------------------------------------
# Leak guards
# ---------------------------------------------------------------------------


def test_heartbeat_payload_carries_no_secrets() -> None:
    """The heartbeat schema has no SRT/streamid/token fields, but
    confirm nothing sneaks in via the reporter's wrapping logic."""

    done = threading.Event()
    client = FakeClient(call_event=done)
    reporter, _ = _make_reporter(client=client)

    reporter.start()
    try:
        assert done.wait(timeout=2.0)
    finally:
        reporter.stop(timeout=1.0)

    for _agent_id, hb in client.calls:
        dumped = hb.model_dump_json()
        assert "srt://" not in dumped
        assert SECRET_SRT not in dumped
        assert SECRET_TOKEN not in dumped
        assert "session_token" not in dumped
        assert "streamid" not in dumped


def test_no_token_or_srt_in_caplog_on_transient_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    done = threading.Event()
    client = FakeClient(
        responses=[ApiConnectionError("blip"), None],
        call_event=done,
        calls_to_signal=1,
    )
    reporter, _ = _make_reporter(client=client)

    reporter.start()
    try:
        assert done.wait(timeout=2.0)
    finally:
        reporter.stop(timeout=1.0)

    for record in caplog.records:
        msg = record.getMessage()
        assert SECRET_TOKEN not in msg
        assert SECRET_SRT not in msg
        assert "srt://" not in msg
