"""InteractiveShell REPL: login flow, status/limits/cameras rendering,
start/streams/stop lifecycle, logs view, disconnect handling.

Driven entirely through injected ``input_fn`` / ``password_fn`` /
``client_factory`` / ``run_fn`` and a Rich Console writing to StringIO —
no TTY, server, camera, or home-directory access.
"""

from __future__ import annotations

import io
import logging
import threading
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from upstream_agent.api.schemas import (
    Agent,
    AgentRegisterRequest,
    AgentRegisterResponse,
    BootstrapResponse,
    CameraClaimResponse,
)
from upstream_agent.errors import ApiAuthError, ApiConnectionError
from upstream_agent.interactive.shell import InteractiveShell
from upstream_agent.interactive.worker import WorkerRegistry
from upstream_agent.logging_setup import RingBufferHandler

REAL_TOKEN = "ups_test_ABCDEFGH_SECRETSECRETSECRET22"

_BOOTSTRAP_BODY = {
    "account": {
        "id": "acct-1",
        "name": "Josh Demo",
        "slug": "josh-demo",
        "plan": "free",
        "status": "active",
    },
    "token": {"scopes": ["agent"], "last_four": "T22", "expires_at": None},
    "agents": [{"id": "agent_1", "name": "macbook-agent", "status": "active"}],
    "limits": {
        "max_cameras": 5,
        "max_concurrent_sessions": 1,
        "max_fps": 15,
        "max_width": 640,
        "max_height": 360,
        "max_bitrate_kbps": 1500,
        "allowed_codecs": ["h264"],
        "allowed_stream_types": ["webcam", "rtsp"],
        "monthly_minutes_quota": 600,
        "snapshot_storage_mb": 1024,
    },
    "usage": {"camera_count": 1, "active_session_count": 0, "monthly_minutes_used": 12},
    "cameras": [
        {
            "id": "cam-1111",
            "name": "Front Door",
            "source_type": "webcam",
            "device_path": "webcam:0",
            "agent_id": None,
            "default_width": 640,
            "default_height": 360,
            "default_fps": 15,
        }
    ],
    "active_sessions": [],
}


class FakeShellClient:
    """Stands in for UpstreamApiClient inside the shell."""

    def __init__(
        self,
        *,
        bootstrap_raises: Exception | None = None,
        valid_tokens: set[str] | None = None,
    ) -> None:
        self.bootstrap_raises = bootstrap_raises
        self.valid_tokens = valid_tokens if valid_tokens is not None else {REAL_TOKEN}
        self.token: str = ""
        self.bootstrap_calls = 0
        self.claim_calls: list[tuple[str, str]] = []
        self.register_agent_calls: list[AgentRegisterRequest] = []

    def bind(self, token: str) -> "FakeShellClient":
        self.token = token
        return self

    def __enter__(self) -> "FakeShellClient":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def get_bootstrap(self) -> BootstrapResponse:
        self.bootstrap_calls += 1
        if self.bootstrap_raises is not None:
            raise self.bootstrap_raises
        if self.token not in self.valid_tokens:
            raise ApiAuthError("GET /v1/agents/bootstrap -> 401, code=invalid_token")
        return BootstrapResponse.model_validate(_BOOTSTRAP_BODY)

    def register_agent(self, req: AgentRegisterRequest) -> AgentRegisterResponse:
        self.register_agent_calls.append(req)
        return AgentRegisterResponse(agent=Agent(id="agent_1", status="active"))

    def claim_camera(self, *, camera_id: str, agent_id: str) -> CameraClaimResponse:
        self.claim_calls.append((camera_id, agent_id))
        return CameraClaimResponse(id=camera_id, agent_id=agent_id)


def _write_config(tmp_path: Path, token: str = REAL_TOKEN) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
api:
  base_url: "http://api.test.local"
  token: "{token}"
camera:
  name: "Front Door"
  source_type: "webcam"
  source: 0
""",
        encoding="utf-8",
    )
    return config_path


def _make_shell(
    tmp_path: Path,
    inputs: list[str],
    *,
    client: FakeShellClient | None = None,
    config_path: Path | None = None,
    passwords: list[str] | None = None,
    run_fn: Any = None,
) -> tuple[InteractiveShell, FakeShellClient, io.StringIO, WorkerRegistry]:
    fake_client = client or FakeShellClient()
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=100)
    inputs_iter = iter(inputs)
    passwords_iter = iter(passwords or [])
    registry = WorkerRegistry()

    def _blocking_run(cfg: Any, state: Any, *, shutdown: threading.Event, **_: Any) -> None:
        shutdown.wait(timeout=10.0)

    shell = InteractiveShell(
        config_path=config_path if config_path is not None else _write_config(tmp_path),
        state_path=tmp_path / "agent_state.json",
        ring=RingBufferHandler(),
        registry=registry,
        client_factory=lambda _base_url, token: fake_client.bind(token),
        run_fn=run_fn or _blocking_run,
        input_fn=lambda _prompt: next(inputs_iter),
        password_fn=lambda _prompt: next(passwords_iter),
        console=console,
    )
    return shell, fake_client, out, registry


def test_launch_shows_logo(tmp_path: Path) -> None:
    shell, _client, out, _ = _make_shell(tmp_path, ["quit"])
    shell.run()
    text = out.getvalue()
    # The UpStream mark + wordmark print on every launch.
    assert "$$$$$$$" in text
    assert "interactive mode" in text


def test_brand_colors_render_in_truecolor_terminals(tmp_path: Path) -> None:
    """Green #344C2C is the main color from the logo down; blue and gold
    accent. Assert the truecolor escape codes for all three appear."""

    fake_client = FakeShellClient()
    out = io.StringIO()
    console = Console(file=out, force_terminal=True, color_system="truecolor", width=100)
    inputs = iter(["help", "cameras", "quit"])
    shell = InteractiveShell(
        config_path=_write_config(tmp_path),
        state_path=tmp_path / "agent_state.json",
        ring=RingBufferHandler(),
        registry=WorkerRegistry(),
        client_factory=lambda _b, t: fake_client.bind(t),
        input_fn=lambda _p: next(inputs),
        console=console,
    )
    shell.run()
    text = out.getvalue()
    assert "\x1b[38;2;52;76;44" in text  # green #344C2C
    assert "\x1b[38;2;75;129;108" in text  # blue #4B816C
    assert "\x1b[38;2;129;118;75" in text  # gold #81764B


def test_help_lists_all_commands(tmp_path: Path) -> None:
    shell, _client, out, _ = _make_shell(tmp_path, ["help", "quit"])
    assert shell.run() == 0
    text = out.getvalue()
    for cmd in ("status", "limits", "cameras", "streams", "start", "stop", "logs", "login"):
        assert cmd in text


def test_status_renders_account_caps_and_source_types(tmp_path: Path) -> None:
    shell, client, out, _ = _make_shell(tmp_path, ["quit"])
    shell.run()
    text = out.getvalue()
    assert client.bootstrap_calls == 1
    assert "Josh Demo" in text
    assert "Streams: 0 / 1 active" in text
    assert "Max stream config: 640x360 @ 15fps" in text
    assert "Allowed source types: webcam, rtsp" in text


def test_unconfigured_shell_opens_and_hints_login(tmp_path: Path) -> None:
    shell, client, out, _ = _make_shell(
        tmp_path, ["quit"], config_path=tmp_path / "missing.yaml"
    )
    assert shell.run() == 0
    assert "No API token configured" in out.getvalue()
    assert client.bootstrap_calls == 0


def test_login_validates_saves_and_connects(tmp_path: Path) -> None:
    config_path = tmp_path / "fresh.yaml"
    shell, client, out, _ = _make_shell(
        tmp_path,
        ["login", "quit"],
        config_path=config_path,
        passwords=[REAL_TOKEN],
    )
    assert shell.run() == 0
    text = out.getvalue()
    assert "Connected to Josh Demo" in text
    # Token never echoed; only the ****-suffix hint appears.
    assert REAL_TOKEN not in text
    assert "****" in text
    # Persisted for next launch.
    assert REAL_TOKEN in config_path.read_text(encoding="utf-8")


def test_login_rejects_bad_token_without_saving(tmp_path: Path) -> None:
    config_path = tmp_path / "fresh.yaml"
    shell, _client, out, _ = _make_shell(
        tmp_path,
        ["login", "quit"],
        config_path=config_path,
        passwords=["ups_test_WRONGGGG_notvalidnotvalid22"],
    )
    assert shell.run() == 0
    assert "Token rejected" in out.getvalue()
    assert not config_path.exists()


def test_login_rejects_malformed_token_shape(tmp_path: Path) -> None:
    config_path = tmp_path / "fresh.yaml"
    shell, client, out, _ = _make_shell(
        tmp_path,
        ["login", "quit"],
        config_path=config_path,
        passwords=["not-a-token"],
    )
    shell.run()
    assert "doesn't look like an UpStream token" in out.getvalue()
    assert client.bootstrap_calls == 0


def test_disconnected_then_refresh_recovers(tmp_path: Path) -> None:
    client = FakeShellClient(bootstrap_raises=ApiConnectionError("no route to host"))
    shell, client, out, _ = _make_shell(
        tmp_path, ["refresh", "quit"], client=client
    )

    real_get = FakeShellClient.get_bootstrap
    calls = {"n": 0}

    def flaky(self: FakeShellClient) -> BootstrapResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ApiConnectionError("no route to host")
        self.bootstrap_raises = None
        return real_get(self)

    client.get_bootstrap = flaky.__get__(client)  # type: ignore[method-assign]
    shell.run()
    text = out.getvalue()
    assert "Disconnected" in text
    assert calls["n"] == 2  # startup failed, manual refresh succeeded


def test_limits_and_cameras_render_server_data(tmp_path: Path) -> None:
    shell, _client, out, _ = _make_shell(tmp_path, ["limits", "cameras", "quit"])
    shell.run()
    text = out.getvalue()
    assert "Allowed source types" in text
    assert "webcam, rtsp" in text
    assert "Front Door" in text
    assert "unassigned" in text


def test_start_claims_camera_and_runs_worker_then_stop(tmp_path: Path) -> None:
    started = threading.Event()

    def run_fn(cfg: Any, state: Any, *, shutdown: threading.Event, **_: Any) -> None:
        started.set()
        shutdown.wait(timeout=10.0)

    shell, client, out, registry = _make_shell(
        tmp_path, ["start", "streams", "stop", "quit"], run_fn=run_fn
    )
    assert shell.run() == 0
    text = out.getvalue()

    assert started.is_set()
    # Camera was unassigned -> agent registered + camera claimed first.
    assert len(client.register_agent_calls) == 1
    assert client.claim_calls == [("cam-1111", "agent_1")]
    assert "Started 'Front Door'" in text
    assert "Front Door stopped." in text
    assert registry.running() == []

    # The worker's config came from the SERVER camera record.
    # (run_fn received it; verify via the registry's worker.)
    worker = registry.get("Front Door")
    assert worker is not None
    assert worker._cfg.camera.requested_width == 640  # noqa: SLF001
    assert worker._cfg.camera.requested_fps == 15  # noqa: SLF001


def test_quit_stops_running_workers(tmp_path: Path) -> None:
    started = threading.Event()

    def run_fn(cfg: Any, state: Any, *, shutdown: threading.Event, **_: Any) -> None:
        started.set()
        shutdown.wait(timeout=10.0)

    shell, _client, out, registry = _make_shell(tmp_path, ["start", "quit"], run_fn=run_fn)
    assert shell.run() == 0
    assert started.is_set()
    assert registry.running() == []
    assert "Stopping 1 stream(s) before exit" in out.getvalue()


def test_logs_prints_ring_buffer_tail(tmp_path: Path) -> None:
    shell, _client, out, _ = _make_shell(tmp_path, ["logs 3", "quit"])
    ring: RingBufferHandler = shell._ring  # noqa: SLF001
    ring.setFormatter(logging.Formatter("%(message)s"))
    for i in range(5):
        ring.emit(
            logging.LogRecord("w", logging.INFO, __file__, 1, f"log-{i}", None, None)
        )
    shell.run()
    text = out.getvalue()
    assert "log-2" in text and "log-4" in text
    assert "log-0" not in text


def test_unknown_command_prints_hint(tmp_path: Path) -> None:
    shell, _client, out, _ = _make_shell(tmp_path, ["frobnicate", "quit"])
    shell.run()
    assert "Unknown command" in out.getvalue()


def test_eof_quits_cleanly(tmp_path: Path) -> None:
    def raise_eof(_prompt: str) -> str:
        raise EOFError

    out = io.StringIO()
    shell = InteractiveShell(
        config_path=_write_config(tmp_path),
        state_path=tmp_path / "agent_state.json",
        ring=RingBufferHandler(),
        registry=WorkerRegistry(),
        client_factory=lambda _b, t: FakeShellClient().bind(t),
        input_fn=raise_eof,
        console=Console(file=out, force_terminal=False, width=100),
    )
    assert shell.run() == 0
    assert "Bye." in out.getvalue()
