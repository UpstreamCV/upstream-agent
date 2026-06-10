"""The ``upstream`` CLI.

Three subcommands today:

* ``upstream init`` — write a default config template to
  ``~/.upstream/config.yaml`` (or a path supplied via ``--config``).
* ``upstream status`` — show the current config and local state, with
  the API token always displayed as ``****<last4>``.
* ``upstream check --camera`` — local camera probe. Opens the
  configured webcam, reads one frame, reports actual resolution/FPS,
  and releases the device. Makes no API call.
* ``upstream check --api`` — three-step API probe. Registers the agent,
  registers the camera, and requests a stream session. Stops before
  any streaming. Never prints the full token or SRT URL.
* ``upstream check --streaming`` — local streaming preflight. Probes
  FFmpeg/SRT/H264, obtains an approved SRT URL via the API flow,
  validates the URL, builds the FFmpeg command Milestone 5 will
  execute, then stops the temporary session. Does not stream.
* ``upstream run`` — live streaming. Opens the camera, requests a
  stream session, launches FFmpeg, and pipes BGR frames over SRT
  until ``Ctrl+C``. Cleanly stops FFmpeg, releases the camera, and
  stops the API session on exit.

``--version`` prints the package version and exits.

All exceptions of type :class:`~upstream_agent.errors.UpstreamError` are
caught at the top level, written to stderr as ``Error: <msg>``, and the
process exits with code 1.
"""

from __future__ import annotations

from pathlib import Path

import typer

from upstream_agent.branding import maybe_print_welcome
from upstream_agent.config import (
    Config,
    load_config,
    resolve_config_path,
    write_default_config,
)
from upstream_agent.diagnostics.api import ApiCheckResult, run_api_check
from upstream_agent.diagnostics.camera import CameraCheckResult, run_camera_check
from upstream_agent.diagnostics.streaming import (
    StreamingCheckResult,
    run_streaming_check,
)
from upstream_agent.errors import UpstreamError
from upstream_agent.logging_setup import redact_token, setup_logging
from upstream_agent.state import State, default_state_path, load_state, save_state
from upstream_agent.supervisor import run_agent
from upstream_agent.version import __version__

app = typer.Typer(
    name="upstream",
    help="UpStream agent CLI. Run with no arguments for interactive mode.",
    add_completion=False,
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"upstream {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    version: bool = typer.Option(  # noqa: ARG001  (used via callback)
        False,
        "--version",
        callback=_print_version,
        is_eager=True,
        help="Show version and exit.",
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        help="Override the config file path (interactive mode only).",
    ),
) -> None:
    """UpStream agent CLI.

    With no subcommand, opens the interactive terminal: account status,
    cameras, stream start/stop, and logs in one live session.
    """

    if ctx.invoked_subcommand is not None:
        return
    raise typer.Exit(code=_launch_interactive(config))


def _launch_interactive(config: Path | None) -> int:
    """Start the interactive shell with ring-buffered logging.

    The stderr log handler is disabled so background stream workers
    can't scroll log lines over the prompt; logs go to the rotating
    file and the in-memory ring buffer (the shell's ``logs`` command).
    """

    # Imported lazily: the interactive module pulls in Rich and the
    # supervisor stack, which subcommands like `upstream init` never need.
    from upstream_agent.interactive.shell import InteractiveShell  # noqa: PLC0415
    from upstream_agent.logging_setup import RingBufferHandler  # noqa: PLC0415

    level = "info"
    sensitive: tuple[str, ...] = ()
    try:
        cfg = load_config(config, allow_placeholder=True)
        level = cfg.logging.level
        if not cfg.api.is_placeholder_token:
            sensitive = (cfg.api.token,)
    except UpstreamError:
        # No config yet — the shell opens in "not configured" mode and
        # offers `login`.
        pass

    ring = RingBufferHandler()
    setup_logging(level, sensitive_values=sensitive, console=False, extra_handlers=(ring,))
    shell = InteractiveShell(config_path=config, ring=ring)
    return shell.run()


def main() -> None:
    maybe_print_welcome()
    app()


def _default_log_dir() -> Path:
    return Path.home() / ".upstream" / "logs"


@app.command("init")
def init_cmd(
    config: Path | None = typer.Option(
        None,
        "--config",
        help="Override the config file path (default: ~/.upstream/config.yaml).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite the config file if it already exists.",
    ),
) -> None:
    """Create a default config file at the resolved config path."""

    target = resolve_config_path(config)
    try:
        write_default_config(target, force=force)
    except UpstreamError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"Wrote default config to {target}")
    typer.echo("Next steps:")
    typer.echo(
        "  1. Edit the file and replace YOUR_API_TOKEN_HERE with your real token."
    )
    typer.echo(
        "  2. Never commit this file. It is gitignored by default in upstream-agent."
    )


def _print_config_block(cfg: Config, path: Path) -> None:
    placeholder_note = (
        "  (placeholder — set your real token)" if cfg.api.is_placeholder_token else ""
    )
    token_display = redact_token(cfg.api.token)
    typer.echo("Config:")
    typer.echo(f"  path:       {path}")
    typer.echo(
        f"  api:        {cfg.api.base_url}  (token {token_display}){placeholder_note}"
    )
    typer.echo(
        "  camera:     "
        f"{cfg.camera.name} ({cfg.camera.source_type}, source={cfg.camera.source}, "
        f"{cfg.camera.requested_width}x{cfg.camera.requested_height}@{cfg.camera.requested_fps})"
    )
    typer.echo(f"  stream:     {cfg.stream.latency_mode}, {cfg.stream.buffer_ms}ms buffer")
    typer.echo(f"  logging:    {cfg.logging.level}")


@app.command("status")
def status_cmd(
    config: Path | None = typer.Option(
        None,
        "--config",
        help="Override the config file path (default: ~/.upstream/config.yaml).",
    ),
) -> None:
    """Show local agent, camera, and session state."""

    typer.echo(f"upstream-agent {__version__}")
    typer.echo("")

    config_path = resolve_config_path(config)
    cfg: Config | None = None
    if not config_path.exists():
        typer.echo(f"No config found at {config_path}.")
        typer.echo("Run 'upstream init' to create one.")
    else:
        try:
            cfg = load_config(config_path, allow_placeholder=True)
        except UpstreamError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from None

        sensitive = (
            {cfg.api.token}
            if cfg.api.token and not cfg.api.is_placeholder_token
            else set()
        )
        setup_logging(
            level=cfg.logging.level,
            log_dir=_default_log_dir(),
            sensitive_values=sensitive,
        )
        _print_config_block(cfg, config_path)

    typer.echo("")
    state_path = default_state_path()
    try:
        state = load_state(state_path)
    except UpstreamError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    typer.echo("State:")
    typer.echo(f"  path:           {state_path}")
    typer.echo(f"  agent_id:       {state.agent_id or '<not registered>'}")
    typer.echo(f"  device_id:      {state.device_id}")
    typer.echo(f"  last_camera_id: {state.last_camera_id or '<none>'}")


def _format_fps(fps: float) -> str:
    if fps == 0.0:
        return "unknown"
    if fps.is_integer():
        return str(int(fps))
    return f"{fps:.2f}"


def _print_camera_check_success(result: CameraCheckResult) -> None:
    info = result.info
    assert info is not None  # success implies info is populated

    typer.echo("Camera opened successfully")
    typer.echo(f"Resolution: {info.width}x{info.height}")
    typer.echo(f"FPS: {_format_fps(info.fps)}")

    requested_fps_mismatch = info.fps != 0.0 and int(info.fps) != result.requested_fps
    if (info.width, info.height) != (
        result.requested_width,
        result.requested_height,
    ) or requested_fps_mismatch:
        typer.echo("")
        typer.echo(
            f"Note: requested {result.requested_width}x{result.requested_height} "
            f"@ {result.requested_fps}, device negotiated "
            f"{info.width}x{info.height} @ {_format_fps(info.fps)}."
        )


def _print_streaming_check_success(result: StreamingCheckResult) -> None:
    typer.echo("Streaming preflight successful")
    if result.ffmpeg_version:
        typer.echo(f"FFmpeg: found, version {result.ffmpeg_version}")
    else:
        typer.echo("FFmpeg: found, version unknown")
    typer.echo("SRT support: yes")
    encoder_label = result.h264_encoder or "yes"
    typer.echo(f"H264 encoder: yes ({encoder_label})")
    typer.echo("API stream approval: yes")
    typer.echo(
        f"Approved config: {result.approved_codec}, "
        f"{result.approved_width}x{result.approved_height} @ "
        f"{result.approved_fps} FPS, "
        f"{result.approved_bitrate_kbps} kbps, "
        f"{result.approved_latency_mode}"
    )
    typer.echo(
        f"SRT URL received: {'yes' if result.srt_url_present else 'no'}"
    )
    typer.echo(
        f"FFmpeg command build: {'yes' if result.command_build_success else 'no'}"
    )


def _print_api_check_success(result: ApiCheckResult) -> None:
    typer.echo("API check successful")
    typer.echo(f"Agent registered: {result.agent_id}")
    typer.echo(f"Camera registered: {result.camera_id}")
    typer.echo(f"Stream session approved: {result.session_id}")
    typer.echo(
        f"Approved config: {result.approved_codec}, "
        f"{result.approved_width}x{result.approved_height} @ "
        f"{result.approved_fps} FPS, "
        f"{result.approved_bitrate_kbps} kbps, "
        f"{result.approved_latency_mode}"
    )
    typer.echo(
        f"SRT URL received: {'yes' if result.srt_url_present else 'no'}"
    )


@app.command("check")
def check_cmd(
    camera: bool = typer.Option(
        False,
        "--camera",
        help="Probe the configured camera (open, read one frame, release).",
    ),
    api: bool = typer.Option(
        False,
        "--api",
        help=(
            "Probe the API: register agent, register camera, request "
            "stream session. Stops before streaming."
        ),
    ),
    streaming: bool = typer.Option(
        False,
        "--streaming",
        help=(
            "Streaming preflight: FFmpeg, SRT support, H264 encoder, "
            "API approval, SRT URL validity, FFmpeg command build. "
            "Does NOT stream."
        ),
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        help="Override the config file path (default: ~/.upstream/config.yaml).",
    ),
) -> None:
    """Run local diagnostics. Supports ``--camera``, ``--api``, and ``--streaming``."""

    if not camera and not api and not streaming:
        typer.echo(
            "Specify what to check, e.g. `upstream check --camera`, "
            "`upstream check --api`, or `upstream check --streaming`. "
            "See `upstream check --help` for options.",
            err=True,
        )
        raise typer.Exit(code=2)

    config_path = resolve_config_path(config)
    if not config_path.exists():
        typer.echo(f"No config found at {config_path}.", err=True)
        typer.echo("Run 'upstream init' to create one.", err=True)
        raise typer.Exit(code=1)

    try:
        cfg = load_config(config_path, allow_placeholder=True)
    except UpstreamError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    needs_api = api or streaming
    if needs_api and cfg.api.is_placeholder_token:
        typer.echo(
            "Error: API token is still a placeholder. Edit "
            f"{config_path} (api.token) and re-run.",
            err=True,
        )
        raise typer.Exit(code=1)

    sensitive = (
        {cfg.api.token}
        if cfg.api.token and not cfg.api.is_placeholder_token
        else set()
    )
    setup_logging(
        level=cfg.logging.level,
        log_dir=_default_log_dir(),
        sensitive_values=sensitive,
    )

    overall_ok = True
    printed_a_block = False

    if camera:
        cam_result = run_camera_check(cfg.camera)
        if not cam_result.success:
            typer.echo(f"Error: {cam_result.error}", err=True)
            overall_ok = False
        else:
            _print_camera_check_success(cam_result)
            printed_a_block = True

    if api:
        if printed_a_block:
            typer.echo("")
        state = load_state(default_state_path())

        def _persist(updated: State) -> None:
            save_state(updated, default_state_path())

        api_result, updated_state = run_api_check(
            cfg, state, save_progress=_persist
        )
        # Final write — idempotent; covers the success path where
        # save_progress already wrote the last update.
        save_state(updated_state, default_state_path())

        if not api_result.success:
            typer.echo(
                f"Error ({api_result.failed_step}): {api_result.error}",
                err=True,
            )
            overall_ok = False
        else:
            _print_api_check_success(api_result)
            printed_a_block = True
            if api_result.stop_warning is not None:
                # Cleanup stop failed. The check itself succeeded, so
                # exit stays 0 — but warn the operator so they know
                # the session may linger until it expires.
                typer.echo(
                    f"Warning: failed to stop stream session after check: "
                    f"{api_result.stop_warning}",
                    err=True,
                )

    if streaming:
        if printed_a_block:
            typer.echo("")
        state = load_state(default_state_path())

        def _persist_streaming(updated: State) -> None:
            save_state(updated, default_state_path())

        stream_result, updated_state = run_streaming_check(
            cfg, state, save_progress=_persist_streaming
        )
        save_state(updated_state, default_state_path())

        if not stream_result.success:
            typer.echo(
                f"Error ({stream_result.failed_step}): {stream_result.error}",
                err=True,
            )
            overall_ok = False
            if stream_result.stop_warning is not None:
                typer.echo(
                    f"Warning: failed to stop stream session after check: "
                    f"{stream_result.stop_warning}",
                    err=True,
                )
        else:
            _print_streaming_check_success(stream_result)
            if stream_result.stop_warning is not None:
                typer.echo(
                    f"Warning: failed to stop stream session after check: "
                    f"{stream_result.stop_warning}",
                    err=True,
                )

    if not overall_ok:
        raise typer.Exit(code=1)


@app.command("run")
def run_cmd(
    config: Path | None = typer.Option(
        None,
        "--config",
        help="Override the config file path (default: ~/.upstream/config.yaml).",
    ),
) -> None:
    """Stream video to UpStream. Press Ctrl+C to stop."""

    config_path = resolve_config_path(config)
    if not config_path.exists():
        typer.echo(f"No config found at {config_path}.", err=True)
        typer.echo("Run 'upstream init' to create one.", err=True)
        raise typer.Exit(code=1)

    try:
        cfg = load_config(config_path, allow_placeholder=True)
    except UpstreamError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if cfg.api.is_placeholder_token:
        typer.echo(
            "Error: API token is still a placeholder. Edit "
            f"{config_path} (api.token) and re-run.",
            err=True,
        )
        raise typer.Exit(code=1)

    sensitive = (
        {cfg.api.token}
        if cfg.api.token and not cfg.api.is_placeholder_token
        else set()
    )
    setup_logging(
        level=cfg.logging.level,
        log_dir=_default_log_dir(),
        sensitive_values=sensitive,
    )

    state = load_state(default_state_path())

    def _persist_run(updated: State) -> None:
        save_state(updated, default_state_path())

    def _on_event(stage: str, message: str) -> None:
        typer.echo(message)

    typer.echo("Starting UpStream agent...")
    try:
        _result, updated_state = run_agent(
            cfg,
            state,
            save_progress=_persist_run,
            on_event=_on_event,
        )
    except KeyboardInterrupt:
        # The supervisor's finally block already cleaned up and emitted
        # the shutdown lines; just exit 0.
        save_state(load_state(default_state_path()), default_state_path())
        raise typer.Exit(code=0) from None
    except UpstreamError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    save_state(updated_state, default_state_path())
