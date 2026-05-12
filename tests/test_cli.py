"""Tests for the ``upstream`` CLI (``init`` and ``status``)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from textwrap import dedent

from typer.testing import CliRunner

from upstream_agent.cli import app
from upstream_agent.config import PLACEHOLDER_TOKEN

runner = CliRunner()


def _is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


def test_help_lists_init_and_status() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "init" in result.stdout
    assert "status" in result.stdout


def test_version_flag_prints_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    from upstream_agent.version import __version__

    assert __version__ in result.stdout


# ---------------------------------------------------------------------------
# upstream init
# ---------------------------------------------------------------------------


def test_init_creates_config_at_default_path(home_dir: Path) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.stdout + result.stderr

    config_file = home_dir / ".upstream" / "config.yaml"
    assert config_file.exists()
    contents = config_file.read_text(encoding="utf-8")
    assert PLACEHOLDER_TOKEN in contents
    assert "DO NOT COMMIT" in contents
    assert "Wrote default config" in result.stdout
    assert "Never commit" in result.stdout


def test_init_refuses_overwrite_without_force(home_dir: Path) -> None:
    first = runner.invoke(app, ["init"])
    assert first.exit_code == 0
    second = runner.invoke(app, ["init"])
    assert second.exit_code == 1
    assert "already exists" in second.stderr
    assert "--force" in second.stderr


def test_init_force_overwrites(home_dir: Path) -> None:
    config_file = home_dir / ".upstream" / "config.yaml"
    runner.invoke(app, ["init"])
    config_file.write_text("# tampered\n", encoding="utf-8")

    result = runner.invoke(app, ["init", "--force"])
    assert result.exit_code == 0
    assert PLACEHOLDER_TOKEN in config_file.read_text(encoding="utf-8")


def test_init_respects_config_flag(home_dir: Path, tmp_path: Path) -> None:
    alt = tmp_path / "elsewhere" / "alt.yaml"
    result = runner.invoke(app, ["init", "--config", str(alt)])
    assert result.exit_code == 0
    assert alt.exists()
    # Default path was NOT created.
    assert not (home_dir / ".upstream" / "config.yaml").exists()


# ---------------------------------------------------------------------------
# upstream status
# ---------------------------------------------------------------------------


def test_status_with_no_config_is_informational(home_dir: Path) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "No config found" in result.stdout
    assert "upstream init" in result.stdout
    # Even without a config, state should be initialised.
    assert "device_id" in result.stdout


def test_status_with_valid_config_redacts_token(home_dir: Path) -> None:
    config_file = home_dir / ".upstream" / "config.yaml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    secret = "sk_live_abcd1234efgh"
    config_file.write_text(
        dedent(
            f"""\
            api:
              base_url: "https://api.upstream.ai"
              token: "{secret}"
            camera:
              name: "Front Door"
              source_type: "webcam"
              source: 0
            """
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert secret not in result.stdout
    assert "****efgh" in result.stdout
    assert "Front Door" in result.stdout
    assert "https://api.upstream.ai" in result.stdout


def test_status_creates_device_id_on_first_run(home_dir: Path) -> None:
    runner.invoke(app, ["init"])
    # Edit out the placeholder so status doesn't show a placeholder note.
    config_file = home_dir / ".upstream" / "config.yaml"
    config_file.write_text(
        config_file.read_text().replace(PLACEHOLDER_TOKEN, "real_token_value_123"),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0

    state_file = home_dir / ".upstream" / "agent_state.json"
    assert state_file.exists()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert _is_valid_uuid(state["device_id"])
    assert state["device_id"] in result.stdout


def test_status_with_placeholder_token_warns(home_dir: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "placeholder" in result.stdout.lower()
    # Even when displaying the placeholder note, the literal placeholder
    # string is shown as part of the user-facing reminder — but the token
    # field on the API line should still be redacted.
    assert "****" in result.stdout


def test_status_with_corrupt_state_recovers(home_dir: Path) -> None:
    state_file = home_dir / ".upstream" / "agent_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("{not valid json", encoding="utf-8")

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    # Fresh state file regenerated.
    new_state = json.loads(state_file.read_text(encoding="utf-8"))
    assert _is_valid_uuid(new_state["device_id"])
    # Backup preserved.
    backups = list(state_file.parent.glob("agent_state.json.corrupt-*"))
    assert len(backups) == 1


def test_status_invalid_yaml_exits_with_error(home_dir: Path) -> None:
    config_file = home_dir / ".upstream" / "config.yaml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("api: : not valid yaml: :\n  - oops", encoding="utf-8")

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1
    assert "Error" in result.stderr
