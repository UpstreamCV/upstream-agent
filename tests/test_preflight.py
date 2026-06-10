"""Tests for :mod:`upstream_agent.preflight` — the local limits pre-check."""

from __future__ import annotations

import pytest

from upstream_agent.api.schemas import BootstrapLimits
from upstream_agent.errors import PreflightError
from upstream_agent.preflight import plan_stream_config


def _plan(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "width": 1920,
        "height": 1080,
        "fps": 60,
        "codec": "h264",
        "bitrate_kbps": 6000,
        "latency_mode": "balanced",
        "limits": None,
        "source_type": "webcam",
    }
    base.update(overrides)
    return base


def _limits(**overrides: object) -> BootstrapLimits:
    base: dict[str, object] = {
        "max_fps": 30,
        "max_width": 1280,
        "max_height": 720,
        "max_bitrate_kbps": 3000,
        "allowed_codecs": ["h264"],
        "allowed_stream_types": ["webcam", "rtsp"],
    }
    base.update(overrides)
    return BootstrapLimits.model_validate(base)


def test_plan_passthrough_when_limits_none() -> None:
    planned = plan_stream_config(**_plan(limits=None))
    assert planned.width == 1920
    assert planned.height == 1080
    assert planned.fps == 60
    assert planned.bitrate_kbps == 6000
    assert not planned.was_adjusted


def test_plan_no_adjustments_within_limits() -> None:
    planned = plan_stream_config(
        **_plan(width=640, height=360, fps=15, bitrate_kbps=1000, limits=_limits())
    )
    assert (planned.width, planned.height, planned.fps) == (640, 360, 15)
    assert not planned.was_adjusted


def test_plan_clamps_resolution_fps_bitrate_and_records_adjustments() -> None:
    planned = plan_stream_config(**_plan(limits=_limits()))
    assert (planned.width, planned.height) == (1280, 720)
    assert planned.fps == 30
    assert planned.bitrate_kbps == 3000
    assert planned.adjustments == (
        "resolution 1920x1080 -> 1280x720",
        "fps 60 -> 30",
        "bitrate 6000kbps -> 3000kbps",
    )


def test_plan_skips_dimensions_the_server_did_not_report() -> None:
    sparse = BootstrapLimits.model_validate({"max_fps": 30})
    planned = plan_stream_config(**_plan(limits=sparse))
    assert planned.fps == 30
    assert planned.width == 1920  # no max_width reported -> untouched
    assert planned.adjustments == ("fps 60 -> 30",)


def test_plan_raises_on_disallowed_codec() -> None:
    with pytest.raises(PreflightError) as exc:
        plan_stream_config(**_plan(codec="av1", limits=_limits()))
    assert "codec 'av1'" in str(exc.value)
    assert "h264" in str(exc.value)


def test_plan_raises_on_disallowed_stream_type() -> None:
    with pytest.raises(PreflightError) as exc:
        plan_stream_config(
            **_plan(source_type="webcam", limits=_limits(allowed_stream_types=["rtsp"]))
        )
    assert "source type 'webcam'" in str(exc.value)
    assert "rtsp" in str(exc.value)


def test_plan_allow_list_checks_are_case_insensitive() -> None:
    planned = plan_stream_config(
        **_plan(
            codec="H264",
            source_type="WEBCAM",
            limits=_limits(allowed_codecs=["h264"], allowed_stream_types=["webcam"]),
        )
    )
    assert planned.codec == "H264"  # value preserved, only the check is folded
