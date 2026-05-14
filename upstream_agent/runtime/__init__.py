"""Runtime instrumentation for the live streaming loop.

:mod:`upstream_agent.runtime.stats` exposes :class:`RuntimeStats`, a
thread-safe counter object that the supervisor updates as frames are
written and the heartbeat reporter reads once per interval. The
snapshot type returned to readers is :class:`StatsSnapshot`, a frozen
dataclass — readers can never accidentally mutate live state.
"""

from upstream_agent.runtime.stats import RuntimeStats, StatsSnapshot

__all__ = ["RuntimeStats", "StatsSnapshot"]
