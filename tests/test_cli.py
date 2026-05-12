"""Tests for the ``upstream`` CLI (``init``, ``status``, ``check``)."""

from __future__ import annotations

import json
import sys
import types
import uuid
from pathlib import Path
from textwrap import dedent

import pytest
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


def test_help_lists_init_status_and_check() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "init" in result.stdout
    assert "status" in result.stdout
    assert "check" in result.stdout


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


# ---------------------------------------------------------------------------
# upstream check --camera
# ---------------------------------------------------------------------------


# Match the property constants used by the fake cv2 in test_sources_webcam.
_CAP_PROP_FRAME_WIDTH = 3
_CAP_PROP_FRAME_HEIGHT = 4
_CAP_PROP_FPS = 5


def _build_fake_cv2(
    opens: bool = True,
    read_returns: tuple[bool, object] = (True, "frame"),
    effective_width: int = 1280,
    effective_height: int = 720,
    effective_fps: float = 30.0,
) -> types.SimpleNamespace:
    """Build a stand-in cv2 module with one configurable VideoCapture."""

    props = {
        _CAP_PROP_FRAME_WIDTH: float(effective_width),
        _CAP_PROP_FRAME_HEIGHT: float(effective_height),
        _CAP_PROP_FPS: float(effective_fps),
    }

    class _FakeCap:
        def __init__(self, source: object) -> None:
            self.source = source
            self.release_count = 0

        def isOpened(self) -> bool:  # noqa: N802
            return opens

        def set(self, prop: int, value: float) -> bool:  # noqa: A003
            return True

        def get(self, prop: int) -> float:
            return props.get(prop, 0.0)

        def read(self) -> tuple[bool, object]:
            return read_returns

        def release(self) -> None:
            self.release_count += 1

    return types.SimpleNamespace(
        CAP_PROP_FRAME_WIDTH=_CAP_PROP_FRAME_WIDTH,
        CAP_PROP_FRAME_HEIGHT=_CAP_PROP_FRAME_HEIGHT,
        CAP_PROP_FPS=_CAP_PROP_FPS,
        VideoCapture=_FakeCap,
    )


def _write_camera_config(
    home_dir: Path,
    *,
    token: str = PLACEHOLDER_TOKEN,
    source_type: str = "webcam",
    source: int | str = 0,
    width: int = 1280,
    height: int = 720,
    fps: int = 30,
) -> Path:
    config_file = home_dir / ".upstream" / "config.yaml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    source_repr = repr(source) if isinstance(source, str) else str(source)
    config_file.write_text(
        dedent(
            f"""\
            api:
              base_url: "https://api.upstream.ai"
              token: "{token}"
            camera:
              name: "Test Camera"
              source_type: "{source_type}"
              source: {source_repr}
              requested_width: {width}
              requested_height: {height}
              requested_fps: {fps}
            """
        ),
        encoding="utf-8",
    )
    return config_file


def test_check_no_flag_exits_with_usage_hint(home_dir: Path) -> None:
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 2
    assert "Specify what to check" in result.stderr


def test_check_camera_without_config_exits_with_init_hint(home_dir: Path) -> None:
    result = runner.invoke(app, ["check", "--camera"])
    assert result.exit_code == 1
    assert "No config found" in result.stderr
    assert "upstream init" in result.stderr


def test_check_camera_success_prints_resolution_and_fps(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_camera_config(home_dir)
    monkeypatch.setitem(sys.modules, "cv2", _build_fake_cv2(opens=True))

    result = runner.invoke(app, ["check", "--camera"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Camera opened successfully" in result.stdout
    assert "Resolution: 1280x720" in result.stdout
    assert "FPS: 30" in result.stdout
    # Matching request → no Note line.
    assert "Note: requested" not in result.stdout


def test_check_camera_renegotiated_resolution_emits_note(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_camera_config(home_dir, width=1920, height=1080, fps=60)
    monkeypatch.setitem(
        sys.modules,
        "cv2",
        _build_fake_cv2(opens=True, effective_width=1280, effective_height=720, effective_fps=30.0),
    )

    result = runner.invoke(app, ["check", "--camera"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Resolution: 1280x720" in result.stdout
    assert "FPS: 30" in result.stdout
    assert "Note: requested 1920x1080 @ 60" in result.stdout
    assert "device negotiated 1280x720 @ 30" in result.stdout


def test_check_camera_open_failure_exits_one_with_actionable_error(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_camera_config(home_dir, source=0)
    monkeypatch.setitem(sys.modules, "cv2", _build_fake_cv2(opens=False))

    result = runner.invoke(app, ["check", "--camera"])
    assert result.exit_code == 1
    assert "Error: Could not open webcam" in result.stderr
    assert "index 0" in result.stderr
    assert "not already in use" in result.stderr
    assert "Camera opened successfully" not in result.stdout


def test_check_camera_unsupported_source_type_exits_one(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_camera_config(home_dir, source_type="rtsp", source="rtsp://example/stream")
    # cv2 must not even be consulted; install a poisoned module so any
    # accidental access blows up the test instead of silently passing.
    poisoned = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "cv2", poisoned)

    result = runner.invoke(app, ["check", "--camera"])
    assert result.exit_code == 1
    assert "rtsp" in result.stderr
    assert "not supported yet" in result.stderr


def test_check_camera_works_with_placeholder_token(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_camera_config(home_dir, token=PLACEHOLDER_TOKEN)
    monkeypatch.setitem(sys.modules, "cv2", _build_fake_cv2(opens=True))

    result = runner.invoke(app, ["check", "--camera"])
    # Camera check does not call the API, so placeholder must not block.
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Camera opened successfully" in result.stdout


def test_check_camera_does_not_leak_real_token(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk_live_abcd1234efgh"
    _write_camera_config(home_dir, token=secret)
    monkeypatch.setitem(sys.modules, "cv2", _build_fake_cv2(opens=True))

    result = runner.invoke(app, ["check", "--camera"])
    assert result.exit_code == 0
    assert secret not in result.stdout
    assert secret not in result.stderr

    # The log file shouldn't carry the raw token either.
    log_file = home_dir / ".upstream" / "logs" / "agent.log"
    if log_file.exists():
        assert secret not in log_file.read_text(encoding="utf-8")


def test_check_camera_invalid_yaml_exits_with_error(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = home_dir / ".upstream" / "config.yaml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("api: : not yaml: :\n  - bad", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "cv2", _build_fake_cv2(opens=True))

    result = runner.invoke(app, ["check", "--camera"])
    assert result.exit_code == 1
    assert "Error" in result.stderr


def test_check_camera_fps_unknown_when_driver_reports_zero(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_camera_config(home_dir)
    monkeypatch.setitem(sys.modules, "cv2", _build_fake_cv2(opens=True, effective_fps=0.0))

    result = runner.invoke(app, ["check", "--camera"])
    assert result.exit_code == 0
    assert "FPS: unknown" in result.stdout
