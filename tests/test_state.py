"""Tests for the local state manager."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from upstream_agent.state import (
    State,
    default_state_path,
    load_state,
    save_state,
)


def _is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def test_default_state_path_is_under_dot_upstream(home_dir: Path) -> None:
    assert default_state_path() == home_dir / ".upstream" / "agent_state.json"


def test_load_state_creates_file_when_missing(tmp_path: Path) -> None:
    target = tmp_path / "agent_state.json"
    assert not target.exists()

    state = load_state(target)

    assert target.exists()
    assert _is_valid_uuid(state.device_id)
    assert state.agent_id is None
    assert state.last_camera_id is None


def test_load_state_creates_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / "agent_state.json"
    state = load_state(target)
    assert target.exists()
    assert _is_valid_uuid(state.device_id)


def test_load_state_reuses_existing_device_id(tmp_path: Path) -> None:
    target = tmp_path / "agent_state.json"
    first = load_state(target)
    second = load_state(target)
    assert first.device_id == second.device_id


def test_load_state_preserves_agent_id_and_last_camera(tmp_path: Path) -> None:
    target = tmp_path / "agent_state.json"
    save_state(
        State(
            agent_id="agent_123",
            device_id="device-abc",
            last_camera_id="cam_456",
        ),
        target,
    )
    loaded = load_state(target)
    assert loaded.agent_id == "agent_123"
    assert loaded.device_id == "device-abc"
    assert loaded.last_camera_id == "cam_456"


def test_load_state_handles_corrupt_json(tmp_path: Path) -> None:
    target = tmp_path / "agent_state.json"
    target.write_text("{not valid json here", encoding="utf-8")

    state = load_state(target)

    # Returns fresh state.
    assert _is_valid_uuid(state.device_id)
    assert state.agent_id is None

    # Original corrupt file preserved under .corrupt-<ts> suffix.
    backups = list(tmp_path.glob("agent_state.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not valid json here"

    # And a fresh valid file was written in its place.
    assert target.exists()
    new_state = json.loads(target.read_text(encoding="utf-8"))
    assert new_state["device_id"] == state.device_id


def test_load_state_handles_wrong_shape(tmp_path: Path) -> None:
    target = tmp_path / "agent_state.json"
    # Valid JSON but missing required device_id.
    target.write_text(json.dumps({"agent_id": "a"}), encoding="utf-8")

    state = load_state(target)

    assert _is_valid_uuid(state.device_id)
    backups = list(tmp_path.glob("agent_state.json.corrupt-*"))
    assert len(backups) == 1


def test_save_state_writes_atomically_via_tmp_file(
    tmp_path: Path, monkeypatch
) -> None:
    """Verify the write strategy uses a tmp file + os.replace, not a direct write."""

    target = tmp_path / "agent_state.json"
    state = State(device_id="abc-123")

    seen_tmp: list[Path] = []
    real_write_text = Path.write_text

    def spy_write_text(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.name.endswith(".tmp"):
            seen_tmp.append(self)
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", spy_write_text)
    save_state(state, target)

    assert seen_tmp, "save_state should write through a .tmp sibling first"
    # The tmp file is renamed onto the target — should no longer exist.
    assert not seen_tmp[0].exists()
    assert target.exists()


def test_state_schema_does_not_store_secrets() -> None:
    """Lock the field set so future edits can't accidentally add a token field."""

    assert set(State.model_fields.keys()) == {
        "agent_id",
        "device_id",
        "last_camera_id",
        # Update M2: epoch-seconds of the last "update recommended" notice
        # (a throttle timestamp — not a secret, not an identifier).
        "last_update_notice_at",
    }


def test_state_schema_rejects_extra_fields() -> None:
    """A corrupt file with an unexpected token-shaped field is treated as corrupt."""

    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        State.model_validate(
            {
                "device_id": "d",
                "api_token": "sk_live_should_be_rejected",
            }
        )
