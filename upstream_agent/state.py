"""Local agent state persisted at ``~/.upstream/agent_state.json``.

State stores only stable identifiers — never API tokens, session
tokens, or any other secret. The ``device_id`` is generated once on
first run and reused thereafter so the API server can recognise the
same physical install across restarts.

If the state file is corrupt (unparseable JSON, wrong shape, etc.) it
is renamed to ``agent_state.json.corrupt-<unix-ts>`` and a fresh state
is generated. The corrupt file is preserved so the user can inspect it
to recover an ``agent_id`` if needed.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from upstream_agent.errors import StateError

logger = logging.getLogger(__name__)


class State(BaseModel):
    """Persisted local agent state.

    Deliberately small: only stable identifiers. No tokens, no
    short-lived session credentials.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str | None = None
    device_id: str = Field(..., min_length=1)
    last_camera_id: str | None = None
    # Update M2: when the last "update recommended" notice was shown
    # (epoch seconds) — throttles the reminder to once per snooze window.
    # Required-update notices must never consult this.
    last_update_notice_at: float | None = None


def default_state_path() -> Path:
    """Default user state path (``~/.upstream/agent_state.json``)."""

    return Path.home() / ".upstream" / "agent_state.json"


def _fresh_state() -> State:
    return State(device_id=str(uuid.uuid4()))


def load_state(path: Path | None = None) -> State:
    """Load the agent state from disk, creating it if missing or corrupt.

    Always returns a valid :class:`State` with a non-empty ``device_id``.
    The parent directory is created if it does not exist. A corrupt file
    is renamed with a ``.corrupt-<unix-ts>`` suffix and a fresh state is
    generated.
    """

    target = path if path is not None else default_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    if not target.exists():
        state = _fresh_state()
        save_state(state, target)
        return state

    try:
        raw = target.read_text(encoding="utf-8")
        data = json.loads(raw)
        state = State.model_validate(data)
        return state
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        backup = target.with_name(f"{target.name}.corrupt-{int(time.time())}")
        try:
            target.rename(backup)
        except OSError:
            # If we can't even rename it, surface that — but only after
            # logging so the operator has a breadcrumb.
            logger.warning(
                "could not rename corrupt state file %s: %s", target, exc
            )
            raise StateError(
                f"State file {target} is corrupt and could not be moved aside: {exc}"
            ) from None
        logger.warning(
            "state file %s was corrupt (%s); renamed to %s and starting fresh.",
            target,
            exc.__class__.__name__,
            backup,
        )
        state = _fresh_state()
        save_state(state, target)
        return state


def save_state(state: State, path: Path | None = None) -> None:
    """Atomically write ``state`` to ``path``.

    Writes to a sibling tmp file and renames into place so an
    interrupted write cannot leave a half-written file behind.
    """

    target = path if path is not None else default_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = target.with_name(target.name + ".tmp")
    payload = state.model_dump_json(indent=2)
    tmp_path.write_text(payload, encoding="utf-8")
    os.replace(tmp_path, target)
