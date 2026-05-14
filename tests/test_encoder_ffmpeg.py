"""Tests for :class:`upstream_agent.encoder.ffmpeg.FfmpegEncoderProcess`.

Every test injects a ``popen_factory`` so the real ffmpeg binary is
never invoked.
"""

from __future__ import annotations

import io
import subprocess
from typing import Any

import pytest

from upstream_agent.encoder.ffmpeg import FfmpegEncoderProcess
from upstream_agent.errors import FfmpegStartError, FfmpegWriteError

_PLACEHOLDER_SRT_URL = "srt://ingest.test.local:8890/?streamid=PLACEHOLDER"


def _happy_cmd() -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", "640x480", "-r", "30",
        "-i", "pipe:0",
        "-c:v", "libx264", "-preset", "veryfast",
        "-f", "mpegts", _PLACEHOLDER_SRT_URL,
    ]


def _redacted_cmd() -> list[str]:
    cmd = _happy_cmd()
    cmd[-1] = "srt://<redacted>"
    return cmd


class FakeStdin:
    def __init__(self, raise_on_write: Exception | None = None) -> None:
        self.writes: list[bytes] = []
        self.closed = False
        self._raise = raise_on_write

    def write(self, data: bytes) -> int:
        if self._raise is not None:
            raise self._raise
        self.writes.append(data)
        return len(data)

    def close(self) -> None:
        self.closed = True


class FakeProc:
    """Stand-in for ``subprocess.Popen``."""

    def __init__(
        self,
        *,
        write_raises: Exception | None = None,
        stderr_text: str = "",
        poll_returns: list[int | None] | None = None,
        wait_raises: Exception | None = None,
    ) -> None:
        self.stdin = FakeStdin(raise_on_write=write_raises)
        self.stderr = io.BytesIO(stderr_text.encode("utf-8")) if stderr_text else None
        self._poll_returns = poll_returns if poll_returns is not None else [None]
        self._poll_index = 0
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls: list[float | None] = []
        self._wait_raises = wait_raises

    def poll(self) -> int | None:
        if self._poll_index < len(self._poll_returns):
            value = self._poll_returns[self._poll_index]
            self._poll_index += 1
            return value
        return self._poll_returns[-1]

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self._wait_raises is not None:
            exc = self._wait_raises
            self._wait_raises = None  # Only the first wait raises.
            raise exc
        return 0


# ---------------------------------------------------------------------------
# __init__ / repr
# ---------------------------------------------------------------------------


def test_init_with_empty_command_raises() -> None:
    with pytest.raises(FfmpegStartError):
        FfmpegEncoderProcess([])


def test_init_stores_redacted_command_when_omitted() -> None:
    cmd = _happy_cmd()
    proc = FfmpegEncoderProcess(cmd)
    # Falls back to the raw command if no redacted form is supplied.
    assert proc.redacted_command == cmd


def test_repr_contains_redacted_not_raw_command() -> None:
    cmd = _happy_cmd()
    redacted = _redacted_cmd()
    proc = FfmpegEncoderProcess(cmd, redacted)
    text = repr(proc)
    assert "srt://<redacted>" in text
    assert _PLACEHOLDER_SRT_URL not in text


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------


def test_start_invokes_factory_with_command() -> None:
    seen: list[list[str]] = []
    fake = FakeProc()

    def factory(cmd: list[str]) -> Any:
        seen.append(list(cmd))
        return fake

    proc = FfmpegEncoderProcess(_happy_cmd(), _redacted_cmd(), popen_factory=factory)
    proc.start()
    assert len(seen) == 1
    assert seen[0] == _happy_cmd()
    assert proc.is_running


def test_start_twice_raises() -> None:
    proc = FfmpegEncoderProcess(_happy_cmd(), popen_factory=lambda c: FakeProc())
    proc.start()
    with pytest.raises(FfmpegStartError):
        proc.start()


def test_start_file_not_found_raises_typed_error() -> None:
    def factory(cmd: list[str]) -> Any:
        raise FileNotFoundError("no ffmpeg")

    proc = FfmpegEncoderProcess(_happy_cmd(), _redacted_cmd(), popen_factory=factory)
    with pytest.raises(FfmpegStartError) as exc_info:
        proc.start()
    assert "PATH" in str(exc_info.value) or "not found" in str(exc_info.value).lower()


def test_start_oserror_raises_typed_error_without_leak() -> None:
    def factory(cmd: list[str]) -> Any:
        raise OSError("permission denied to ffmpeg")

    proc = FfmpegEncoderProcess(_happy_cmd(), _redacted_cmd(), popen_factory=factory)
    with pytest.raises(FfmpegStartError) as exc_info:
        proc.start()
    # The OSError args aren't echoed verbatim — only the class name.
    assert "OSError" in str(exc_info.value)


# ---------------------------------------------------------------------------
# write_frame()
# ---------------------------------------------------------------------------


def test_write_frame_pipes_bytes_to_stdin() -> None:
    fake = FakeProc()
    proc = FfmpegEncoderProcess(_happy_cmd(), popen_factory=lambda c: fake)
    proc.start()
    proc.write_frame(b"abcdef")
    proc.write_frame(b"ghi")
    assert fake.stdin.writes == [b"abcdef", b"ghi"]


def test_write_frame_before_start_raises() -> None:
    proc = FfmpegEncoderProcess(_happy_cmd(), popen_factory=lambda c: FakeProc())
    with pytest.raises(FfmpegWriteError):
        proc.write_frame(b"x")


def test_write_frame_broken_pipe_raises_clean_error_with_exit_code() -> None:
    fake = FakeProc(write_raises=BrokenPipeError("pipe closed"), poll_returns=[42])
    proc = FfmpegEncoderProcess(_happy_cmd(), _redacted_cmd(), popen_factory=lambda c: fake)
    proc.start()
    with pytest.raises(FfmpegWriteError) as exc_info:
        proc.write_frame(b"x")
    msg = str(exc_info.value)
    assert "exit_code=42" in msg
    # No raw SRT URL ever in the error.
    assert _PLACEHOLDER_SRT_URL not in msg
    assert "srt://" not in msg


def test_write_frame_scrubs_srt_url_from_stderr_tail() -> None:
    stderr_text = (
        "Some FFmpeg warning\n"
        f"Error: Connection refused to {_PLACEHOLDER_SRT_URL}\n"
        "Goodbye\n"
    )
    fake = FakeProc(
        write_raises=BrokenPipeError("pipe"),
        stderr_text=stderr_text,
        poll_returns=[1],
    )
    proc = FfmpegEncoderProcess(_happy_cmd(), _redacted_cmd(), popen_factory=lambda c: fake)
    proc.start()
    with pytest.raises(FfmpegWriteError) as exc_info:
        proc.write_frame(b"x")
    msg = str(exc_info.value)
    assert _PLACEHOLDER_SRT_URL not in msg
    assert "srt://" not in msg
    # Other lines may appear (we keep "Goodbye").
    assert "Goodbye" in msg


def test_write_frame_oserror_other_than_broken_pipe_raises_typed() -> None:
    fake = FakeProc(write_raises=OSError("disk full"), poll_returns=[99])
    proc = FfmpegEncoderProcess(_happy_cmd(), popen_factory=lambda c: fake)
    proc.start()
    with pytest.raises(FfmpegWriteError):
        proc.write_frame(b"x")


# ---------------------------------------------------------------------------
# poll() / is_running
# ---------------------------------------------------------------------------


def test_poll_returns_none_before_start() -> None:
    proc = FfmpegEncoderProcess(_happy_cmd(), popen_factory=lambda c: FakeProc())
    assert proc.poll() is None


def test_poll_proxies_to_subprocess() -> None:
    fake = FakeProc(poll_returns=[None, None, 7])
    proc = FfmpegEncoderProcess(_happy_cmd(), popen_factory=lambda c: fake)
    proc.start()
    assert proc.poll() is None
    assert proc.poll() is None
    assert proc.poll() == 7


def test_is_running_flips_after_exit() -> None:
    # is_running itself calls poll(), so each is_running access advances
    # the FakeProc's poll cursor by one. Pad with Nones to keep the
    # first observation True before we let the process "exit".
    fake = FakeProc(poll_returns=[None, None, None, 0])
    proc = FfmpegEncoderProcess(_happy_cmd(), popen_factory=lambda c: fake)
    proc.start()
    assert proc.is_running is True
    # Advance to the exit code on the next poll.
    fake._poll_index = len(fake._poll_returns) - 1  # type: ignore[attr-defined]
    assert proc.is_running is False


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------


def test_stop_closes_stdin_then_terminates_then_waits() -> None:
    fake = FakeProc()
    proc = FfmpegEncoderProcess(_happy_cmd(), popen_factory=lambda c: fake)
    proc.start()
    proc.stop(timeout_seconds=2.0)
    assert fake.stdin.closed
    assert fake.terminate_calls == 1
    assert fake.wait_calls == [2.0]
    assert fake.kill_calls == 0
    assert proc.is_running is False


def test_stop_kills_after_timeout() -> None:
    fake = FakeProc(wait_raises=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=2.0))
    proc = FfmpegEncoderProcess(_happy_cmd(), popen_factory=lambda c: fake)
    proc.start()
    proc.stop(timeout_seconds=2.0)
    assert fake.terminate_calls == 1
    assert fake.kill_calls == 1
    # First wait was the timeout, second wait was unbounded.
    assert fake.wait_calls == [2.0, None]


def test_stop_before_start_is_noop() -> None:
    proc = FfmpegEncoderProcess(_happy_cmd(), popen_factory=lambda c: FakeProc())
    proc.stop()
    assert proc.is_running is False


def test_stop_is_idempotent() -> None:
    fake = FakeProc()
    proc = FfmpegEncoderProcess(_happy_cmd(), popen_factory=lambda c: fake)
    proc.start()
    proc.stop()
    proc.stop()
    assert fake.terminate_calls == 1  # Second stop did nothing.


def test_stop_never_raises_even_if_terminate_errors() -> None:
    class BoomProc(FakeProc):
        def terminate(self) -> None:
            raise OSError("nope")

    fake = BoomProc()
    proc = FfmpegEncoderProcess(_happy_cmd(), popen_factory=lambda c: fake)
    proc.start()
    proc.stop()  # Must not propagate.


# ---------------------------------------------------------------------------
# Leak guards
# ---------------------------------------------------------------------------


def test_redacted_command_property_returns_copy() -> None:
    redacted = _redacted_cmd()
    proc = FfmpegEncoderProcess(_happy_cmd(), redacted)
    snapshot = proc.redacted_command
    snapshot.append("MUTATION")
    assert proc.redacted_command == redacted  # Unchanged.


# ---------------------------------------------------------------------------
# read_stderr_snippet
# ---------------------------------------------------------------------------


def test_read_stderr_snippet_returns_empty_before_start() -> None:
    proc = FfmpegEncoderProcess(_happy_cmd(), _redacted_cmd())
    assert proc.read_stderr_snippet() == ""


def test_read_stderr_snippet_scrubs_srt_url_and_caps_length() -> None:
    stderr_text = (
        "frame= 1\n"
        f"[srt @ 0x1] connecting to {_PLACEHOLDER_SRT_URL}\n"
        "Input/output error\n"
    )
    fake = FakeProc(stderr_text=stderr_text)
    proc = FfmpegEncoderProcess(
        _happy_cmd(), _redacted_cmd(), popen_factory=lambda c: fake
    )
    proc.start()
    snippet = proc.read_stderr_snippet()
    assert "srt://" not in snippet
    assert _PLACEHOLDER_SRT_URL not in snippet
    # The non-SRT lines are preserved.
    assert "Input/output error" in snippet


def test_read_stderr_snippet_handles_missing_stderr_pipe() -> None:
    fake = FakeProc(stderr_text="")  # No stderr captured.
    proc = FfmpegEncoderProcess(
        _happy_cmd(), _redacted_cmd(), popen_factory=lambda c: fake
    )
    proc.start()
    assert proc.read_stderr_snippet() == ""
