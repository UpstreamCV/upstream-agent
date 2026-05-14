"""Build the FFmpeg command Milestone 5 will execute.

The command returned by :func:`build_ffmpeg_command` is an argv
``list[str]`` — never a shell string. M4 only verifies that the
command can be built from approved config; M5 will actually launch
the subprocess.

Input handling is intentionally provisional: this builder uses
``-f rawvideo -pix_fmt bgr24 ... -i pipe:0``, matching the most likely
M5 path (OpenCV reads frames and pipes BGR bytes into FFmpeg's stdin).
A future milestone may swap in ``avfoundation`` / ``v4l2`` / etc. once
the source layer decides whether the agent pipes frames or hands the
camera directly to FFmpeg.

Only H264 is supported in M4. The encoder name is supplied by the
caller (typically picked by :func:`upstream_agent.diagnostics.ffmpeg_check.probe_ffmpeg`),
so the agent does not hard-code ``libx264`` — it can also use
``h264_videotoolbox`` on Apple Silicon or other H264 encoders.

:func:`redact_command` returns a fresh list with the SRT URL replaced
by the literal string ``srt://<redacted>``. The original list is not
mutated. The redacted form is the only safe shape to print, log, or
attach to a support email.
"""

from __future__ import annotations

from upstream_agent.errors import CommandBuildError

_SUPPORTED_CODEC: str = "h264"

# Encoders the builder recognises as H264. Kept in sync with the
# detection priority in :mod:`upstream_agent.diagnostics.ffmpeg_check`.
_SUPPORTED_H264_ENCODERS: frozenset[str] = frozenset(
    {
        "libx264",
        "h264_videotoolbox",
        "h264_nvenc",
        "h264_qsv",
        "h264_amf",
    }
)

_REDACTED_SRT_URL: str = "srt://<redacted>"


def build_ffmpeg_command(
    *,
    width: int,
    height: int,
    fps: int,
    bitrate_kbps: int,
    codec: str,
    encoder: str,
    latency_mode: str,  # noqa: ARG001  (reserved for future preset tuning)
    srt_url: str,
) -> list[str]:
    """Construct the FFmpeg argv list for streaming over SRT.

    Raises :class:`CommandBuildError` if the codec is not H264, the
    encoder is not one of the recognised H264 encoders, or any
    dimension is non-positive.
    """

    if codec.lower() != _SUPPORTED_CODEC:
        raise CommandBuildError(
            f"Unsupported codec '{codec}'. This build only emits H264."
        )
    if encoder not in _SUPPORTED_H264_ENCODERS:
        raise CommandBuildError(
            f"Unsupported FFmpeg H264 encoder '{encoder}'."
        )
    if width <= 0 or height <= 0:
        raise CommandBuildError("Width and height must be positive.")
    if fps <= 0:
        raise CommandBuildError("FPS must be positive.")
    if bitrate_kbps <= 0:
        raise CommandBuildError("Bitrate must be positive.")
    if not isinstance(srt_url, str) or not srt_url:
        raise CommandBuildError("SRT URL must be a non-empty string.")

    # Provisional input — M5 will swap this for the source-specific input
    # once the OpenCV-pipe vs FFmpeg-native-input decision is settled.
    input_args = [
        "-f", "rawvideo",
        "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}",
        "-r", str(fps),
        "-i", "pipe:0",
    ]

    encode_args = [
        "-c:v", encoder,
        "-preset", "veryfast",
        "-tune", "zerolatency",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-b:v", f"{bitrate_kbps}k",
    ]

    output_args = ["-f", "mpegts", srt_url]

    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        *input_args,
        *encode_args,
        *output_args,
    ]


def redact_command(cmd: list[str], srt_url: str) -> list[str]:
    """Return a copy of ``cmd`` with the SRT URL replaced.

    Replacement rules:

    * Any element exactly equal to ``srt_url`` becomes ``srt://<redacted>``.
    * Any other element starting with ``srt://`` is also redacted, in case
      the URL got transformed (e.g. with appended query parameters).

    Does not mutate the input list.
    """

    redacted: list[str] = []
    for token in cmd:
        if token == srt_url or token.startswith("srt://"):
            redacted.append(_REDACTED_SRT_URL)
        else:
            redacted.append(token)
    return redacted
