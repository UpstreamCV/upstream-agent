"""Tests for :mod:`upstream_agent.encoder.command_builder`."""

from __future__ import annotations

import pytest

from upstream_agent.encoder.command_builder import (
    build_ffmpeg_command,
    redact_command,
)
from upstream_agent.errors import CommandBuildError

_PLACEHOLDER_SRT_URL = "srt://ingest.test.local:8890/?streamid=TEST_PLACEHOLDER"


def _happy_kwargs() -> dict:
    return dict(
        width=1280,
        height=720,
        fps=30,
        bitrate_kbps=3000,
        codec="h264",
        encoder="libx264",
        latency_mode="balanced",
        srt_url=_PLACEHOLDER_SRT_URL,
    )


def test_returns_list_of_strings_not_a_shell_string() -> None:
    cmd = build_ffmpeg_command(**_happy_kwargs())
    assert isinstance(cmd, list)
    assert all(isinstance(t, str) for t in cmd)


def test_includes_approved_resolution_fps_bitrate_codec() -> None:
    cmd = build_ffmpeg_command(**_happy_kwargs())
    assert "1280x720" in cmd
    assert "30" in cmd
    assert "3000k" in cmd
    assert "libx264" in cmd
    # Output container suitable for SRT.
    assert "mpegts" in cmd


def test_srt_url_is_the_final_positional_arg() -> None:
    cmd = build_ffmpeg_command(**_happy_kwargs())
    assert cmd[-1] == _PLACEHOLDER_SRT_URL
    # And the format flag is right before it.
    assert cmd[-2] == "mpegts"


def test_encoder_choice_drives_c_v_arg() -> None:
    kw = _happy_kwargs()
    kw["encoder"] = "h264_videotoolbox"
    cmd = build_ffmpeg_command(**kw)
    c_v_index = cmd.index("-c:v")
    assert cmd[c_v_index + 1] == "h264_videotoolbox"


def test_keyframe_cadence_is_one_second() -> None:
    """GOP must equal fps so WebRTC readers see a keyframe within ~1s of join.

    Without an explicit ``-g``, libx264 defaults to 250 and h264_videotoolbox
    similar — meaning the dashboard's first-frame latency would be 4+ seconds
    at 60 fps. Pinning the GOP to fps caps that wait at ~1s.
    """
    for fps in (15, 30, 60):
        kw = _happy_kwargs()
        kw["fps"] = fps
        cmd = build_ffmpeg_command(**kw)
        g_idx = cmd.index("-g")
        assert cmd[g_idx + 1] == str(fps), (
            f"expected -g {fps} for fps={fps}, got {cmd[g_idx + 1]}"
        )
        keyint_idx = cmd.index("-keyint_min")
        assert cmd[keyint_idx + 1] == str(fps)


def test_scene_cut_keyframes_disabled() -> None:
    """``-sc_threshold 0`` is required for predictable GOP cadence.

    Without it, libx264 will insert extra keyframes on scene cuts and the
    GOP becomes irregular, which can defeat the WebRTC join-latency budget
    if the encoder happens to drop a keyframe right after a reader joins.
    """
    cmd = build_ffmpeg_command(**_happy_kwargs())
    sc_idx = cmd.index("-sc_threshold")
    assert cmd[sc_idx + 1] == "0"


def test_redact_command_replaces_srt_url_only() -> None:
    cmd = build_ffmpeg_command(**_happy_kwargs())
    redacted = redact_command(cmd, _PLACEHOLDER_SRT_URL)

    # Other args preserved.
    assert "1280x720" in redacted
    assert "libx264" in redacted
    assert "mpegts" in redacted
    # Raw URL is gone.
    assert _PLACEHOLDER_SRT_URL not in redacted
    # Redaction marker present.
    assert "srt://<redacted>" in redacted


def test_redact_command_does_not_mutate_input() -> None:
    cmd = build_ffmpeg_command(**_happy_kwargs())
    original = list(cmd)
    redact_command(cmd, _PLACEHOLDER_SRT_URL)
    assert cmd == original


def test_redact_command_handles_url_with_extra_params() -> None:
    cmd = build_ffmpeg_command(**_happy_kwargs())
    # Even if downstream tooling rewrote the URL with extra params,
    # any element starting with srt:// must still get scrubbed.
    cmd_mutated = list(cmd)
    cmd_mutated[-1] = "srt://other.host:9999/?streamid=x"
    redacted = redact_command(cmd_mutated, _PLACEHOLDER_SRT_URL)
    assert all(not t.startswith("srt://other") for t in redacted)
    assert "srt://<redacted>" in redacted


def test_bad_codec_raises() -> None:
    kw = _happy_kwargs()
    kw["codec"] = "vp9"
    with pytest.raises(CommandBuildError):
        build_ffmpeg_command(**kw)


def test_unknown_encoder_raises() -> None:
    kw = _happy_kwargs()
    kw["encoder"] = "h264_mystery"
    with pytest.raises(CommandBuildError):
        build_ffmpeg_command(**kw)


def test_nonpositive_dimensions_raise() -> None:
    kw = _happy_kwargs()
    kw["width"] = 0
    with pytest.raises(CommandBuildError):
        build_ffmpeg_command(**kw)


def test_nonpositive_fps_raises() -> None:
    kw = _happy_kwargs()
    kw["fps"] = 0
    with pytest.raises(CommandBuildError):
        build_ffmpeg_command(**kw)


def test_empty_srt_url_raises() -> None:
    kw = _happy_kwargs()
    kw["srt_url"] = ""
    with pytest.raises(CommandBuildError):
        build_ffmpeg_command(**kw)


def test_command_starts_with_ffmpeg_and_loglevel_error() -> None:
    cmd = build_ffmpeg_command(**_happy_kwargs())
    assert cmd[0] == "ffmpeg"
    assert "-loglevel" in cmd
    assert "error" in cmd
