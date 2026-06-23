"""Upstream Reliability local collector.

Receives observation batches from the customer's CV system on a local HTTP
endpoint, buffers them in a durable spool, and uploads them to Upstream Cloud
(``POST /v1/reliability/observations``). See :class:`CollectorService`.
"""

from __future__ import annotations

from upstream_agent.collector.service import CollectorService
from upstream_agent.collector.status import CollectorCounters, CollectorStatus

__all__ = ["CollectorCounters", "CollectorService", "CollectorStatus"]
