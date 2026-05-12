"""The ``upstream`` CLI.

Three subcommands today:

* ``upstream init`` — write a default config template to
  ``~/.upstream/config.yaml`` (or a path supplied via ``--config``).
* ``upstream status`` — show the current config and local state, with
  the API token always displayed as ``****<last4>``.
* ``upstream check --camera`` — local camera probe. Opens the
  configured webcam, reads one frame, reports actual resolution/FPS,
  and releases the device. Makes no API call.

``--version`` prints the package version and exits.

All exceptions of type :class:`~upstream_agent.errors.UpstreamError` are
caught at the top level, written to stderr as ``Error: <msg>``, and the
process exits with code 1.
"""

from __future__ import annotations

from pathlib import Path

import typer

from upstream_agent.config import (
    Config,
    load_config,
    resolve_config_path,
    write_default_config,
)
from upstream_agent.diagnostics.camera import CameraCheckResult, run_camera_check
from upstream_agent.errors import UpstreamError
from upstream_agent.logging_setup import redact_token, setup_logging
from upstream_agent.state import default_state_path, load_state
from upstream_agent.version import __version__

app = typer.Typer(
    name="upstream",
    help="UpStream agent CLI.",
    no_args_is_help=True,
    add_completion=False,
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"upstream {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(  # noqa: ARG001  (used via callback)
        False,
        "--version",
        callback=_print_version,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """UpStream agent CLI."""


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


@app.command("check")
def check_cmd(
    camera: bool = typer.Option(
        False,
        "--camera",
        help="Probe the configured camera (open, read one frame, release).",
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        help="Override the config file path (default: ~/.upstream/config.yaml).",
    ),
) -> None:
    """Run local diagnostics. Currently supports ``--camera``."""

    if not camera:
        typer.echo(
            "Specify what to check, e.g. `upstream check --camera`. "
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

    result = run_camera_check(cfg.camera)
    if not result.success:
        typer.echo(f"Error: {result.error}", err=True)
        raise typer.Exit(code=1)

    _print_camera_check_success(result)
