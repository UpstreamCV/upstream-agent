"""The Upstream Collector service: receiver + flusher under one lifecycle.

Mirrors :class:`~upstream_agent.interactive.worker.StreamWorker`: a small
object with ``start`` / ``stop`` / ``is_running`` / ``status``, owning the
shared shutdown :class:`threading.Event` and two daemon threads (the HTTP
receiver and the upload flusher). Starting twice is rejected; stopping when
idle is a no-op; the service shuts down cleanly on agent exit.

The spool is created in ``__init__`` (not ``start``) so any batches left on
disk from a previous run are visible via :meth:`status` and drained on the next
start.
"""

from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Callable
from pathlib import Path

from upstream_agent.api.client import UpstreamApiClient
from upstream_agent.collector.flusher import Flusher
from upstream_agent.collector.heartbeat import HeartbeatSender
from upstream_agent.collector.receiver import ObservationReceiver
from upstream_agent.collector.spool import Spool
from upstream_agent.collector.status import CollectorCounters, CollectorStatus
from upstream_agent.config import CollectorConfig, Config
from upstream_agent.runtime.backoff import BackoffPolicy

logger = logging.getLogger(__name__)

_ClientFactory = Callable[[str, str], UpstreamApiClient]
_BackoffFactory = Callable[[], BackoffPolicy]


def _default_client_factory(base_url: str, token: str) -> UpstreamApiClient:
    return UpstreamApiClient(base_url, token)


class CollectorService:
    """Receiver + flusher lifecycle for the local Upstream Collector."""

    def __init__(
        self,
        *,
        config: CollectorConfig,
        base_url: str,
        token: str,
        client_factory: _ClientFactory | None = None,
        counters: CollectorCounters | None = None,
        backoff_factory: _BackoffFactory | None = None,
    ) -> None:
        self._config = config
        self._base_url = base_url
        self._token = token
        self._client_factory = client_factory or _default_client_factory
        self._backoff_factory = backoff_factory
        self._counters = counters or CollectorCounters()
        self._spool = Spool(
            Path(config.spool_dir).expanduser(),
            max_observations=config.max_spool_observations,
            max_bytes=config.max_spool_bytes,
            counters=self._counters,
        )
        self._shutdown = threading.Event()
        self._lock = threading.Lock()
        self._receiver: ObservationReceiver | None = None
        self._receiver_thread: threading.Thread | None = None
        self._flusher_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._running = False

    @classmethod
    def from_config(
        cls,
        cfg: Config,
        *,
        client_factory: _ClientFactory | None = None,
        backoff_factory: _BackoffFactory | None = None,
    ) -> CollectorService:
        return cls(
            config=cfg.collector,
            base_url=cfg.api.base_url,
            token=cfg.api.token,
            client_factory=client_factory,
            backoff_factory=backoff_factory,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Bind the receiver and start both daemon threads.

        Raises :class:`RuntimeError` if already running and :class:`OSError`
        if the port is already in use (the receiver binds eagerly). The shell
        guards on :meth:`is_running` first, so a double ``collector start`` never
        reaches here twice.
        """

        with self._lock:
            if self._running:
                raise RuntimeError("Collector is already running.")
            self._shutdown.clear()
            receiver = ObservationReceiver(
                host=self._config.host,
                port=self._config.port,
                spool=self._spool,
                inbound_token=self._config.inbound_token,
                max_batch_size=self._config.max_batch_size,
                status_provider=self.status,
            )
            self._receiver = receiver
            self._receiver_thread = threading.Thread(
                target=receiver.serve_forever, name="collector-receiver", daemon=True
            )
            self._receiver_thread.start()

            flusher = Flusher(
                spool=self._spool,
                counters=self._counters,
                client_factory=self._client_factory,
                base_url=self._base_url,
                token=self._token,
                flush_interval_seconds=self._config.flush_interval_seconds,
                shutdown=self._shutdown,
                backoff_factory=self._backoff_factory,
            )
            self._flusher_thread = threading.Thread(
                target=flusher.run, name="collector-flusher", daemon=True
            )
            self._flusher_thread.start()

            # Cloud-visible health heartbeat (M1c). 0 disables it.
            if self._config.heartbeat_interval_seconds > 0:
                sender = HeartbeatSender(
                    spool=self._spool,
                    counters=self._counters,
                    source=self._config.source or socket.gethostname(),
                    interval_seconds=self._config.heartbeat_interval_seconds,
                    shutdown=self._shutdown,
                )
                self._heartbeat_thread = threading.Thread(
                    target=sender.run, name="collector-heartbeat", daemon=True
                )
                self._heartbeat_thread.start()
            self._running = True

    def stop(self, timeout: float = 5.0) -> None:
        """Signal shutdown and join both threads. Safe when not running."""

        with self._lock:
            if not self._running:
                return
            self._shutdown.set()
            receiver = self._receiver
            receiver_thread = self._receiver_thread
            flusher_thread = self._flusher_thread
            heartbeat_thread = self._heartbeat_thread
            self._running = False

        # Outside the lock: status() (called from the receiver thread) reads
        # service state without this lock, so joining here can't deadlock.
        if receiver is not None:
            receiver.shutdown()
        if receiver_thread is not None:
            receiver_thread.join(timeout=timeout)
        if flusher_thread is not None:
            flusher_thread.join(timeout=timeout)
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=timeout)
        with self._lock:
            self._receiver = None
            self._receiver_thread = None
            self._flusher_thread = None
            self._heartbeat_thread = None

    def is_running(self) -> bool:
        thread = self._receiver_thread
        return self._running and thread is not None and thread.is_alive()

    # ------------------------------------------------------------------
    # Status (lock-free reads; safe from the receiver thread)
    # ------------------------------------------------------------------

    def status(self) -> CollectorStatus:
        running = self.is_running()
        receiver = self._receiver
        if running and receiver is not None:
            host, port = receiver.host, receiver.port
        else:
            host, port = self._config.host, self._config.port
        batches, observations = self._spool.counts()
        snapshot = self._counters.snapshot()
        return CollectorStatus(
            running=running,
            local_endpoint=f"http://{host}:{port}/v1/observations",
            spooled_batches=batches,
            spooled_observations=observations,
            **snapshot,
        )
