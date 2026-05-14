"""Background heartbeat reporting for the live streaming runtime.

:class:`HeartbeatReporter` runs on its own daemon thread and POSTs a
heartbeat to the API every :data:`DEFAULT_HEARTBEAT_INTERVAL_S`
seconds while ``upstream run`` is streaming. See
:mod:`upstream_agent.heartbeat.reporter` for the full contract.
"""

from upstream_agent.heartbeat.reporter import (
    DEFAULT_HEARTBEAT_INTERVAL_S,
    HeartbeatReporter,
)

__all__ = ["DEFAULT_HEARTBEAT_INTERVAL_S", "HeartbeatReporter"]
