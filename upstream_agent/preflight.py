"""Local pre-check of the requested stream config against account limits.

After :meth:`UpstreamApiClient.get_bootstrap` returns the account's
limits, :func:`plan_stream_config` clamps the locally requested
width/height/fps/bitrate down to those caps so the session-start request
is something the server will approve, and records each adjustment as a
human-readable string for display::

    Requested: 1920x1080@60fps
    Approved:  1280x720@30fps
    Action:    using server-approved config

Two dimensions cannot be clamped — codec and source type are allowed or
they aren't — so those raise :class:`~upstream_agent.errors.PreflightError`
with the allow-list in the message.

This is a convenience layer only. The server re-validates every request
(`check_camera_defaults` / `check_session_start` / stream-type checks on
the api-server) and remains the source of truth; when bootstrap is
unavailable (older server, network blip) the planner passes the local
config through unchanged and lets the server decide.
"""

from __future__ import annotations

from dataclasses import dataclass

from upstream_agent.api.schemas import BootstrapLimits
from upstream_agent.errors import PreflightError


@dataclass(frozen=True)
class PlannedStreamConfig:
    """The config the agent will actually request, post-clamp."""

    width: int
    height: int
    fps: int
    codec: str
    bitrate_kbps: int
    latency_mode: str
    # Human-readable record of every clamp applied, e.g.
    # ("resolution 1920x1080 -> 1280x720", "fps 60 -> 30"). Empty when
    # the local request was already within limits (or limits unknown).
    adjustments: tuple[str, ...] = ()

    @property
    def was_adjusted(self) -> bool:
        return bool(self.adjustments)


def plan_stream_config(
    *,
    width: int,
    height: int,
    fps: int,
    codec: str,
    bitrate_kbps: int,
    latency_mode: str,
    limits: BootstrapLimits | None,
    source_type: str | None = None,
) -> PlannedStreamConfig:
    """Clamp the requested config to account limits.

    ``limits=None`` (bootstrap unavailable) passes everything through
    unchanged. Numeric dimensions clamp to ``min(requested, limit)``;
    codec and source type are allow-lists and raise
    :class:`PreflightError` when not allowed, since no downshift exists.
    """

    if limits is None:
        return PlannedStreamConfig(
            width=width,
            height=height,
            fps=fps,
            codec=codec,
            bitrate_kbps=bitrate_kbps,
            latency_mode=latency_mode,
        )

    _check_allowed(
        kind="source type",
        value=source_type,
        allowed=limits.allowed_stream_types,
    )
    _check_allowed(kind="codec", value=codec, allowed=limits.allowed_codecs)

    adjustments: list[str] = []

    planned_width, planned_height = width, height
    if (limits.max_width is not None and width > limits.max_width) or (
        limits.max_height is not None and height > limits.max_height
    ):
        planned_width = min(width, limits.max_width or width)
        planned_height = min(height, limits.max_height or height)
        adjustments.append(
            f"resolution {width}x{height} -> {planned_width}x{planned_height}"
        )

    planned_fps = fps
    if limits.max_fps is not None and fps > limits.max_fps:
        planned_fps = limits.max_fps
        adjustments.append(f"fps {fps} -> {planned_fps}")

    planned_bitrate = bitrate_kbps
    if limits.max_bitrate_kbps is not None and bitrate_kbps > limits.max_bitrate_kbps:
        planned_bitrate = limits.max_bitrate_kbps
        adjustments.append(f"bitrate {bitrate_kbps}kbps -> {planned_bitrate}kbps")

    return PlannedStreamConfig(
        width=planned_width,
        height=planned_height,
        fps=planned_fps,
        codec=codec,
        bitrate_kbps=planned_bitrate,
        latency_mode=latency_mode,
        adjustments=tuple(adjustments),
    )


def _check_allowed(*, kind: str, value: str | None, allowed: list[str] | None) -> None:
    """Allow-list check; ``value=None`` or ``allowed=None`` means "no opinion"."""

    if value is None or allowed is None:
        return
    if value.lower() not in {entry.lower() for entry in allowed}:
        raise PreflightError(
            f"This account does not allow {kind} '{value}' "
            f"(allowed: {', '.join(sorted(allowed))}). "
            "Update your config or ask an account owner to change the "
            "account's limits."
        )
