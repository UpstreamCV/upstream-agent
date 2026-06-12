"""The interactive ``upstream`` terminal.

A line-oriented REPL (deliberately not a full-screen TUI yet) that keeps
the server as the source of truth: on connect it calls
``GET /v1/agents/bootstrap`` and renders account, limits, cameras, and
active sessions from the response. Streams run on background
:class:`~upstream_agent.interactive.worker.StreamWorker` threads, so the
prompt stays responsive while video flows; their logs land in the ring
buffer (``logs`` command) instead of scrolling over the prompt.

Token handling: the ``login`` command reads the API key with a hidden
prompt, verifies it against the server (bootstrap call) before saving,
persists it via :func:`upstream_agent.config.save_api_token`, and never
echoes or logs it.

Everything injectable (``input_fn``, ``password_fn``, ``client_factory``,
``run_fn``, Rich ``Console``) so tests can drive a full session without
a TTY, a server, or a camera.
"""

from __future__ import annotations

import getpass
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from upstream_agent.api.client import UpstreamApiClient
from upstream_agent.api.schemas import (
    Agent,
    AgentRegisterRequest,
    BootstrapResponse,
    Camera,
)
from upstream_agent.branding import (
    BRAND_BLUE,
    BRAND_GOLD,
    BRAND_GREEN,
    logo_text,
)
from upstream_agent.config import Config, load_config, save_api_token
from upstream_agent.diagnostics.api import (
    _agent_name_from,
    _safe_os,
    _safe_python_version,
)
from upstream_agent.errors import (
    ApiConnectionError,
    ApiError,
    ConfigError,
    UpstreamError,
)
from upstream_agent.interactive.worker import StreamWorker, WorkerRegistry
from upstream_agent.logging_setup import RingBufferHandler, redact_token
from upstream_agent.server_config import camera_config_from_server
from upstream_agent.state import State, load_state, save_state
from upstream_agent.supervisor import run_agent
from upstream_agent.update_check import (
    UpdateStatus,
    VersionPolicy,
    evaluate_policy,
    update_notice,
)
from upstream_agent.version import __version__

logger = logging.getLogger(__name__)

# Brand styling (see upstream_agent.branding): green is the main color —
# logo, borders, headings, healthy states; blue carries information;
# gold marks highlights and in-between states. Errors stay red.
_G = BRAND_GREEN
_B = BRAND_BLUE
_GOLD = BRAND_GOLD

_WORKER_STATE_STYLES: dict[str, str] = {
    "streaming": f"bold {_G}",
    "starting": _GOLD,
    "reconnecting": _GOLD,
    "error": "bold red",
    "stopped": _B,
    "idle": _B,
}


def _state_markup(state: str) -> str:
    style = _WORKER_STATE_STYLES.get(state, _B)
    return f"[{style}]{state}[/]"


def _style_kv_table(table: Table) -> None:
    """Label column in brand green, value columns in brand blue."""

    if not table.columns:
        return
    table.columns[0].style = _G
    for column in table.columns[1:]:
        column.style = _B


_HELP_LINES: tuple[tuple[str, str], ...] = (
    ("status", "Connection, account, and stream overview"),
    ("account", "Account details and usage"),
    ("limits", "Account limits (resolution, fps, stream types, ...)"),
    ("cameras", "Cameras configured for this account (from the server)"),
    ("streams", "Local stream workers and server-side active sessions"),
    ("start \\[camera]", "Start streaming a camera (default: first available)"),
    ("stop \\[camera]", "Stop a running stream"),
    ("logs \\[n]", "Show the last n log lines (default 50)"),
    ("login", "Enter / replace the API token"),
    ("refresh", "Re-fetch account status from the server"),
    ("help", "This list"),
    ("quit", "Stop streams and exit"),
)


class InteractiveShell:
    """Line-oriented interactive session. See module docstring."""

    def __init__(
        self,
        *,
        config_path: Path | None = None,
        state_path: Path | None = None,
        ring: RingBufferHandler | None = None,
        registry: WorkerRegistry | None = None,
        client_factory: Callable[..., UpstreamApiClient] | None = None,
        run_fn: Callable[..., Any] = run_agent,
        input_fn: Callable[[str], str] = input,
        password_fn: Callable[[str], str] = getpass.getpass,
        console: Console | None = None,
    ) -> None:
        self._config_path = config_path
        self._state_path = state_path
        self._ring = ring or RingBufferHandler()
        self._registry = registry or WorkerRegistry()
        self._client_factory = client_factory or (
            lambda base_url, token: UpstreamApiClient(base_url, token)
        )
        self._run_fn = run_fn
        # Update M2: show the version notice at most once per shell session.
        self._update_notice_shown = False
        self._input = input_fn
        self._password = password_fn
        self._console = console or Console()

        self._cfg: Config | None = None
        self._state: State = load_state(state_path)
        self._state_lock = threading.Lock()
        self._bootstrap: BootstrapResponse | None = None
        self._connected = False

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> int:
        self._load_config()
        self._console.print(logo_text())
        self._console.print(
            Panel.fit(
                f"[bold {_G}]UpStream agent[/] [{_B}]v{__version__}[/] — interactive mode\n"
                f"Type [bold {_G}]help[/] for commands, [bold {_G}]quit[/] to exit.",
                border_style=_G,
            )
        )

        if not self._has_usable_token():
            self._console.print(
                f"[{_GOLD}]No API token configured.[/] "
                f"Type [bold {_G}]login[/] to enter one (created on the "
                "dashboard under API Tokens)."
            )
        else:
            self.cmd_refresh()
            if self._connected:
                self.cmd_status()

        while True:
            try:
                line = self._input("upstream> ").strip()
            except (EOFError, KeyboardInterrupt):
                self._console.print()
                return self._quit()

            if not line:
                continue
            parts = line.split(maxsplit=1)
            command, arg = parts[0].lower(), (parts[1] if len(parts) > 1 else "")

            if command in ("quit", "exit", "q"):
                return self._quit()
            handler = getattr(self, f"cmd_{command}", None)
            if handler is None:
                self._console.print(
                    f"Unknown command: [{_GOLD}]{command}[/]. "
                    f"Type [bold {_G}]help[/]."
                )
                continue
            try:
                if arg:
                    handler(arg)
                else:
                    handler()
            except UpstreamError as exc:
                self._console.print(f"[red]Error:[/red] {exc}")
            except Exception:  # pragma: no cover - keep the REPL alive
                logger.exception("command %r raised", command)
                self._console.print(
                    f"[red]Unexpected error running '{command}' — see logs.[/red]"
                )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def cmd_help(self) -> None:
        table = Table(show_header=False, box=None, padding=(0, 2))
        for name, desc in _HELP_LINES:
            table.add_row(f"[bold {_G}]{name}[/]", f"[{_B}]{desc}[/]")
        self._console.print(table)

    def cmd_login(self) -> None:
        token = self._password("API token (input hidden): ").strip()
        if not token:
            self._console.print("No token entered; aborting login.")
            return
        if not token.startswith("ups_") or token.count("_") < 3:
            self._console.print(
                "[red]That doesn't look like an UpStream token[/red] "
                "(expected ups_<env>_<id>_<secret>). Not saved."
            )
            return

        base_url = (
            self._cfg.api.base_url if self._cfg else "https://api.upstreamcv.com"
        )
        try:
            with self._client_factory(base_url, token) as client:
                boot = client.get_bootstrap()
        except ApiError as exc:
            self._console.print(
                f"[red]Token rejected by {base_url}:[/red] {exc}\nNot saved."
            )
            return

        path = save_api_token(token, self._config_path)
        self._load_config()
        self._bootstrap = boot
        self._connected = True
        account = boot.account.name if boot.account and boot.account.name else "account"
        self._console.print(
            f"[bold {_G}]Connected to {account}[/] as token "
            f"[{_B}]{redact_token(token)}[/] (saved to [{_B}]{path}[/])."
        )
        self._maybe_print_update_notice()
        self.cmd_status()

    def _maybe_print_update_notice(self) -> None:
        """Update M2 (notify-only): one short, dismissable notice per shell
        session when the server's bootstrap policy says we're behind. Dev
        builds / missing policy / offline are silent."""
        if self._update_notice_shown or self._bootstrap is None:
            return
        msg = self._bootstrap.agent_version_policy
        if msg is None:
            return
        policy = VersionPolicy(
            latest_version=msg.latest_version,
            minimum_supported_version=msg.minimum_supported_version,
            severity_hint=msg.severity_hint,
            release_notes_url=msg.release_notes_url,
            install_hint=msg.install_hint,
        )
        status = evaluate_policy(__version__, policy)
        notice = update_notice(__version__, policy, status)
        if notice is None:
            return
        self._update_notice_shown = True
        color = "red" if status is UpdateStatus.UPDATE_REQUIRED else _GOLD
        for line in notice.splitlines():
            self._console.print(f"[{color}]{line}[/]")

    def cmd_refresh(self) -> None:
        if not self._has_usable_token():
            self._console.print(
                f"Not configured — type [bold {_G}]login[/] first."
            )
            return
        assert self._cfg is not None
        try:
            with self._new_client() as client:
                self._bootstrap = client.get_bootstrap()
        except ApiConnectionError as exc:
            self._connected = False
            self._console.print(
                f"[red]Disconnected:[/red] {exc} — type "
                f"[bold {_G}]refresh[/] to retry."
            )
            return
        except ApiError as exc:
            self._connected = False
            self._console.print(f"[red]Server error:[/red] {exc}")
            return
        self._connected = True
        self._maybe_print_update_notice()

    def cmd_status(self) -> None:
        boot = self._bootstrap
        lines: list[str] = []
        if not self._has_usable_token():
            lines.append(
                f"[{_GOLD}]Not configured[/] — type [bold {_G}]login[/]"
            )
        elif not self._connected or boot is None:
            lines.append(
                f"[red]Disconnected from API[/red] — type [bold {_G}]refresh[/] to retry"
            )
        else:
            account = boot.account
            if account is not None:
                label = account.name or account.slug or account.id
                plan = f" [{_GOLD}]({account.plan})[/]" if account.plan else ""
                lines.append(f"Account: [bold {_G}]{label}[/]{plan}")
            agent = self._my_agent()
            if agent is not None and agent.name:
                lines.append(f"Agent: [{_B}]{agent.name}[/]")
            lines.append(f"Status: [bold {_G}]Connected[/]")
            usage, limits = boot.usage, boot.limits
            if usage is not None and limits is not None:
                active = usage.active_session_count
                cap = limits.max_concurrent_sessions
                if active is not None and cap is not None:
                    lines.append(f"Streams: [{_GOLD}]{active} / {cap}[/] active")
                cams = usage.camera_count
                cam_cap = limits.max_cameras
                if cams is not None and cam_cap is not None:
                    lines.append(f"Cameras: [{_GOLD}]{cams} / {cam_cap}[/]")
                if usage.monthly_minutes_used is not None:
                    quota = limits.monthly_minutes_quota
                    suffix = f" / {quota}" if quota is not None else ""
                    lines.append(
                        f"Minutes this month: "
                        f"[{_GOLD}]{usage.monthly_minutes_used}{suffix}[/]"
                    )
            if limits is not None:
                if limits.max_width and limits.max_height and limits.max_fps:
                    lines.append(
                        f"Max stream config: [{_B}]{limits.max_width}x"
                        f"{limits.max_height} @ {limits.max_fps}fps[/]"
                    )
                if limits.allowed_stream_types:
                    lines.append(
                        f"Allowed source types: [{_B}]"
                        + ", ".join(limits.allowed_stream_types)
                        + "[/]"
                    )

        statuses = self._registry.statuses()
        if statuses:
            lines.append("")
            lines.append("Local workers:")
            for st in statuses:
                detail = f" [{_B}]— {st.last_message}[/]" if st.last_message else ""
                lines.append(f"  {st.name}: {_state_markup(st.state)}{detail}")

        self._console.print(
            Panel(
                "\n".join(lines),
                title=f"[bold {_G}]status[/]",
                border_style=_G,
            )
        )

    def cmd_account(self) -> None:
        if not self._require_bootstrap():
            return
        assert self._bootstrap is not None
        account = self._bootstrap.account
        token = self._bootstrap.token
        table = Table(show_header=False, box=None, padding=(0, 2))
        if account is not None:
            table.add_row("Name", account.name or "—")
            table.add_row("Slug", account.slug or "—")
            table.add_row("Plan", account.plan or "—")
            table.add_row("Status", account.status or "—")
        if token is not None:
            table.add_row("Token", f"****{token.last_four}" if token.last_four else "—")
            table.add_row("Scopes", ", ".join(token.scopes) or "—")
            table.add_row("Token expires", token.expires_at or "never")
        usage = self._bootstrap.usage
        if usage is not None:
            table.add_row("Cameras", str(usage.camera_count))
            table.add_row("Active sessions", str(usage.active_session_count))
            table.add_row("Minutes this month", str(usage.monthly_minutes_used))
        _style_kv_table(table)
        self._console.print(
            Panel(table, title=f"[bold {_G}]account[/]", border_style=_G)
        )

    def cmd_limits(self) -> None:
        if not self._require_bootstrap():
            return
        assert self._bootstrap is not None
        limits = self._bootstrap.limits
        if limits is None:
            self._console.print("The server did not report limits.")
            return
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_row("Max cameras", str(limits.max_cameras))
        table.add_row("Max concurrent streams", str(limits.max_concurrent_sessions))
        table.add_row(
            "Max resolution", f"{limits.max_width}x{limits.max_height}"
        )
        table.add_row("Max FPS", str(limits.max_fps))
        table.add_row("Max bitrate (kbps)", str(limits.max_bitrate_kbps))
        table.add_row("Allowed codecs", ", ".join(limits.allowed_codecs or []) or "—")
        table.add_row(
            "Allowed source types",
            ", ".join(limits.allowed_stream_types or []) or "—",
        )
        table.add_row("Monthly minutes quota", str(limits.monthly_minutes_quota))
        _style_kv_table(table)
        self._console.print(
            Panel(table, title=f"[bold {_G}]limits[/]", border_style=_G)
        )

    def cmd_cameras(self) -> None:
        if not self._require_bootstrap():
            return
        assert self._bootstrap is not None
        cameras = self._bootstrap.cameras
        if not cameras:
            self._console.print(
                "No cameras configured. Add one on the dashboard (Cameras → "
                f"New) — the agent picks it up on [bold {_G}]refresh[/]."
            )
            return
        table = Table(box=None, padding=(0, 2), header_style=f"bold {_G}")
        table.add_column("Name", style=f"bold {_G}")
        table.add_column("Type", style=_B)
        table.add_column("Source", style=_B)
        table.add_column("Default config", style=_B)
        table.add_column("Assigned to")
        my_agent_id = self._state.agent_id
        for cam in cameras:
            config = "—"
            if cam.default_width and cam.default_height:
                config = f"{cam.default_width}x{cam.default_height}"
                if cam.default_fps:
                    config += f" @ {cam.default_fps}fps"
            if cam.agent_id is None:
                assigned = f"[{_GOLD}]unassigned[/]"
            elif cam.agent_id == my_agent_id:
                assigned = f"[bold {_G}]this agent[/]"
            else:
                assigned = f"[{_B}]another agent[/]"
            table.add_row(
                cam.name or cam.id[:8],
                cam.source_type or "?",
                cam.device_path or "—",
                config,
                assigned,
            )
        self._console.print(
            Panel(table, title=f"[bold {_G}]cameras[/]", border_style=_G)
        )

    def cmd_streams(self) -> None:
        statuses = self._registry.statuses()
        table = Table(box=None, padding=(0, 2), header_style=f"bold {_G}")
        table.add_column("Stream", style=f"bold {_G}")
        table.add_column("State")
        table.add_column("Detail", style=_B)
        for st in statuses:
            table.add_row(st.name, _state_markup(st.state), st.last_message or "")
        if self._bootstrap is not None:
            local_names = {st.name for st in statuses}
            camera_names = {
                cam.id: (cam.name or cam.id[:8]) for cam in self._bootstrap.cameras
            }
            for sess in self._bootstrap.active_sessions:
                cam_name = camera_names.get(sess.camera_id or "", sess.camera_id or "?")
                if cam_name in local_names:
                    continue  # already shown as a local worker
                table.add_row(
                    cam_name,
                    _state_markup(sess.state or "?"),
                    f"[{_GOLD}]active on server[/]",
                )
        if table.row_count == 0:
            self._console.print(
                f"No streams running. Type [bold {_G}]start[/] to begin one."
            )
            return
        self._console.print(
            Panel(table, title=f"[bold {_G}]streams[/]", border_style=_G)
        )

    def cmd_start(self, name: str = "") -> None:
        if self._cfg is None or not self._has_usable_token():
            self._console.print(
                f"Not configured — type [bold {_G}]login[/] first."
            )
            return
        if self._bootstrap is None:
            self.cmd_refresh()

        camera = self._pick_server_camera(name)
        if camera is not None:
            self._start_server_camera(camera)
            return

        # Local fallback: no server cameras (or no match) — stream the
        # locally configured camera; registration happens inside run_agent.
        if name:
            self._console.print(
                f"[red]No camera named '{name}'.[/red] See [bold {_G}]cameras[/]."
            )
            return
        self._spawn_worker(self._cfg, self._state, worker_name=self._cfg.camera.name)

    def cmd_stop(self, name: str = "") -> None:
        running = self._registry.running()
        if not running:
            self._console.print("No streams are running.")
            return
        if name:
            worker = self._registry.get(name)
            if worker is None or not worker.is_running:
                self._console.print(f"No running stream named '{name}'.")
                return
        elif len(running) == 1:
            worker = running[0]
        else:
            self._console.print(
                "Multiple streams running — name one: "
                + ", ".join(w.name for w in running)
            )
            return
        self._console.print(f"[{_GOLD}]Stopping {worker.name}...[/]")
        worker.stop()
        self._console.print(f"[bold {_G}]{worker.name} stopped.[/]")

    def cmd_logs(self, arg: str = "") -> None:
        try:
            n = int(arg) if arg else 50
        except ValueError:
            self._console.print(f"Not a number: {arg!r}")
            return
        lines = self._ring.tail(n)
        if not lines:
            self._console.print("No log lines yet.")
            return
        for line in lines:
            self._console.print(line, highlight=False, markup=False)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _quit(self) -> int:
        running = self._registry.running()
        if running:
            self._console.print(
                f"[{_GOLD}]Stopping {len(running)} stream(s) before exit...[/]"
            )
        self._registry.stop_all()
        self._console.print(f"[bold {_G}]Bye.[/]")
        return 0

    def _load_config(self) -> None:
        try:
            self._cfg = load_config(self._config_path, allow_placeholder=True)
        except ConfigError:
            self._cfg = None

    def _has_usable_token(self) -> bool:
        return self._cfg is not None and not self._cfg.api.is_placeholder_token

    def _new_client(self) -> UpstreamApiClient:
        assert self._cfg is not None
        return self._client_factory(self._cfg.api.base_url, self._cfg.api.token)

    def _require_bootstrap(self) -> bool:
        if self._bootstrap is None:
            self.cmd_refresh()
        if self._bootstrap is None:
            return False
        return True

    def _my_agent(self) -> Agent | None:
        if self._bootstrap is None:
            return None
        agent_id = self._state.agent_id
        for agent in self._bootstrap.agents:
            if agent.id == agent_id:
                return agent
        return self._bootstrap.agents[0] if self._bootstrap.agents else None

    def _save_progress(self, new_state: Any) -> None:
        with self._state_lock:
            self._state = new_state
            save_state(new_state, self._state_path)

    def _pick_server_camera(self, name: str) -> Camera | None:
        if self._bootstrap is None or not self._bootstrap.cameras:
            return None
        cameras = self._bootstrap.cameras
        if not name:
            # Prefer a camera already assigned to this agent, else an
            # unassigned one; never auto-pick another agent's camera.
            my_id = self._state.agent_id
            for cam in cameras:
                if cam.agent_id is not None and cam.agent_id == my_id:
                    return cam
            for cam in cameras:
                if cam.agent_id is None:
                    return cam
            return None
        lowered = name.lower()
        for cam in cameras:
            if (cam.name or "").lower() == lowered or cam.id.startswith(name):
                return cam
        return None

    def _start_server_camera(self, camera: Camera) -> None:
        assert self._cfg is not None
        worker_name = camera.name or camera.id[:8]
        existing = self._registry.get(worker_name)
        if existing is not None and existing.is_running:
            self._console.print(f"'{worker_name}' is already streaming.")
            return

        try:
            camera_cfg = camera_config_from_server(camera)
        except ConfigError as exc:
            self._console.print(f"[red]Cannot start '{worker_name}':[/red] {exc}")
            return

        try:
            agent_id = self._ensure_agent_registered()
            if camera.agent_id is None or camera.agent_id != agent_id:
                with self._new_client() as client:
                    client.claim_camera(camera_id=camera.id, agent_id=agent_id)
                self._console.print(
                    f"[{_B}]Claimed camera '{worker_name}' for this agent.[/]"
                )
        except ApiError as exc:
            self._console.print(f"[red]Cannot start '{worker_name}':[/red] {exc}")
            return

        run_cfg = self._cfg.model_copy(update={"camera": camera_cfg})
        run_state = self._state.model_copy(
            update={"agent_id": agent_id, "last_camera_id": camera.id}
        )
        self._spawn_worker(run_cfg, run_state, worker_name=worker_name)

    def _ensure_agent_registered(self) -> str:
        """Make sure this install has an agent row; return its id."""

        with self._state_lock:
            agent_id = self._state.agent_id
        if agent_id:
            return agent_id
        with self._new_client() as client:
            resp = client.register_agent(
                AgentRegisterRequest(
                    name=_agent_name_from(self._state.device_id),
                    hostname=None,
                    version=__version__,
                    os=_safe_os(),
                    python_version=_safe_python_version(),
                    capabilities={},
                )
            )
        self._save_progress(self._state.model_copy(update={"agent_id": resp.agent.id}))
        return resp.agent.id

    def _spawn_worker(self, cfg: Config, state: Any, *, worker_name: str) -> None:
        worker = StreamWorker(
            cfg,
            state,
            name=worker_name,
            save_progress=self._save_progress,
            run_fn=self._run_fn,
        )
        self._registry.add(worker)
        worker.start()
        self._console.print(
            f"[bold {_G}]Started '{worker_name}'.[/] It runs in the "
            f"background — use [bold {_G}]streams[/] / [bold {_G}]logs[/] to "
            f"watch it, [bold {_G}]stop[/] to end it."
        )
