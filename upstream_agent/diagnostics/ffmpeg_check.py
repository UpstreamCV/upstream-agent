"""Local FFmpeg installation probe.

:func:`probe_ffmpeg` runs three subprocess calls (``ffmpeg -version``,
``ffmpeg -protocols``, ``ffmpeg -encoders``) and returns a structured
:class:`FfmpegProbeResult`. It never raises; the orchestrator (and the
CLI) decide what to surface as an error.

Subprocess discipline:

* ``shell=False`` (argv list, never a string)
* explicit timeout per call
* stdout and stderr captured as text so nothing escapes to the user
  terminal
* ``FileNotFoundError`` / ``TimeoutExpired`` / nonzero exit are all
  folded into ``result.error`` with a short, leak-free message

Detection notes:

* The SRT regex matches ``srt`` on a line by itself to avoid matching
  the unrelated ``srtp`` protocol that ships with most FFmpeg builds.
* H264 encoder priority is libx264 → h264_videotoolbox → h264_nvenc →
  h264_qsv → h264_amf. CPU is enough for M4; GPU encoders are picked
  only if libx264 is missing.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

_FFMPEG_BINARY: str = "ffmpeg"
_DEFAULT_TIMEOUT: float = 5.0

# Priority order: prefer the portable CPU encoder, then platform GPU
# encoders. First match wins.
_H264_ENCODER_PRIORITY: tuple[str, ...] = (
    "libx264",
    "h264_videotoolbox",
    "h264_nvenc",
    "h264_qsv",
    "h264_amf",
)

_VERSION_LINE_RE = re.compile(r"^ffmpeg version (\S+)", re.MULTILINE)
_SRT_LINE_RE = re.compile(r"(?m)^\s*srt\s*$")


# Type alias for the subprocess runner. Tests can inject a callable
# that returns a CompletedProcess so they never touch the real binary.
Runner = Callable[[list[str], float], "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class FfmpegProbeResult:
    """Outcome of a local FFmpeg probe.

    Safe to log or display. Carries no paths, environment variables,
    or arguments that could identify the host.
    """

    available: bool
    version: str | None
    srt_supported: bool
    h264_encoder: str | None
    error: str | None


def _default_runner(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  (shell=False; argv list is fully controlled)
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _truncate(text: str, limit: int = 200) -> str:
    """Trim long stderr blobs to a single short line."""

    snippet = " ".join(text.split())
    if len(snippet) > limit:
        return snippet[: limit - 1] + "…"
    return snippet


def _run_ffmpeg(
    args: list[str], *, timeout: float, runner: Runner
) -> tuple[subprocess.CompletedProcess[str] | None, str | None]:
    """Run an ffmpeg subcommand and return (proc, error_message).

    Either ``proc`` is non-None and ``error_message`` is None, or vice
    versa. Never raises.
    """

    cmd = [_FFMPEG_BINARY, *args]
    try:
        proc = runner(cmd, timeout)
    except FileNotFoundError:
        return None, "FFmpeg is not installed or not on PATH."
    except subprocess.TimeoutExpired:
        return None, f"FFmpeg probe '{args[0]}' timed out after {timeout:g}s."
    except OSError as exc:
        return None, f"FFmpeg probe '{args[0]}' failed to launch: {exc.__class__.__name__}."
    if proc.returncode != 0:
        stderr_snippet = _truncate(proc.stderr or "")
        msg = f"ffmpeg {args[0]} exited with {proc.returncode}"
        if stderr_snippet:
            msg = f"{msg}: {stderr_snippet}"
        return None, msg
    return proc, None


def _parse_version(version_output: str) -> str | None:
    match = _VERSION_LINE_RE.search(version_output)
    if not match:
        return None
    return match.group(1)


def _detect_srt(protocols_output: str) -> bool:
    return _SRT_LINE_RE.search(protocols_output) is not None


def _detect_h264_encoder(encoders_output: str) -> str | None:
    for name in _H264_ENCODER_PRIORITY:
        # `ffmpeg -encoders` lines look like:
        #     V....D libx264              libx264 H.264 ...
        # i.e. a flags column followed by the encoder name as a token.
        pattern = re.compile(rf"(?m)^\s*\S+\s+{re.escape(name)}\b")
        if pattern.search(encoders_output):
            return name
    return None


def probe_ffmpeg(
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT,
    runner: Runner | None = None,
) -> FfmpegProbeResult:
    """Probe the local FFmpeg install and report what we found.

    Never raises. The caller (orchestrator or CLI) translates the
    result into an exit code or typed error.
    """

    active_runner = runner or _default_runner

    # Step 1: is ffmpeg even on PATH?
    version_proc, version_error = _run_ffmpeg(
        ["-hide_banner", "-version"], timeout=timeout_seconds, runner=active_runner
    )
    if version_proc is None:
        return FfmpegProbeResult(
            available=False,
            version=None,
            srt_supported=False,
            h264_encoder=None,
            error=version_error,
        )

    version = _parse_version(version_proc.stdout or "")

    # Step 2: protocols (SRT support).
    protocols_proc, protocols_error = _run_ffmpeg(
        ["-hide_banner", "-protocols"], timeout=timeout_seconds, runner=active_runner
    )
    if protocols_proc is None:
        return FfmpegProbeResult(
            available=True,
            version=version,
            srt_supported=False,
            h264_encoder=None,
            error=protocols_error,
        )
    srt_supported = _detect_srt(protocols_proc.stdout or "")

    # Step 3: encoders (H264 availability).
    encoders_proc, encoders_error = _run_ffmpeg(
        ["-hide_banner", "-encoders"], timeout=timeout_seconds, runner=active_runner
    )
    if encoders_proc is None:
        return FfmpegProbeResult(
            available=True,
            version=version,
            srt_supported=srt_supported,
            h264_encoder=None,
            error=encoders_error,
        )
    h264_encoder = _detect_h264_encoder(encoders_proc.stdout or "")

    return FfmpegProbeResult(
        available=True,
        version=version,
        srt_supported=srt_supported,
        h264_encoder=h264_encoder,
        error=None,
    )
