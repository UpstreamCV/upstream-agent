"""Bounded exponential backoff with jitter and interruptible sleep.

The supervisor's recovery loop calls :meth:`BackoffPolicy.next_delay`
to pick a wait between two streaming attempts, then
:meth:`BackoffPolicy.sleep` to actually wait. The sleep is built on
:meth:`threading.Event.wait` so a shutdown signal — either Ctrl+C in
the main thread or an explicit ``shutdown.set()`` from elsewhere —
wakes the wait immediately rather than holding the agent in the field
for up to 30 s after the user has asked it to stop.

Defaults match the M7 spec: 1 s initial, ×2 each step, capped at 30 s,
with ±20% multiplicative jitter. The policy keeps internal step state
so successive ``next_delay()`` calls grow geometrically until
:meth:`reset` is called (typically after a successful streaming
attempt). The ``rng`` injection point lets tests assert progression
deterministically.
"""

from __future__ import annotations

import logging
import random
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

DEFAULT_INITIAL_DELAY_S: float = 1.0
DEFAULT_MAX_DELAY_S: float = 30.0
DEFAULT_MULTIPLIER: float = 2.0
DEFAULT_JITTER_FACTOR: float = 0.2


class BackoffPolicy:
    """Bounded exponential backoff with multiplicative jitter.

    Construction is cheap; the policy keeps two pieces of state — the
    current geometric step and the cached "next" base delay — both
    plain floats with no locking. The supervisor is single-threaded
    over the recovery loop, so callers do not need to share a policy
    across threads.
    """

    def __init__(
        self,
        *,
        initial: float = DEFAULT_INITIAL_DELAY_S,
        multiplier: float = DEFAULT_MULTIPLIER,
        max_delay: float = DEFAULT_MAX_DELAY_S,
        jitter_factor: float = DEFAULT_JITTER_FACTOR,
        rng: Callable[[], float] | None = None,
    ) -> None:
        if initial < 0:
            raise ValueError("initial must be non-negative")
        if multiplier < 1.0:
            raise ValueError("multiplier must be >= 1.0")
        if max_delay < initial:
            raise ValueError("max_delay must be >= initial")
        if not 0.0 <= jitter_factor <= 1.0:
            raise ValueError("jitter_factor must be in [0.0, 1.0]")

        self._initial = float(initial)
        self._multiplier = float(multiplier)
        self._max_delay = float(max_delay)
        self._jitter_factor = float(jitter_factor)
        self._rng: Callable[[], float] = rng if rng is not None else random.random
        self._base = self._initial

    def next_delay(self) -> float:
        """Return the next delay in seconds and advance the policy.

        Applies ±``jitter_factor`` multiplicative jitter to the current
        base delay and caps the result at ``max_delay``. The internal
        base delay then advances geometrically (also capped) so a
        subsequent call returns a longer wait. ``reset()`` restores
        the policy to its initial step.
        """

        base = min(self._base, self._max_delay)
        delay = _apply_jitter(base, self._jitter_factor, self._rng)
        delay = max(0.0, min(delay, self._max_delay))
        self._base = min(self._base * self._multiplier, self._max_delay)
        return delay

    def reset(self) -> None:
        """Reset to the initial step. Call after a successful attempt."""

        self._base = self._initial

    def sleep(self, delay: float, shutdown: threading.Event) -> bool:
        """Sleep up to ``delay`` seconds, interruptible by ``shutdown``.

        Returns ``True`` if the sleep was interrupted (either by the
        shutdown event being set, or by the wait raising
        :class:`KeyboardInterrupt`), and ``False`` if the full delay
        elapsed. ``KeyboardInterrupt`` is re-raised — callers that
        want a "did we get interrupted?" boolean only should call
        :meth:`sleep_silenced` instead.

        A ``delay`` of zero or negative returns ``False`` immediately
        without touching the event. A delay below 1 ms is also treated
        as a no-op so tests with ``initial=0`` don't sit in
        ``Event.wait(0)`` unnecessarily.
        """

        if delay <= 0.001:
            # Still honor an already-set shutdown so the caller can
            # short-circuit even on a zero-length sleep.
            return shutdown.is_set()
        return shutdown.wait(delay)


def _apply_jitter(
    base: float,
    jitter_factor: float,
    rng: Callable[[], float],
) -> float:
    """Return ``base`` multiplied by a factor in ``[1-jf, 1+jf]``."""

    if jitter_factor <= 0.0:
        return base
    # rng() returns a float in [0.0, 1.0). Map to [-1.0, 1.0).
    swing = (rng() * 2.0) - 1.0
    return base * (1.0 + swing * jitter_factor)
