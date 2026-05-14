"""Tests for :func:`upstream_agent.diagnostics.ffmpeg_check.probe_ffmpeg`.

All tests inject a synthetic ``runner`` so the real ``ffmpeg`` binary
is never invoked.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from upstream_agent.diagnostics.ffmpeg_check import (
    FfmpegProbeResult,
    probe_ffmpeg,
)

# ---------------------------------------------------------------------------
# Fixture output samples
# ---------------------------------------------------------------------------

_VERSION_TEXT = (
    "ffmpeg version 8.1.1 Copyright (c) 2000-2026 the FFmpeg developers\n"
    "  built with Apple clang version 21.0.0\n"
    "  configuration: --enable-libx264 --enable-libsrt\n"
)

_PROTOCOLS_WITH_SRT = (
    "Supported file protocols:\n"
    "Input:\n"
    "  rtmp\n"
    "  srt\n"
    "  tcp\n"
    "  udp\n"
    "Output:\n"
    "  file\n"
    "  srt\n"
    "  srtp\n"
)

_PROTOCOLS_WITHOUT_SRT = (
    "Supported file protocols:\n"
    "Input:\n"
    "  rtmp\n"
    "  srtp\n"  # SRTP only — not SRT
    "  tcp\n"
    "Output:\n"
    "  file\n"
    "  srtp\n"
)

_ENCODERS_WITH_LIBX264 = (
    "Encoders:\n"
    " V..... = Video\n"
    " A..... = Audio\n"
    " V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC\n"
    " V....D libx264rgb           libx264 H.264 RGB\n"
    " V....D h264_videotoolbox    VideoToolbox H.264 Encoder\n"
)

_ENCODERS_WITH_ONLY_VIDEOTOOLBOX = (
    " V....D h264_videotoolbox    VideoToolbox H.264 Encoder (codec h264)\n"
)

_ENCODERS_NO_H264 = (
    " V....D libxvid              MPEG-4 part 2\n"
    " V....D mpeg4                MPEG-4 part 2\n"
)


# ---------------------------------------------------------------------------
# Runner harness
# ---------------------------------------------------------------------------


@dataclass
class _FakeRunner:
    """Maps the first ffmpeg subcommand arg to a CompletedProcess.

    ``responses`` is a list of tuples ``(returncode, stdout)`` keyed
    by call order. ``exc`` overrides everything and is raised instead.
    """

    responses: list[tuple[int, str]]
    exc: Exception | None = None

    def __post_init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self, args: list[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        if self.exc is not None:
            raise self.exc
        idx = len(self.calls) - 1
        if idx >= len(self.responses):
            raise AssertionError(f"FakeRunner: unexpected extra call #{idx + 1} -> {args}")
        rc, stdout = self.responses[idx]
        return subprocess.CompletedProcess(
            args=args, returncode=rc, stdout=stdout, stderr=""
        )


def _happy_runner() -> _FakeRunner:
    return _FakeRunner(
        responses=[
            (0, _VERSION_TEXT),
            (0, _PROTOCOLS_WITH_SRT),
            (0, _ENCODERS_WITH_LIBX264),
        ]
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_probe_happy_path_reports_version_srt_and_libx264() -> None:
    result = probe_ffmpeg(runner=_happy_runner())

    assert isinstance(result, FfmpegProbeResult)
    assert result.available is True
    assert result.version == "8.1.1"
    assert result.srt_supported is True
    assert result.h264_encoder == "libx264"
    assert result.error is None


def test_probe_picks_videotoolbox_when_libx264_absent() -> None:
    runner = _FakeRunner(
        responses=[
            (0, _VERSION_TEXT),
            (0, _PROTOCOLS_WITH_SRT),
            (0, _ENCODERS_WITH_ONLY_VIDEOTOOLBOX),
        ]
    )
    result = probe_ffmpeg(runner=runner)
    assert result.h264_encoder == "h264_videotoolbox"


def test_probe_missing_ffmpeg_returns_unavailable() -> None:
    runner = _FakeRunner(responses=[], exc=FileNotFoundError("no such file"))
    result = probe_ffmpeg(runner=runner)

    assert result.available is False
    assert result.version is None
    assert result.srt_supported is False
    assert result.h264_encoder is None
    assert result.error is not None
    assert "not installed" in result.error.lower() or "not on path" in result.error.lower()


def test_probe_timeout_returns_clean_error() -> None:
    runner = _FakeRunner(
        responses=[],
        exc=subprocess.TimeoutExpired(cmd=["ffmpeg", "-version"], timeout=5.0),
    )
    result = probe_ffmpeg(runner=runner, timeout_seconds=5.0)

    assert result.available is False
    assert result.error is not None
    assert "timed out" in result.error.lower()


def test_probe_nonzero_exit_on_version_returns_clean_error() -> None:
    def runner_with_stderr(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args, returncode=2, stdout="", stderr="some failure"
        )

    result = probe_ffmpeg(runner=runner_with_stderr)
    assert result.available is False
    assert result.error is not None
    assert "exited with 2" in result.error


def test_probe_srt_absent_returns_srt_supported_false() -> None:
    runner = _FakeRunner(
        responses=[
            (0, _VERSION_TEXT),
            (0, _PROTOCOLS_WITHOUT_SRT),
            (0, _ENCODERS_WITH_LIBX264),
        ]
    )
    result = probe_ffmpeg(runner=runner)
    assert result.available is True
    assert result.srt_supported is False
    # H264 still reported.
    assert result.h264_encoder == "libx264"


def test_probe_no_h264_encoder_returns_none() -> None:
    runner = _FakeRunner(
        responses=[
            (0, _VERSION_TEXT),
            (0, _PROTOCOLS_WITH_SRT),
            (0, _ENCODERS_NO_H264),
        ]
    )
    result = probe_ffmpeg(runner=runner)
    assert result.available is True
    assert result.srt_supported is True
    assert result.h264_encoder is None


def test_probe_unknown_version_still_proceeds() -> None:
    runner = _FakeRunner(
        responses=[
            (0, "weird first line that does not match the pattern\n"),
            (0, _PROTOCOLS_WITH_SRT),
            (0, _ENCODERS_WITH_LIBX264),
        ]
    )
    result = probe_ffmpeg(runner=runner)
    assert result.available is True
    assert result.version is None
    assert result.srt_supported is True
    assert result.h264_encoder == "libx264"


def test_probe_runner_called_with_argv_list_and_timeout() -> None:
    runner = _happy_runner()
    probe_ffmpeg(runner=runner, timeout_seconds=2.0)

    # All three commands fired with -hide_banner.
    assert len(runner.calls) == 3
    assert runner.calls[0][0] == "ffmpeg"
    assert "-hide_banner" in runner.calls[0]
    assert "-version" in runner.calls[0]
    assert "-protocols" in runner.calls[1]
    assert "-encoders" in runner.calls[2]


def test_probe_protocols_failure_does_not_break_run() -> None:
    """If `-protocols` fails (rare) we still get a partial result."""

    def runner(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if "-version" in args:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=_VERSION_TEXT, stderr=""
            )
        if "-protocols" in args:
            return subprocess.CompletedProcess(
                args=args, returncode=3, stdout="", stderr="boom"
            )
        raise AssertionError(f"unexpected: {args}")

    result = probe_ffmpeg(runner=runner)
    assert result.available is True
    assert result.version == "8.1.1"
    assert result.srt_supported is False
    assert result.h264_encoder is None
    assert result.error is not None


def test_probe_error_messages_do_not_leak_sensitive_strings() -> None:
    """Error strings stay short and contain no token/URL-shaped values."""

    runner = _FakeRunner(responses=[], exc=FileNotFoundError())
    result = probe_ffmpeg(runner=runner)
    assert result.error is not None
    assert "Bearer" not in result.error
    assert "srt://" not in result.error
    assert "eyJ" not in result.error


def test_probe_unexpected_oserror_returns_clean_message() -> None:
    runner = _FakeRunner(responses=[], exc=OSError("permission denied"))
    result = probe_ffmpeg(runner=runner)
    assert result.available is False
    assert result.error is not None
    assert "failed to launch" in result.error
