"""FFmpeg encoder package.

For Milestone 4 this contains only the command-line builder. The
actual subprocess wrapper (used by Milestone 5+) will land alongside
it later.
"""

from upstream_agent.encoder.command_builder import (
    build_ffmpeg_command,
    redact_command,
)

__all__ = ["build_ffmpeg_command", "redact_command"]
