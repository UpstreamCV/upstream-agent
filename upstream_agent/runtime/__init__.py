"""Runtime instrumentation for the live streaming loop.

:mod:`upstream_agent.runtime.stats` exposes :class:`RuntimeStats`, a
thread-safe counter object that the supervisor updates as frames are
written and the heartbeat reporter reads once per interval. The
snapshot type returned to readers is :class:`StatsSnapshot`, a frozen
dataclass — readers can never accidentally mutate live state.

:mod:`upstream_agent.runtime.backoff` exposes :class:`BackoffPolicy`,
the bounded-exponential-backoff helper the supervisor's recovery loop
uses between attempts after a transient FFmpeg, SRT, or API failure.
"""

from upstream_agent.runtime.backoff import (
    DEFAULT_INITIAL_DELAY_S,
    DEFAULT_JITTER_FACTOR,
    DEFAULT_MAX_DELAY_S,
    DEFAULT_MULTIPLIER,
    BackoffPolicy,
)
from upstream_agent.runtime.stats import RuntimeStats, StatsSnapshot

__all__ = [
    "BackoffPolicy",
    "DEFAULT_INITIAL_DELAY_S",
    "DEFAULT_JITTER_FACTOR",
    "DEFAULT_MAX_DELAY_S",
    "DEFAULT_MULTIPLIER",
    "RuntimeStats",
    "StatsSnapshot",
]
