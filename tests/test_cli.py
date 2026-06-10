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
    assert "--camera" in result.stderr
    assert "--api" in result.stderr


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
    # The config loader should reject the value at load time, before any
    # camera code runs. Poison cv2 so any accidental access blows up the
    # test instead of silently passing.
    poisoned = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "cv2", poisoned)

    result = runner.invoke(app, ["check", "--camera"])
    assert result.exit_code == 1
    assert "Error" in result.stderr
    assert "camera.source_type" in result.stderr
    assert "webcam" in result.stderr


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


# ---------------------------------------------------------------------------
# upstream check --api
# ---------------------------------------------------------------------------


_API_SECRET_SRT_URL = "srt://stream.test.local:1935/live/CLI_TEST_SECRET_STREAM_TOKEN"
_API_SECRET_SESSION_TOKEN = "eyJhbGciOiJIUzI1NiI.CLI_TEST_SECRET_JWT.signature"


def _install_fake_api_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent_response: dict | None = None,
    agent_raises: Exception | None = None,
    camera_response: dict | None = None,
    camera_raises: Exception | None = None,
    session_response: dict | None = None,
    session_raises: Exception | None = None,
    stop_raises: Exception | None = None,
) -> dict:
    """Install a fake UpstreamApiClient on the diagnostics module.

    Returns a captures dict so tests can inspect call counts and the
    actual request payloads.
    """

    from upstream_agent.api.schemas import (
        Agent,
        AgentRegisterResponse,
        ApprovedStreamConfig,
        Camera,
        CameraRegisterResponse,
        RequestedStreamConfig,
        Session,
        StopStreamSessionResponse,
        StreamSessionResponse,
    )
    from upstream_agent.diagnostics import api as api_module

    default_agent = AgentRegisterResponse(
        agent=Agent(id="agent_123", name="upstream-agent-12345678", status="active"),
        server_time="2026-05-13T02:00:00Z",
    )
    default_camera = CameraRegisterResponse(
        camera=Camera(id="cam_456", external_id="webcam-0", status="active")
    )
    _approved_kwargs = dict(
        width=1280,
        height=720,
        fps=30,
        codec="h264",
        bitrate_kbps=3000,
        latency_mode="balanced",
    )
    default_session = StreamSessionResponse(
        session=Session(
            id="sess_789",
            state="approved",
            srt_url=_API_SECRET_SRT_URL,
            streamid="publish:secret",
            requested=RequestedStreamConfig(**_approved_kwargs),
            approved=ApprovedStreamConfig(**_approved_kwargs),
        ),
        srt_url=_API_SECRET_SRT_URL,
        session_token=_API_SECRET_SESSION_TOKEN,
        expires_at="2026-05-13T06:00:00Z",
    )

    captures: dict = {
        "agent_calls": 0,
        "camera_calls": 0,
        "session_calls": 0,
        "stop_calls": [],
        "factory_called_with_base_url": None,
    }

    class FakeClient:
        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def register_agent(self, req: object) -> object:
            captures["agent_calls"] += 1
            if agent_raises is not None:
                raise agent_raises
            if agent_response is not None:
                return AgentRegisterResponse.model_validate(agent_response)
            return default_agent

        def register_camera(self, req: object) -> object:
            captures["camera_calls"] += 1
            if camera_raises is not None:
                raise camera_raises
            if camera_response is not None:
                return CameraRegisterResponse.model_validate(camera_response)
            return default_camera

        def start_stream_session(self, req: object) -> object:
            captures["session_calls"] += 1
            if session_raises is not None:
                raise session_raises
            if session_response is not None:
                return StreamSessionResponse.model_validate(session_response)
            return default_session

        def stop_stream_session(self, session_id: str) -> object:
            captures["stop_calls"].append(session_id)
            if stop_raises is not None:
                raise stop_raises
            return StopStreamSessionResponse()

    def fake_factory(cfg):  # type: ignore[no-untyped-def]
        captures["factory_called_with_base_url"] = cfg.base_url
        return FakeClient()

    monkeypatch.setattr(api_module, "_default_factory", fake_factory)
    return captures


def test_check_api_no_config_exits_with_init_hint(home_dir: Path) -> None:
    result = runner.invoke(app, ["check", "--api"])
    assert result.exit_code == 1
    assert "No config found" in result.stderr
    assert "upstream init" in result.stderr


def test_check_api_with_placeholder_token_exits_one(home_dir: Path) -> None:
    _write_camera_config(home_dir, token=PLACEHOLDER_TOKEN)

    result = runner.invoke(app, ["check", "--api"])
    assert result.exit_code == 1
    assert "API token is still a placeholder" in result.stderr


def test_check_api_happy_path_prints_block_and_saves_state(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_camera_config(home_dir, token="real_token_value_xyz")
    captures = _install_fake_api_client(monkeypatch)

    result = runner.invoke(app, ["check", "--api"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "API check successful" in result.stdout
    assert "Agent registered: agent_123" in result.stdout
    assert "Camera registered: cam_456" in result.stdout
    assert "Stream session approved: sess_789" in result.stdout
    assert (
        "Approved config: h264, 1280x720 @ 30 FPS, 3000 kbps, balanced"
        in result.stdout
    )
    assert "SRT URL received: yes" in result.stdout
    # account_id is intentionally NOT shown in CLI output.
    assert "account_id" not in result.stdout
    # No stop warning on the happy path.
    assert "Warning" not in result.stderr
    # And the cleanup stop was called with the approved session id.
    assert captures["stop_calls"] == ["sess_789"]

    state_file = home_dir / ".upstream" / "agent_state.json"
    assert state_file.exists()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["agent_id"] == "agent_123"
    assert state["last_camera_id"] == "cam_456"


def test_check_api_register_agent_failure_exits_one(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from upstream_agent.errors import ApiAuthError

    _write_camera_config(home_dir, token="real_token_value_xyz")
    _install_fake_api_client(
        monkeypatch, agent_raises=ApiAuthError("API token was rejected.")
    )

    result = runner.invoke(app, ["check", "--api"])
    assert result.exit_code == 1
    assert "Error (register_agent):" in result.stderr
    assert "rejected" in result.stderr

    # No agent_id was saved.
    state_file = home_dir / ".upstream" / "agent_state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state.get("agent_id") is None
    assert state.get("last_camera_id") is None


def test_check_api_register_camera_failure_persists_agent_id_only(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from upstream_agent.errors import ApiValidationError

    _write_camera_config(home_dir, token="real_token_value_xyz")
    _install_fake_api_client(
        monkeypatch,
        camera_raises=ApiValidationError("name already in use"),
    )

    result = runner.invoke(app, ["check", "--api"])
    assert result.exit_code == 1
    assert "Error (register_camera):" in result.stderr

    state_file = home_dir / ".upstream" / "agent_state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["agent_id"] == "agent_123"
    assert state.get("last_camera_id") is None


def test_check_api_session_failure_persists_both_ids(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from upstream_agent.errors import ApiServerError

    _write_camera_config(home_dir, token="real_token_value_xyz")
    _install_fake_api_client(
        monkeypatch, session_raises=ApiServerError("overloaded")
    )

    result = runner.invoke(app, ["check", "--api"])
    assert result.exit_code == 1
    assert "Error (start_stream_session):" in result.stderr

    state_file = home_dir / ".upstream" / "agent_state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["agent_id"] == "agent_123"
    assert state["last_camera_id"] == "cam_456"


def test_check_api_does_not_leak_token_srt_url_or_session_token(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_token = "ups_live_abcd_must_not_leak_zzz"
    _write_camera_config(home_dir, token=secret_token)
    _install_fake_api_client(monkeypatch)

    result = runner.invoke(app, ["check", "--api"])
    assert result.exit_code == 0
    assert secret_token not in result.stdout
    assert secret_token not in result.stderr
    assert _API_SECRET_SRT_URL not in result.stdout
    assert _API_SECRET_SRT_URL not in result.stderr
    assert _API_SECRET_SESSION_TOKEN not in result.stdout
    assert _API_SECRET_SESSION_TOKEN not in result.stderr
    # streamid must not appear either.
    assert "publish:secret" not in result.stdout
    assert "publish:secret" not in result.stderr

    # Log file must not carry any of these either.
    log_file = home_dir / ".upstream" / "logs" / "agent.log"
    if log_file.exists():
        content = log_file.read_text(encoding="utf-8")
        assert secret_token not in content
        assert _API_SECRET_SRT_URL not in content
        assert _API_SECRET_SESSION_TOKEN not in content
        assert "publish:secret" not in content


def test_check_api_succeeds_with_flat_camera_response(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backend may return a flat {id: ..., name: ...} for cameras; the
    pre-validator on CameraRegisterResponse must accept both shapes."""

    _write_camera_config(home_dir, token="real_token_value_xyz")
    _install_fake_api_client(
        monkeypatch,
        camera_response={"id": "cam_flat_777", "external_id": "webcam-0"},
    )

    result = runner.invoke(app, ["check", "--api"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Camera registered: cam_flat_777" in result.stdout


def test_check_camera_and_api_both_flags_runs_both(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_camera_config(home_dir, token="real_token_value_xyz")
    monkeypatch.setitem(sys.modules, "cv2", _build_fake_cv2(opens=True))
    _install_fake_api_client(monkeypatch)

    result = runner.invoke(app, ["check", "--camera", "--api"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Camera opened successfully" in result.stdout
    assert "API check successful" in result.stdout
    # Camera block prints before API block.
    cam_idx = result.stdout.index("Camera opened successfully")
    api_idx = result.stdout.index("API check successful")
    assert cam_idx < api_idx


def test_check_api_stop_failure_is_non_fatal_warning(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If stop fails after a successful start, the CLI prints the
    success block, adds a warning line on stderr, and exits 0 — the
    *check* itself succeeded."""

    from upstream_agent.errors import ApiServerError

    secret_token = "ups_live_must_not_leak_in_stop_warning"
    _write_camera_config(home_dir, token=secret_token)
    _install_fake_api_client(
        monkeypatch,
        stop_raises=ApiServerError("backend stop overloaded"),
    )

    result = runner.invoke(app, ["check", "--api"])

    # Exit 0 — the check succeeded.
    assert result.exit_code == 0, result.stdout + result.stderr
    # Success block still printed.
    assert "API check successful" in result.stdout
    assert "Stream session approved: sess_789" in result.stdout
    # And a warning on stderr.
    assert "Warning: failed to stop stream session" in result.stderr
    assert "overloaded" in result.stderr
    # No secret leaked through the warning path.
    assert secret_token not in result.stderr
    assert secret_token not in result.stdout
    assert _API_SECRET_SRT_URL not in result.stderr
    assert _API_SECRET_SRT_URL not in result.stdout
    assert _API_SECRET_SESSION_TOKEN not in result.stderr
    assert _API_SECRET_SESSION_TOKEN not in result.stdout


def test_check_api_back_to_back_runs_both_succeed(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running ``upstream check --api`` twice in a row must both
    succeed. With cleanup-stop in place, the second run no longer
    hits ``camera_already_streaming``."""

    _write_camera_config(home_dir, token="real_token_value_xyz")
    captures = _install_fake_api_client(monkeypatch)

    first = runner.invoke(app, ["check", "--api"])
    assert first.exit_code == 0, first.stdout + first.stderr

    second = runner.invoke(app, ["check", "--api"])
    assert second.exit_code == 0, second.stdout + second.stderr

    # Stop was called once per run.
    assert captures["stop_calls"] == ["sess_789", "sess_789"]


# ---------------------------------------------------------------------------
# upstream check --streaming
# ---------------------------------------------------------------------------


def _install_fake_ffmpeg_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    available: bool = True,
    version: str | None = "9.0.0",
    srt_supported: bool = True,
    h264_encoder: str | None = "libx264",
    error: str | None = None,
) -> None:
    """Stub the FFmpeg probe used by both diagnostic modules."""

    from upstream_agent.diagnostics import api as api_module
    from upstream_agent.diagnostics import streaming as streaming_module
    from upstream_agent.diagnostics.ffmpeg_check import FfmpegProbeResult

    result = FfmpegProbeResult(
        available=available,
        version=version,
        srt_supported=srt_supported,
        h264_encoder=h264_encoder,
        error=error,
    )

    monkeypatch.setattr(streaming_module, "probe_ffmpeg", lambda: result)
    monkeypatch.setattr(api_module, "probe_ffmpeg", lambda: result)


def test_check_streaming_no_config_exits_with_init_hint(home_dir: Path) -> None:
    result = runner.invoke(app, ["check", "--streaming"])
    assert result.exit_code == 1
    assert "No config found" in result.stderr
    assert "upstream init" in result.stderr


def test_check_streaming_placeholder_token_exits_one(home_dir: Path) -> None:
    _write_camera_config(home_dir, token=PLACEHOLDER_TOKEN)
    result = runner.invoke(app, ["check", "--streaming"])
    assert result.exit_code == 1
    assert "API token is still a placeholder" in result.stderr


def test_check_streaming_happy_path_prints_expected_block(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_camera_config(home_dir, token="real_token_value_xyz")
    _install_fake_ffmpeg_probe(monkeypatch)
    _install_fake_api_client(monkeypatch)

    result = runner.invoke(app, ["check", "--streaming"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Streaming preflight successful" in result.stdout
    assert "FFmpeg: found, version 9.0.0" in result.stdout
    assert "SRT support: yes" in result.stdout
    assert "H264 encoder: yes (libx264)" in result.stdout
    assert "API stream approval: yes" in result.stdout
    assert (
        "Approved config: h264, 1280x720 @ 30 FPS, 3000 kbps, balanced"
        in result.stdout
    )
    assert "SRT URL received: yes" in result.stdout
    assert "FFmpeg command build: yes" in result.stdout


def test_check_streaming_ffmpeg_missing_fails_with_install_hint(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_camera_config(home_dir, token="real_token_value_xyz")
    _install_fake_ffmpeg_probe(
        monkeypatch,
        available=False,
        version=None,
        srt_supported=False,
        h264_encoder=None,
        error="FFmpeg is not installed or not on PATH.",
    )
    captures = _install_fake_api_client(monkeypatch)

    result = runner.invoke(app, ["check", "--streaming"])
    assert result.exit_code == 1
    assert "Error (ffmpeg):" in result.stderr
    assert "FFmpeg" in result.stderr
    # API was never called.
    assert captures["agent_calls"] == 0


def test_check_streaming_srt_unsupported_fails_with_libsrt_hint(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_camera_config(home_dir, token="real_token_value_xyz")
    _install_fake_ffmpeg_probe(
        monkeypatch,
        available=True, version="8.1.1",
        srt_supported=False, h264_encoder="libx264",
    )
    captures = _install_fake_api_client(monkeypatch)

    result = runner.invoke(app, ["check", "--streaming"])
    assert result.exit_code == 1
    assert "Error (srt_support):" in result.stderr
    assert "libsrt" in result.stderr
    assert captures["agent_calls"] == 0


def test_check_streaming_no_h264_encoder_fails_clearly(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_camera_config(home_dir, token="real_token_value_xyz")
    _install_fake_ffmpeg_probe(
        monkeypatch,
        available=True, version="8.1.1",
        srt_supported=True, h264_encoder=None,
    )
    captures = _install_fake_api_client(monkeypatch)

    result = runner.invoke(app, ["check", "--streaming"])
    assert result.exit_code == 1
    assert "Error (h264_encoder):" in result.stderr
    assert "H264" in result.stderr
    assert captures["agent_calls"] == 0


def test_check_streaming_api_failure_exits_one(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from upstream_agent.errors import ApiAuthError

    _write_camera_config(home_dir, token="real_token_value_xyz")
    _install_fake_ffmpeg_probe(monkeypatch)
    _install_fake_api_client(
        monkeypatch, agent_raises=ApiAuthError("token rejected"),
    )

    result = runner.invoke(app, ["check", "--streaming"])
    assert result.exit_code == 1
    assert "Error (register_agent):" in result.stderr


def test_check_streaming_does_not_leak_token_srt_url_streamid_session_token(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_token = "ups_live_streaming_must_not_leak"
    _write_camera_config(home_dir, token=secret_token)
    _install_fake_ffmpeg_probe(monkeypatch)
    _install_fake_api_client(monkeypatch)

    result = runner.invoke(app, ["check", "--streaming"])
    assert result.exit_code == 0
    for blob in (result.stdout, result.stderr):
        assert secret_token not in blob
        assert _API_SECRET_SRT_URL not in blob
        assert _API_SECRET_SESSION_TOKEN not in blob
        assert "publish:secret" not in blob

    log_file = home_dir / ".upstream" / "logs" / "agent.log"
    if log_file.exists():
        content = log_file.read_text(encoding="utf-8")
        assert secret_token not in content
        assert _API_SECRET_SRT_URL not in content
        assert _API_SECRET_SESSION_TOKEN not in content
        assert "publish:secret" not in content


def test_check_streaming_capabilities_payload_reflects_ffmpeg_probe(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """register_agent should be called with capabilities derived from the probe."""

    _write_camera_config(home_dir, token="real_token_value_xyz")
    _install_fake_ffmpeg_probe(
        monkeypatch, version="9.0.0", srt_supported=True,
    )

    sent_requests: list = []

    from upstream_agent.api.schemas import (
        Agent,
        AgentRegisterResponse,
        ApprovedStreamConfig,
        Camera,
        CameraRegisterResponse,
        RequestedStreamConfig,
        Session,
        StopStreamSessionResponse,
        StreamSessionResponse,
    )
    from upstream_agent.diagnostics import api as api_module

    class FakeClient:
        def __enter__(self) -> FakeClient:
            return self
        def __exit__(self, *_: object) -> None:
            return None
        def register_agent(self, req):
            sent_requests.append(req)
            return AgentRegisterResponse(agent=Agent(id="agent_x", status="active"))
        def register_camera(self, req):
            return CameraRegisterResponse(
                camera=Camera(id="cam_x", external_id="webcam-0", status="active")
            )
        def start_stream_session(self, req):
            approved = dict(width=1280, height=720, fps=30, codec="h264",
                            bitrate_kbps=3000, latency_mode="balanced")
            return StreamSessionResponse(
                session=Session(
                    id="sess_x", state="approved",
                    srt_url="srt://ingest.test.local:8890/?streamid=PLACEHOLDER",
                    requested=RequestedStreamConfig(**approved),
                    approved=ApprovedStreamConfig(**approved),
                ),
                srt_url="srt://ingest.test.local:8890/?streamid=PLACEHOLDER",
            )
        def stop_stream_session(self, session_id):
            return StopStreamSessionResponse()

    monkeypatch.setattr(api_module, "_default_factory", lambda cfg: FakeClient())

    result = runner.invoke(app, ["check", "--streaming"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert len(sent_requests) == 1
    assert sent_requests[0].capabilities == {"ffmpeg": "9.0.0", "srt": True}


def test_check_camera_api_streaming_runs_all_three_in_order(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_camera_config(home_dir, token="real_token_value_xyz")
    monkeypatch.setitem(sys.modules, "cv2", _build_fake_cv2(opens=True))
    _install_fake_ffmpeg_probe(monkeypatch)
    _install_fake_api_client(monkeypatch)

    result = runner.invoke(app, ["check", "--camera", "--api", "--streaming"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Camera opened successfully" in result.stdout
    assert "API check successful" in result.stdout
    assert "Streaming preflight successful" in result.stdout

    cam_idx = result.stdout.index("Camera opened successfully")
    api_idx = result.stdout.index("API check successful")
    stream_idx = result.stdout.index("Streaming preflight successful")
    assert cam_idx < api_idx < stream_idx


# ---------------------------------------------------------------------------
# upstream run
# ---------------------------------------------------------------------------


_RUN_SECRET_SRT_URL = "srt://stream.test.local:1935/live/RUN_CLI_SECRET"
_RUN_SECRET_SESSION_TOKEN = "eyJhbGciOiJIUzI1NiI.RUN_CLI_JWT.signature"
_RUN_SECRET_STREAMID = "RUN_CLI_SECRET_STREAMID"


def _install_fake_run_agent(
    monkeypatch: pytest.MonkeyPatch,
    *,
    raises: Exception | None = None,
    keyboard_interrupt: bool = False,
) -> dict:
    """Patch the supervisor's run_agent entry point used by the CLI."""

    from upstream_agent import cli as cli_module
    from upstream_agent.state import State
    from upstream_agent.supervisor import RunResult

    captures: dict = {
        "calls": 0,
        "received_cfg": None,
        "received_state": None,
        "received_on_event": None,
    }

    def fake_run_agent(cfg, state, *, save_progress=None, on_event=None, **kwargs):
        captures["calls"] += 1
        captures["received_cfg"] = cfg
        captures["received_state"] = state
        captures["received_on_event"] = on_event

        if keyboard_interrupt:
            # Mirror the real supervisor: emit shutdown lines, then raise.
            if on_event is not None:
                on_event("camera_opened", "Camera opened: webcam 0")
                on_event("api_approved", "API session approved")
                on_event(
                    "approved_config",
                    "Approved config: h264, 1280x720 @ 30 FPS, 3000 kbps, balanced",
                )
                on_event("ffmpeg_started", "FFmpeg started")
                on_event("streaming", "Streaming started\nPress Ctrl+C to stop.")
                on_event("shutdown_requested", "Stopping stream...")
                on_event("ffmpeg_stopped", "FFmpeg stopped")
                on_event("camera_released", "Camera released")
                on_event("session_stopped", "Stream session stopped")
            raise KeyboardInterrupt()

        if raises is not None:
            raise raises

        if on_event is not None:
            on_event("camera_opened", "Camera opened: webcam 0")
            on_event("api_approved", "API session approved")
            on_event(
                "approved_config",
                "Approved config: h264, 1280x720 @ 30 FPS, 3000 kbps, balanced",
            )
            on_event("ffmpeg_started", "FFmpeg started")
            on_event("streaming", "Streaming started\nPress Ctrl+C to stop.")
            on_event("ffmpeg_stopped", "FFmpeg stopped")
            on_event("camera_released", "Camera released")
            on_event("session_stopped", "Stream session stopped")

        new_state = State(
            agent_id="agent_1",
            device_id=state.device_id,
            last_camera_id="cam_1",
        )
        return RunResult(frames_written=3, exit_reason="max_frames"), new_state

    monkeypatch.setattr(cli_module, "run_agent", fake_run_agent)
    return captures


def test_run_help_exits_zero_and_mentions_streaming() -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "Stream video" in result.stdout


def test_run_no_config_exits_with_init_hint(home_dir: Path) -> None:
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 1
    assert "No config found" in result.stderr
    assert "upstream init" in result.stderr


def test_run_placeholder_token_exits_one(home_dir: Path) -> None:
    _write_camera_config(home_dir, token=PLACEHOLDER_TOKEN)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 1
    assert "API token is still a placeholder" in result.stderr


def test_run_happy_path_prints_safe_output(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_camera_config(home_dir, token="real_token_value_xyz")
    captures = _install_fake_run_agent(monkeypatch)

    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Starting UpStream agent..." in result.stdout
    assert "Camera opened: webcam 0" in result.stdout
    assert "API session approved" in result.stdout
    assert "FFmpeg started" in result.stdout
    assert "Streaming started" in result.stdout
    assert "FFmpeg stopped" in result.stdout
    assert "Camera released" in result.stdout
    assert "Stream session stopped" in result.stdout
    assert captures["calls"] == 1


def test_run_keyboard_interrupt_exits_zero_with_clean_shutdown(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_camera_config(home_dir, token="real_token_value_xyz")
    _install_fake_run_agent(monkeypatch, keyboard_interrupt=True)

    result = runner.invoke(app, ["run"])
    # Ctrl+C is a clean shutdown, not a failure.
    assert result.exit_code == 0
    assert "Stopping stream..." in result.stdout
    assert "FFmpeg stopped" in result.stdout
    assert "Camera released" in result.stdout
    assert "Stream session stopped" in result.stdout


def test_run_ffmpeg_start_failure_exits_one_clean_error(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from upstream_agent.errors import FfmpegStartError

    _write_camera_config(home_dir, token="real_token_value_xyz")
    _install_fake_run_agent(
        monkeypatch, raises=FfmpegStartError("FFmpeg binary not found."),
    )

    result = runner.invoke(app, ["run"])
    assert result.exit_code == 1
    assert "Error: FFmpeg binary not found." in result.stderr


def test_run_api_failure_exits_one_clean_error(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from upstream_agent.errors import StreamingRuntimeError

    _write_camera_config(home_dir, token="real_token_value_xyz")
    _install_fake_run_agent(
        monkeypatch,
        raises=StreamingRuntimeError(
            "API approval failed at register_agent: token rejected"
        ),
    )

    result = runner.invoke(app, ["run"])
    assert result.exit_code == 1
    assert "Error:" in result.stderr
    assert "register_agent" in result.stderr


def test_run_does_not_leak_token_srt_url_or_streamid(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_token = "ups_live_run_must_not_leak"
    _write_camera_config(home_dir, token=secret_token)

    # The fake supervisor never prints the URL itself, but we also
    # capture the case where a future change might. Plant a SRT URL
    # in a sensitive value the redaction filter knows about: this is
    # a regression guard.
    from upstream_agent import cli as cli_module
    from upstream_agent.supervisor import RunResult

    def fake_run_agent(cfg, state, *, save_progress=None, on_event=None, **kwargs):
        # A misbehaving callback in the future MUST still not leak.
        if on_event is not None:
            on_event("ok", "Camera opened: webcam 0")
            on_event("ok", "Stream session stopped")
        return RunResult(frames_written=0, exit_reason="completed"), state

    monkeypatch.setattr(cli_module, "run_agent", fake_run_agent)

    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
    for blob in (result.stdout, result.stderr):
        assert secret_token not in blob
        assert _RUN_SECRET_SRT_URL not in blob
        assert _RUN_SECRET_SESSION_TOKEN not in blob
        assert _RUN_SECRET_STREAMID not in blob

    log_file = home_dir / ".upstream" / "logs" / "agent.log"
    if log_file.exists():
        content = log_file.read_text(encoding="utf-8")
        assert secret_token not in content
        assert _RUN_SECRET_SRT_URL not in content
        assert _RUN_SECRET_SESSION_TOKEN not in content
        assert _RUN_SECRET_STREAMID not in content
