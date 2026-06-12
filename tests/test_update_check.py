"""Update M2 — notify-only version checking.

Pure-module truth tables for ``upstream_agent.update_check`` plus
supervisor integration: the startup bootstrap surfaces exactly one
notice when the server's policy says we're behind, stays silent when
current / offline / dev, throttles the recommended tier via
``state.last_update_notice_at``, and NEVER blocks streaming (refusal is
the M3 milestone, deliberately absent here)."""

from __future__ import annotations

from typing import Any

from tests.test_supervisor import (  # type: ignore[import-not-found]
    FakeCamera,
    _cfg,
    _collect_events,
    _happy_probe,
    _make_factories,
    _state,
)
from tests.test_supervisor_precheck import (  # type: ignore[import-not-found]
    PrecheckApiClient,
)
from upstream_agent.api.schemas import AgentVersionPolicy, BootstrapResponse
from upstream_agent.supervisor import run_agent
from upstream_agent.update_check import (
    RECOMMENDED_NOTICE_SNOOZE_S,
    UpdateStatus,
    VersionPolicy,
    evaluate_policy,
    is_dev_build,
    parse_version,
    should_show_recommended_notice,
    update_notice,
)

# --- parse_version -----------------------------------------------------------


def test_parse_version_truth_table() -> None:
    assert parse_version("0.1.0") == (0, 1, 0)
    assert parse_version("10.20.30") == (10, 20, 30)
    assert parse_version(" 1.2.3 ") == (1, 2, 3)  # tolerate whitespace
    for bad in (None, "", "1.2", "1.2.3.4", "1.2.x", "0.2.0-dev", "0.2.0rc1", "-1.0.0", "v1.2.3"):
        assert parse_version(bad) is None, bad


def test_version_ordering_via_tuples() -> None:
    assert parse_version("0.9.9") < parse_version("0.10.0")  # type: ignore[operator]
    assert parse_version("1.0.0") > parse_version("0.99.99")  # type: ignore[operator]


# --- dev-build detection -----------------------------------------------------


def test_dev_build_detection() -> None:
    assert is_dev_build("0.2.0-dev", env={}) is True
    assert is_dev_build("garbage", env={}) is True
    assert is_dev_build("0.1.0", env={}) is False
    assert is_dev_build("0.1.0", env={"UPSTREAM_AGENT_DEV": "1"}) is True
    assert is_dev_build("0.1.0", env={"UPSTREAM_AGENT_DEV": "0"}) is False


# --- evaluate_policy matrix --------------------------------------------------


def _policy(**ov: Any) -> VersionPolicy:
    base: dict[str, Any] = {
        "latest_version": "0.2.0",
        "minimum_supported_version": "0.1.0",
        "install_hint": "pip install --upgrade upstream-agent",
        "release_notes_url": "https://example.test/releases",
    }
    base.update(ov)
    return VersionPolicy(**base)


def test_evaluate_policy_matrix() -> None:
    env: dict[str, str] = {}
    assert evaluate_policy("0.1.0", None, env) is UpdateStatus.NO_POLICY
    assert evaluate_policy("0.2.0", _policy(), env) is UpdateStatus.UP_TO_DATE
    assert evaluate_policy("0.3.0", _policy(), env) is UpdateStatus.UP_TO_DATE  # ahead is fine
    assert evaluate_policy("0.1.0", _policy(), env) is UpdateStatus.UPDATE_RECOMMENDED
    assert (
        evaluate_policy("0.0.9", _policy(), env) is UpdateStatus.UPDATE_REQUIRED
    )  # below minimum outranks recommended
    assert evaluate_policy("0.2.0-dev", _policy(), env) is UpdateStatus.DEV_BUILD
    assert (
        evaluate_policy("0.1.0", _policy(), {"UPSTREAM_AGENT_DEV": "1"})
        is UpdateStatus.DEV_BUILD
    )
    # Policy present but carrying no parseable facts = no policy.
    empty = VersionPolicy()
    assert evaluate_policy("0.1.0", empty, env) is UpdateStatus.NO_POLICY
    garbage = _policy(latest_version="newest", minimum_supported_version=None)
    assert evaluate_policy("0.1.0", garbage, env) is UpdateStatus.NO_POLICY


def test_update_notice_text() -> None:
    rec = update_notice("0.1.0", _policy(), UpdateStatus.UPDATE_RECOMMENDED)
    assert rec is not None
    assert "v0.2.0" in rec and "v0.1.0" in rec
    assert "pip install --upgrade upstream-agent" in rec
    assert "https://example.test/releases" in rec
    req = update_notice("0.0.9", _policy(), UpdateStatus.UPDATE_REQUIRED)
    assert req is not None
    assert "minimum supported version" in req
    assert update_notice("0.2.0", _policy(), UpdateStatus.UP_TO_DATE) is None
    assert update_notice("0.2.0", _policy(), UpdateStatus.NO_POLICY) is None


def test_recommended_snooze_window() -> None:
    now = 1_000_000.0
    assert should_show_recommended_notice(None, now) is True
    assert should_show_recommended_notice(now - 60, now) is False
    assert (
        should_show_recommended_notice(now - RECOMMENDED_NOTICE_SNOOZE_S - 1, now)
        is True
    )


# --- supervisor integration --------------------------------------------------


class PolicyApiClient(PrecheckApiClient):
    """Bootstrap carries a version policy alongside the (None) limits."""

    def __init__(self, policy: AgentVersionPolicy | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._policy = policy

    def get_bootstrap(self) -> BootstrapResponse:
        return BootstrapResponse(agent_version_policy=self._policy)


def _run_with_policy(
    policy: AgentVersionPolicy | None,
    state: Any | None = None,
) -> tuple[Any, list[tuple[str, str]], Any]:
    client = PolicyApiClient(policy)
    cam = FakeCamera(_cfg().camera, frame_shape=(720, 1280))
    factories = _make_factories(client=client, cam=cam)
    events, on_event = _collect_events()
    run_state = state if state is not None else _state()
    result, state_out = run_agent(
        _cfg(),
        run_state,
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        ffmpeg_probe=_happy_probe(),
        on_event=on_event,
        max_frames=2,
    )
    return result, events, state_out


def test_run_emits_one_notice_when_behind_and_still_streams() -> None:
    policy = AgentVersionPolicy(
        latest_version="9.9.9",
        install_hint="pip install --upgrade upstream-agent",
    )
    result, events, state_out = _run_with_policy(policy)
    notices = [msg for stage, msg in events if stage == "update_notice"]
    assert len(notices) == 1
    assert "9.9.9" in notices[0]
    # Notify-only: streaming proceeded normally.
    assert result.frames_written >= 1
    # The snooze timestamp was persisted on the state object.
    assert state_out.last_update_notice_at is not None


def test_run_is_silent_when_current_or_no_policy() -> None:
    for policy in (None, AgentVersionPolicy(latest_version="0.0.1")):
        _result, events, state_out = _run_with_policy(policy)
        assert [m for s, m in events if s == "update_notice"] == []
        assert state_out.last_update_notice_at is None


def test_recommended_notice_respects_snooze_state() -> None:
    import time as _time

    policy = AgentVersionPolicy(latest_version="9.9.9")
    snoozed = _state()
    snoozed.last_update_notice_at = _time.time() - 60  # shown a minute ago
    _result, events, _state_out = _run_with_policy(policy, state=snoozed)
    assert [m for s, m in events if s == "update_notice"] == []


def test_run_offline_changes_nothing() -> None:
    """Bootstrap failure (offline) -> no notice, streaming behavior exactly
    as before (the existing bootstrap_unavailable event still fires)."""
    from upstream_agent.errors import ApiConnectionError

    client = PrecheckApiClient(bootstrap_raises=ApiConnectionError("no route"))
    cam = FakeCamera(_cfg().camera, frame_shape=(720, 1280))
    factories = _make_factories(client=client, cam=cam)
    events, on_event = _collect_events()
    result, _ = run_agent(
        _cfg(),
        _state(),
        camera_factory=factories["camera_factory"],
        client_factory=factories["client_factory"],
        ffmpeg_factory=factories["ffmpeg_factory"],
        ffmpeg_probe=_happy_probe(),
        on_event=on_event,
        max_frames=2,
    )
    assert [m for s, m in events if s == "update_notice"] == []
    assert any(s == "bootstrap_unavailable" for s, _ in events)
    assert result.frames_written >= 1
