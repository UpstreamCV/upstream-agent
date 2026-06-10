"""Video source adapters for upstream-agent.

Each source implements :class:`VideoSource` and answers the same five
questions: can I be opened, what resolution am I producing, what FPS am
I producing, am I currently connected, and what was my last error.

The only supported source today is :class:`WebcamSource` (a local USB
or built-in camera). The :class:`VideoSource` ABC remains in place so a
future test-only file source can slot in without reshaping the runtime.
"""

from upstream_agent.sources.base import Frame, FrameInfo, VideoSource
from upstream_agent.sources.webcam import WebcamSource

__all__ = ["Frame", "FrameInfo", "VideoSource", "WebcamSource"]
