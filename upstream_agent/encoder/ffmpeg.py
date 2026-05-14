"""FFmpeg subprocess wrapper for the streaming runtime.

:class:`FfmpegEncoderProcess` owns one ``subprocess.Popen`` instance. It
runs ffmpeg with ``shell=False`` and pipes BGR frame bytes into
ffmpeg's stdin. The class is deliberately small — it is the only
place the raw FFmpeg command (containing the SRT URL) lives. The
``redacted_command`` form is the only shape that ever appears in
:func:`repr`, logs, or error messages.

Failure modes are typed:

* :class:`FfmpegStartError` — the subprocess could not be launched
  (binary missing, OS denied exec, called twice).
* :class:`FfmpegWriteError` — ``proc.stdin.write`` raised
  ``BrokenPipeError`` / ``OSError`` (usually because FFmpeg exited).
* :class:`FfmpegExitError` is **not** raised by this class — the
  supervisor raises it after observing ``poll()`` go non-None.

Subprocess discipline:

* ``shell=False`` (argv list)
* ``stdin=PIPE`` for raw BGR frames
* ``stdout=DEVNULL`` (we don't need ffmpeg's stdout)
* ``stderr=PIPE`` so we can drain a short tail on failure — but any
  line containing ``srt://`` is scrubbed before it reaches an error
  message or log record.
"""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Callable
from typing import Any

from upstream_agent.errors import FfmpegStartError, FfmpegWriteError

# Type alias kept loose: callers pass either real :class:`subprocess.Popen`
# or a test stand-in that quacks like it (``stdin``, ``poll``, ``wait``,
# ``terminate``, ``kill``, ``communicate``).
PopenFactory = Callable[[list[str]], Any]

_STDERR_SNIPPET_LIMIT = 200


def _default_popen_factory(command: list[str]) -> subprocess.Popen:
    return subprocess.Popen(  # noqa: S603  (shell=False; argv fully controlled)
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        shell=False,
        bufsize=0,
    )


def _scrub_stderr(text: str) -> str:
    """Return a short, leak-free snippet of FFmpeg's stderr.

    Drops any line containing ``srt://`` (FFmpeg likes to echo the
    output URL back in its own error messages). Caps the total
    length so even an otherwise-clean stderr cannot pad a log line.
    """

    if not text:
        return ""
    safe_lines: list[str] = []
    for line in text.splitlines():
        if "srt://" in line.lower():
            continue
        safe_lines.append(line.strip())
    snippet = " | ".join(filter(None, safe_lines))
    if len(snippet) > _STDERR_SNIPPET_LIMIT:
        return snippet[: _STDERR_SNIPPET_LIMIT - 1] + "…"
    return snippet


class FfmpegEncoderProcess:
    """A managed FFmpeg subprocess that consumes raw BGR frames on stdin.

    The constructor stores the command but does **not** start the
    process. Call :meth:`start` once before :meth:`write_frame`. Call
    :meth:`stop` exactly once at shutdown; calling it again is a
    no-op.

    The ``redacted_command`` is what ``__repr__`` returns and what
    error messages quote. It typically comes from
    :func:`upstream_agent.encoder.command_builder.redact_command`.
    """

    def __init__(
        self,
        command: list[str],
        redacted_command: list[str] | None = None,
        *,
        popen_factory: PopenFactory | None = None,
    ) -> None:
        if not command or not isinstance(command, list):
            raise FfmpegStartError("FFmpeg command must be a non-empty list[str].")
        self._command: list[str] = list(command)
        self._redacted_command: list[str] = (
            list(redacted_command) if redacted_command is not None else list(command)
        )
        self._popen_factory: PopenFactory = popen_factory or _default_popen_factory
        self._proc: Any = None
        self._started: bool = False

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Launch FFmpeg. Idempotent guard: a second call raises."""

        if self._started:
            raise FfmpegStartError("FfmpegEncoderProcess.start() called more than once.")
        try:
            self._proc = self._popen_factory(self._command)
        except FileNotFoundError as exc:
            self._started = True
            raise FfmpegStartError(
                "FFmpeg binary not found. Ensure 'ffmpeg' is installed and on PATH."
            ) from exc
        except OSError as exc:
            self._started = True
            raise FfmpegStartError(
                f"Failed to launch FFmpeg: {exc.__class__.__name__}."
            ) from exc
        self._started = True

    def write_frame(self, frame_bytes: bytes) -> None:
        """Write one raw BGR frame to FFmpeg's stdin pipe."""

        if self._proc is None:
            raise FfmpegWriteError("FFmpeg is not running; call start() first.")
        stdin = getattr(self._proc, "stdin", None)
        if stdin is None:
            raise FfmpegWriteError("FFmpeg stdin pipe is not available.")
        try:
            stdin.write(frame_bytes)
        except BrokenPipeError as exc:
            raise self._broken_pipe_error("BrokenPipeError") from exc
        except OSError as exc:
            raise self._broken_pipe_error(exc.__class__.__name__) from exc

    def poll(self) -> int | None:
        """Return the FFmpeg exit code or None if still running."""

        if self._proc is None:
            return None
        return self._proc.poll()

    def stop(self, timeout_seconds: float = 5.0) -> None:
        """Close stdin, terminate FFmpeg, then kill after a timeout.

        Never raises. Idempotent — after :meth:`stop` the wrapper is
        in the same observable state regardless of how many times
        you call it.
        """

        if self._proc is None:
            return
        proc = self._proc
        # Drop the reference up-front so a re-entrant call is a no-op
        # even if anything below raises.
        self._proc = None

        stdin = getattr(proc, "stdin", None)
        if stdin is not None:
            with contextlib.suppress(OSError):
                stdin.close()

        with contextlib.suppress(OSError):
            proc.terminate()

        try:
            proc.wait(timeout=timeout_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        except OSError:
            return

        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(OSError):
            proc.wait()

    # ------------------------------------------------------------------ helpers

    @property
    def is_running(self) -> bool:
        """True iff the subprocess has been started and has not exited."""

        if self._proc is None:
            return False
        return self._proc.poll() is None

    @property
    def redacted_command(self) -> list[str]:
        return list(self._redacted_command)

    def __repr__(self) -> str:
        return f"FfmpegEncoderProcess(redacted_command={self._redacted_command!r})"

    # ------------------------------------------------------------------ internals

    def _broken_pipe_error(self, cause: str) -> FfmpegWriteError:
        """Build a leak-free :class:`FfmpegWriteError` after a pipe failure."""

        exit_code = self.poll()
        stderr_snippet = ""
        proc = self._proc
        if proc is not None:
            stderr_stream = getattr(proc, "stderr", None)
            if stderr_stream is not None:
                try:
                    raw = stderr_stream.read()
                except OSError:
                    raw = None
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                if isinstance(raw, str):
                    stderr_snippet = _scrub_stderr(raw)
        msg = f"FFmpeg pipe closed ({cause}); exit_code={exit_code}"
        if stderr_snippet:
            msg = f"{msg}; stderr: {stderr_snippet}"
        return FfmpegWriteError(msg)
