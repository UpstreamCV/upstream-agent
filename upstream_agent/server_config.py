"""Build a local :class:`~upstream_agent.config.CameraConfig` from a
server-side camera record.

Cameras are configured on the web (or registered by an agent) and pulled
down via ``GET /v1/agents/bootstrap``; the server is the source of truth
for the stream config (resolution / fps / source type). What the server
*cannot* know is how to reach the device from this machine — that is the
``device_path`` convention the agent itself established at registration
time:

* ``webcam:<index>``   — local capture device index
* ``rtsp:<url>``       — network camera URL

This module parses that convention back into a ``CameraConfig`` so the
interactive shell can start a stream for any camera the account exposes,
without a local YAML camera section.
"""

from __future__ import annotations

from upstream_agent.api.schemas import Camera
from upstream_agent.config import CameraConfig
from upstream_agent.errors import ConfigError

# Source types this agent build can actually CAPTURE. The account's
# allowed_stream_types may be broader (e.g. include "rtsp") — that's a
# server-side allow-list; locally only webcam capture is implemented
# (rtsp was removed as never-implemented; see commit-log 2026-06-10).
_LOCAL_SOURCE_TYPES = ("webcam",)
_KNOWN_PATH_PREFIXES = ("webcam", "rtsp")


def camera_config_from_server(camera: Camera) -> CameraConfig:
    """Map a bootstrap camera record onto the local ``CameraConfig``.

    Raises :class:`ConfigError` with an actionable message when the
    record can't be mapped — no declared source, an unsupported source
    type, or a malformed device path.
    """

    source_type, source = _parse_source(camera)

    return CameraConfig(
        name=camera.name or f"camera-{camera.id[:8]}",
        source_type=source_type,  # type: ignore[arg-type]
        source=source,
        requested_width=camera.default_width or 1280,
        requested_height=camera.default_height or 720,
        requested_fps=camera.default_fps or 30,
    )


def _parse_source(camera: Camera) -> tuple[str, int | str]:
    source_type = camera.source_type
    device_path = camera.device_path

    if device_path:
        prefix, sep, rest = device_path.partition(":")
        if sep and prefix in _KNOWN_PATH_PREFIXES and rest:
            inferred_type = source_type or prefix
            if inferred_type != prefix:
                raise ConfigError(
                    f"Camera '{camera.name or camera.id}' declares "
                    f"source_type '{inferred_type}' but its device path is "
                    f"'{prefix}:...'. Fix the camera's configuration on the "
                    "dashboard."
                )
            if prefix not in _LOCAL_SOURCE_TYPES:
                raise ConfigError(
                    f"Camera '{camera.name or camera.id}' is a '{prefix}' "
                    "source, which this agent build cannot capture yet "
                    f"(supported locally: {', '.join(_LOCAL_SOURCE_TYPES)})."
                )
            try:
                return "webcam", int(rest)
            except ValueError:
                raise ConfigError(
                    f"Camera '{camera.name or camera.id}' has a webcam "
                    f"device path with a non-integer index: {rest!r}. "
                    "Expected e.g. 'webcam:0'."
                ) from None

    if source_type and source_type not in _LOCAL_SOURCE_TYPES:
        raise ConfigError(
            f"Camera '{camera.name or camera.id}' uses source type "
            f"'{source_type}', which this agent build does not support "
            f"(supported locally: {', '.join(_LOCAL_SOURCE_TYPES)})."
        )

    raise ConfigError(
        f"Camera '{camera.name or camera.id}' has no usable device path. "
        "Set it on the dashboard as 'webcam:<index>' (e.g. webcam:0) or "
        "'rtsp:<url>'."
    )
