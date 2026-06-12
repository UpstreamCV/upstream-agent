"""Notify-only agent version checking (update M2).

The server owns the version-policy *facts* (latest version, minimum
supported version, release notes URL, install hint) and ships them as an
optional ``agent_version_policy`` object on the bootstrap response the
agent already fetches — zero extra round-trips. This module owns the
*consequences*: comparing the running ``__version__`` against the policy
and producing the user-facing notice text.

Design rules (see the approved auto-update plan):

- **Never block, never update.** M2 is notify-only: the worst outcome is
  one short warning line. Streaming refusal below the minimum version is
  a LATER milestone (M3) and is deliberately not wired here.
- **Offline / no policy = silent.** A missing or malformed policy means
  no notice at all; an unreachable server changes nothing about today's
  behavior.
- **Dev builds are exempt.** A version that doesn't parse as strict
  ``X.Y.Z`` (e.g. ``0.2.0-dev``) or ``UPSTREAM_AGENT_DEV=1`` reduces all
  notices to a debug log.
- **Pure + dependency-free.** Strict ``X.Y.Z`` comparison implemented
  here (``packaging`` is not a dependency); everything is unit-testable
  with no I/O.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

#: How long an ``update_recommended`` notice stays snoozed after being
#: shown (stored in ``agent_state.json``). Required notices (M3) must
#: never consult this.
RECOMMENDED_NOTICE_SNOOZE_S = 24 * 60 * 60


class UpdateStatus(str, Enum):
    NO_POLICY = "no_policy"
    DEV_BUILD = "dev_build"
    UP_TO_DATE = "up_to_date"
    UPDATE_RECOMMENDED = "update_recommended"
    UPDATE_REQUIRED = "update_required"


@dataclass(frozen=True)
class VersionPolicy:
    """The server-owned policy facts, as parsed off the bootstrap
    response. All fields optional — the server omits what it doesn't
    configure."""

    latest_version: str | None = None
    minimum_supported_version: str | None = None
    severity_hint: str | None = None
    release_notes_url: str | None = None
    install_hint: str | None = None


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    """Strict ``X.Y.Z`` -> comparable tuple; anything else -> ``None``.

    Deliberately strict: a suffix like ``0.2.0-dev`` or ``0.2.0rc1`` does
    NOT parse — locally that marks a dev build (exempt from notices), and
    a non-parsing server field disables the corresponding comparison
    rather than guessing.
    """
    if not text:
        return None
    parts = text.strip().split(".")
    if len(parts) != 3:
        return None
    try:
        major, minor, patch = (int(p, 10) for p in parts)
    except ValueError:
        return None
    if major < 0 or minor < 0 or patch < 0:
        return None
    return (major, minor, patch)


def is_dev_build(current_version: str, env: dict[str, str] | None = None) -> bool:
    """Dev builds are exempt from all update notices: explicit opt-out via
    ``UPSTREAM_AGENT_DEV=1`` or a version string that isn't strict
    ``X.Y.Z``."""
    environ = env if env is not None else dict(os.environ)
    if environ.get("UPSTREAM_AGENT_DEV", "") == "1":
        return True
    return parse_version(current_version) is None


def evaluate_policy(
    current_version: str,
    policy: VersionPolicy | None,
    env: dict[str, str] | None = None,
) -> UpdateStatus:
    """The whole decision table. Never raises."""
    if policy is None:
        return UpdateStatus.NO_POLICY
    if is_dev_build(current_version, env):
        return UpdateStatus.DEV_BUILD
    current = parse_version(current_version)
    assert current is not None  # guaranteed by the dev-build check
    minimum = parse_version(policy.minimum_supported_version)
    if minimum is not None and current < minimum:
        return UpdateStatus.UPDATE_REQUIRED
    latest = parse_version(policy.latest_version)
    if latest is not None and current < latest:
        return UpdateStatus.UPDATE_RECOMMENDED
    if latest is None and minimum is None:
        return UpdateStatus.NO_POLICY  # policy present but carries no facts
    return UpdateStatus.UP_TO_DATE


def update_notice(
    current_version: str,
    policy: VersionPolicy,
    status: UpdateStatus,
) -> str | None:
    """The user-facing notice line(s) for a status, or ``None`` for the
    silent statuses. Short on purpose — one glanceable message."""
    if status is UpdateStatus.UPDATE_RECOMMENDED:
        lines = [
            f"A newer Upstream agent is available: v{policy.latest_version} "
            f"(you are running v{current_version})."
        ]
        if policy.install_hint:
            lines.append(f"Update with: {policy.install_hint}")
        if policy.release_notes_url:
            lines.append(f"Release notes: {policy.release_notes_url}")
        return "\n".join(lines)
    if status is UpdateStatus.UPDATE_REQUIRED:
        lines = [
            f"This agent version (v{current_version}) is older than the minimum "
            f"supported version (v{policy.minimum_supported_version}).",
            "Streaming may stop working until you update.",
        ]
        if policy.install_hint:
            lines.append(f"Update with: {policy.install_hint}")
        if policy.release_notes_url:
            lines.append(f"Release notes: {policy.release_notes_url}")
        return "\n".join(lines)
    return None


def should_show_recommended_notice(
    last_notice_at_epoch_s: float | None,
    now_epoch_s: float,
) -> bool:
    """Throttle for the RECOMMENDED tier only (once per snooze window per
    machine). Required notices (M3) must bypass this entirely."""
    if last_notice_at_epoch_s is None:
        return True
    return (now_epoch_s - last_notice_at_epoch_s) >= RECOMMENDED_NOTICE_SNOOZE_S
