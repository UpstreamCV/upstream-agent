"""Collector commands inside the interactive shell: start/status/stop, the
already-running guard, clean stop on exit, help listing, and the login gate.

Driven through the real REPL (``input_fn`` queue) with an injected fake
collector service and an offline API client, so no socket, server, or network
is touched."""

from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from upstream_agent.collector.status import CollectorStatus
from upstream_agent.errors import ApiConnectionError
from upstream_agent.interactive.shell import InteractiveShell
from upstream_agent.logging_setup import RingBufferHandler

REAL_TOKEN = "ups_test_ABCDEFGH_SECRETSECRETSECRET22"


class FakeCollector:
    def __init__(self) -> None:
        self.running = False
        self.start_calls = 0
        self.stop_calls = 0

    def is_running(self) -> bool:
        return self.running

    def start(self) -> None:
        self.start_calls += 1
        self.running = True

    def stop(self, timeout: float = 5.0) -> None:
        self.stop_calls += 1
        self.running = False

    def status(self) -> CollectorStatus:
        return CollectorStatus(
            running=self.running,
            local_endpoint="http://127.0.0.1:8756/v1/observations",
            spooled_batches=0,
            spooled_observations=0,
            uploaded_batches=0,
            uploaded_observations=0,
            dropped_batches=0,
            dropped_observations=0,
            last_upload_at=None,
            last_error=None,
            cloud_reachable=True,
        )


class _OfflineClient:
    def __enter__(self) -> _OfflineClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def get_bootstrap(self) -> object:
        raise ApiConnectionError("offline")


def _write_config(tmp_path: Path, token: str = REAL_TOKEN) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
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
    return path


def _make_shell(
    tmp_path: Path,
    inputs: list[str],
    fake: FakeCollector,
    *,
    config_path: Path | None = None,
) -> tuple[InteractiveShell, io.StringIO]:
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=100)
    inputs_iter = iter(inputs)
    shell = InteractiveShell(
        config_path=config_path if config_path is not None else _write_config(tmp_path),
        state_path=tmp_path / "agent_state.json",
        ring=RingBufferHandler(),
        client_factory=lambda _b, _t: _OfflineClient(),
        collector_factory=lambda _cfg: fake,
        input_fn=lambda _p: next(inputs_iter),
        console=console,
    )
    return shell, out


def test_collector_start_status_stop(tmp_path: Path) -> None:
    fake = FakeCollector()
    shell, out = _make_shell(
        tmp_path, ["collector start", "collector status", "collector stop", "quit"], fake
    )
    assert shell.run() == 0
    text = out.getvalue()
    assert fake.start_calls == 1
    assert fake.stop_calls == 1
    assert "http://127.0.0.1:8756/v1/observations" in text
    assert "Collector stopped" in text


def test_collector_start_twice_does_not_duplicate(tmp_path: Path) -> None:
    fake = FakeCollector()
    shell, out = _make_shell(tmp_path, ["collector start", "collector start", "quit"], fake)
    shell.run()
    assert fake.start_calls == 1  # second call sees it already running
    assert "already running" in out.getvalue()


def test_help_lists_collector_commands(tmp_path: Path) -> None:
    fake = FakeCollector()
    shell, out = _make_shell(tmp_path, ["help", "quit"], fake)
    shell.run()
    assert "collector start" in out.getvalue()


def test_exit_stops_running_collector(tmp_path: Path) -> None:
    fake = FakeCollector()
    shell, _out = _make_shell(tmp_path, ["collector start", "quit"], fake)
    shell.run()
    assert fake.start_calls == 1
    assert fake.stop_calls == 1  # quit stopped the running collector


def test_collector_unknown_subcommand_shows_usage(tmp_path: Path) -> None:
    fake = FakeCollector()
    shell, out = _make_shell(tmp_path, ["collector wat", "quit"], fake)
    shell.run()
    assert "Usage" in out.getvalue()
    assert fake.start_calls == 0


def test_collector_start_requires_login(tmp_path: Path) -> None:
    fake = FakeCollector()
    shell, out = _make_shell(
        tmp_path,
        ["collector start", "quit"],
        fake,
        config_path=tmp_path / "missing.yaml",
    )
    shell.run()
    assert fake.start_calls == 0
    assert "login" in out.getvalue().lower()


def test_status_command_shows_collector_when_running(tmp_path: Path) -> None:
    fake = FakeCollector()
    shell, out = _make_shell(tmp_path, ["collector start", "status", "quit"], fake)
    shell.run()
    assert "Collector:" in out.getvalue()
