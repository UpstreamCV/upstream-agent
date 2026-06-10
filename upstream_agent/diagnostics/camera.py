"""Camera-check diagnostic.

:func:`run_camera_check` opens the configured camera, reads one frame,
captures the effective resolution/FPS, then releases the device — all
inside a context manager so ``release()`` runs even on read failure.

The function never raises; instead it returns a :class:`CameraCheckResult`
that the CLI translates to stdout/stderr and exit codes. This lets the
diagnostic be unit-tested without :mod:`typer` in the loop.
"""

from __future__ import annotations

from dataclasses import dataclass

from upstream_agent.config import CameraConfig
from upstream_agent.errors import CameraError
from upstream_agent.sources.base import FrameInfo
from upstream_agent.sources.webcam import WebcamSource


@dataclass(frozen=True)
class CameraCheckResult:
    """Structured outcome of a camera probe."""

    success: bool
    source_type: str
    source: int | str
    requested_width: int
    requested_height: int
    requested_fps: int
    info: FrameInfo | None
    error: str | None


def run_camera_check(cfg: CameraConfig) -> CameraCheckResult:
    """Probe the configured camera and report what happened.

    Always returns a result; never raises. The result's ``success`` flag
    determines the CLI exit code.
    """

    base_fields = {
        "source_type": cfg.source_type,
        "source": cfg.source,
        "requested_width": cfg.requested_width,
        "requested_height": cfg.requested_height,
        "requested_fps": cfg.requested_fps,
    }

    try:
        with WebcamSource(cfg) as src:
            src.read_frame()
            info = src.info
    except CameraError as exc:
        return CameraCheckResult(success=False, info=None, error=str(exc), **base_fields)

    return CameraCheckResult(success=True, info=info, error=None, **base_fields)
