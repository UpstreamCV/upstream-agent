"""Tests for :class:`upstream_agent.runtime.backoff.BackoffPolicy`."""

from __future__ import annotations

import threading
import time

import pytest

from upstream_agent.runtime.backoff import BackoffPolicy


def _no_jitter_rng() -> float:
    """Return 0.5 — the centre of the [0,1) interval, so jitter swing is 0."""

    return 0.5


def test_default_policy_starts_at_initial_with_no_jitter() -> None:
    policy = BackoffPolicy(rng=_no_jitter_rng)
    assert policy.next_delay() == pytest.approx(1.0)


def test_default_policy_progresses_geometrically() -> None:
    policy = BackoffPolicy(rng=_no_jitter_rng)
    delays = [policy.next_delay() for _ in range(6)]
    # 1, 2, 4, 8, 16, 30 (clamped to max_delay)
    assert delays[0] == pytest.approx(1.0)
    assert delays[1] == pytest.approx(2.0)
    assert delays[2] == pytest.approx(4.0)
    assert delays[3] == pytest.approx(8.0)
    assert delays[4] == pytest.approx(16.0)
    assert delays[5] == pytest.approx(30.0)


def test_max_delay_caps_growth() -> None:
    policy = BackoffPolicy(
        initial=10.0, multiplier=4.0, max_delay=15.0, jitter_factor=0.0
    )
    # 10 → 15 (clamped from 40)
    assert policy.next_delay() == pytest.approx(10.0)
    assert policy.next_delay() == pytest.approx(15.0)
    assert policy.next_delay() == pytest.approx(15.0)


def test_reset_returns_to_initial() -> None:
    policy = BackoffPolicy(rng=_no_jitter_rng)
    policy.next_delay()  # advances to 2.0
    policy.next_delay()  # advances to 4.0
    policy.reset()
    assert policy.next_delay() == pytest.approx(1.0)


def test_jitter_swing_within_bounds() -> None:
    """A deterministic RNG yields delays inside [base*(1-jf), base*(1+jf)]."""

    seed = 0.0
    sequence = iter([0.0, 1.0, 0.25, 0.75])

    def rng() -> float:
        try:
            return next(sequence)
        except StopIteration:  # pragma: no cover - defensive
            return seed

    policy = BackoffPolicy(
        initial=10.0, multiplier=1.0, max_delay=100.0, jitter_factor=0.2, rng=rng
    )
    # rng=0.0 → swing = -1.0 → delay = 10 * (1 + -1*0.2) = 8.0
    # rng=1.0 → swing = +1.0 → delay = 10 * (1 + 1*0.2) = 12.0 (multiplier=1 keeps base at 10)
    # rng=0.25 → swing = -0.5 → 10 * 0.9 = 9.0
    # rng=0.75 → swing = +0.5 → 10 * 1.1 = 11.0
    assert policy.next_delay() == pytest.approx(8.0)
    assert policy.next_delay() == pytest.approx(12.0)
    assert policy.next_delay() == pytest.approx(9.0)
    assert policy.next_delay() == pytest.approx(11.0)


def test_jitter_zero_returns_exact_base() -> None:
    policy = BackoffPolicy(initial=2.0, jitter_factor=0.0)
    # Even with the real rng, jitter_factor=0 means the swing is ignored.
    assert policy.next_delay() == pytest.approx(2.0)


def test_invalid_initial_rejected() -> None:
    with pytest.raises(ValueError):
        BackoffPolicy(initial=-1.0)


def test_invalid_multiplier_rejected() -> None:
    with pytest.raises(ValueError):
        BackoffPolicy(multiplier=0.5)


def test_invalid_max_delay_rejected() -> None:
    with pytest.raises(ValueError):
        BackoffPolicy(initial=10.0, max_delay=1.0)


def test_invalid_jitter_factor_rejected() -> None:
    with pytest.raises(ValueError):
        BackoffPolicy(jitter_factor=1.5)


def test_sleep_returns_false_for_full_wait() -> None:
    policy = BackoffPolicy()
    event = threading.Event()
    start = time.monotonic()
    interrupted = policy.sleep(0.05, event)
    elapsed = time.monotonic() - start
    assert interrupted is False
    assert elapsed >= 0.04


def test_sleep_returns_true_when_event_set_concurrently() -> None:
    policy = BackoffPolicy()
    event = threading.Event()

    def _setter() -> None:
        time.sleep(0.02)
        event.set()

    threading.Thread(target=_setter, daemon=True).start()
    start = time.monotonic()
    interrupted = policy.sleep(5.0, event)
    elapsed = time.monotonic() - start
    assert interrupted is True
    # Woke up within ~100ms; well below the requested 5s.
    assert elapsed < 1.0


def test_sleep_with_event_already_set_returns_immediately() -> None:
    policy = BackoffPolicy()
    event = threading.Event()
    event.set()
    start = time.monotonic()
    interrupted = policy.sleep(2.0, event)
    elapsed = time.monotonic() - start
    assert interrupted is True
    assert elapsed < 0.5


def test_sleep_with_zero_delay_is_noop() -> None:
    policy = BackoffPolicy()
    event = threading.Event()
    start = time.monotonic()
    interrupted = policy.sleep(0.0, event)
    elapsed = time.monotonic() - start
    assert interrupted is False
    assert elapsed < 0.05


def test_sleep_with_zero_delay_honors_already_set_event() -> None:
    policy = BackoffPolicy()
    event = threading.Event()
    event.set()
    interrupted = policy.sleep(0.0, event)
    assert interrupted is True
