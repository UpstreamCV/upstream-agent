"""Video source adapters for upstream-agent.

Each source implements :class:`VideoSource` and answers the same five
questions: can I be opened, what resolution am I producing, what FPS am
I producing, am I currently connected, and what was my last error.

Milestone 2 ships only :class:`WebcamSource`. Future milestones add
RTSP, video-file, and IP-camera sources alongside the webcam.
"""

from upstream_agent.sources.base import Frame, FrameInfo, VideoSource
from upstream_agent.sources.webcam import WebcamSource

__all__ = ["Frame", "FrameInfo", "VideoSource", "WebcamSource"]
