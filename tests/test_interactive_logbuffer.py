"""RingBufferHandler + setup_logging console/extra_handlers options."""

from __future__ import annotations

import logging
from pathlib import Path

from upstream_agent.logging_setup import RingBufferHandler, setup_logging


def test_ring_buffer_caps_at_capacity() -> None:
    handler = RingBufferHandler(capacity=5)
    handler.setFormatter(logging.Formatter("%(message)s"))
    for i in range(10):
        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, f"line {i}", None, None
        )
        handler.emit(record)
    assert len(handler) == 5
    assert handler.tail(100) == [f"line {i}" for i in range(5, 10)]
    assert handler.tail(2) == ["line 8", "line 9"]
    assert handler.tail(0) == []


def test_setup_logging_console_false_omits_stderr_handler(tmp_path: Path) -> None:
    setup_logging("info", log_dir=tmp_path, console=False)
    root = logging.getLogger()
    stream_handlers = [
        h
        for h in root.handlers
        if type(h) is logging.StreamHandler  # RotatingFileHandler subclasses it
    ]
    assert stream_handlers == []


def test_setup_logging_attaches_extra_handlers_with_redaction(tmp_path: Path) -> None:
    ring = RingBufferHandler()
    secret = "ups_test_ABCDEFGH_SUPERSECRETVALUE123"
    setup_logging(
        "info",
        log_dir=tmp_path,
        sensitive_values=(secret,),
        console=False,
        extra_handlers=(ring,),
    )
    logging.getLogger("test.redaction").info("token is %s ok", secret)
    lines = ring.tail(5)
    assert lines, "ring buffer received nothing"
    assert secret not in lines[-1]
    assert "****" in lines[-1]


def test_setup_logging_default_keeps_stderr_handler(tmp_path: Path) -> None:
    setup_logging("info", log_dir=tmp_path)
    root = logging.getLogger()
    stream_handlers = [h for h in root.handlers if type(h) is logging.StreamHandler]
    assert len(stream_handlers) == 1
