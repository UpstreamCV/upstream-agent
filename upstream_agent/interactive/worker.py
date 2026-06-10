"""Background stream workers for the interactive shell.

A :class:`StreamWorker` runs one streaming session — the existing
:func:`upstream_agent.supervisor.run_agent` recovery loop — on a daemon
thread, stoppable from the shell via the supervisor's external
``shutdown`` Event. Supervisor events are mirrored into the logging
system (so they land in the ring buffer + file, never on the prompt)
and folded into a :class:`WorkerStatus` snapshot the shell renders.

:class:`WorkerRegistry` maps camera name → worker. Today's agent config
drives one camera at a time, but the registry is N-ready so multi-camera
support (upstream-agent#2) slots in without reshaping the shell.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from upstream_agent.config import Config
from upstream_agent.supervisor import run_agent

logger = logging.getLogger(__name__)

WorkerState = Literal[
    "idle",
    "starting",
    "streaming",
    "reconnecting",
    "stopped",
    "error",
]

# Supervisor event stages -> coarse worker state. Stages not listed keep
# the current state (they're informational).
_STAGE_TO_STATE: dict[str, WorkerState] = {
    "camera_opened": "starting",
    "api_approved": "starting",
    "ffmpeg_started": "starting",
    "waiting_for_ingest": "starting",
    "streaming": "streaming",
    "ingest_confirmed": "streaming",
    "reconnecting": "reconnecting",
    "reconnect_wait": "reconnecting",
    "camera_lost": "reconnecting",
    "limits_rejected": "error",
}


@dataclass(frozen=True)
class WorkerStatus:
    """Point-in-time snapshot of one worker, safe to render anywhere."""

    name: str
    state: WorkerState
    last_stage: str | None = None
    last_message: str | None = None
    error: str | None = None


class StreamWorker:
    """One streaming run on a daemon thread.

    ``run_fn`` is injectable for tests; production uses
    :func:`upstream_agent.supervisor.run_agent`. The worker owns the
    shutdown Event and hands it to the supervisor, so :meth:`stop` works
    from any thread without a KeyboardInterrupt.
    """

    def __init__(
        self,
        cfg: Config,
        state: Any,
        *,
        name: str | None = None,
        save_progress: Callable[[Any], None] | None = None,
        run_fn: Callable[..., Any] = run_agent,
        run_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._cfg = cfg
        self._state = state
        self.name = name or cfg.camera.name
        self._save_progress = save_progress
        self._run_fn = run_fn
        self._run_kwargs = dict(run_kwargs or {})
        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._worker_state: WorkerState = "idle"
        self._last_stage: str | None = None
        self._last_message: str | None = None
        self._error: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError(f"Worker '{self.name}' is already running.")
        self._shutdown.clear()
        with self._lock:
            self._worker_state = "starting"
            self._error = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"stream-worker-{self.name}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Signal shutdown and wait for the run to finish."""

        self._shutdown.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        with self._lock:
            if self._worker_state not in ("stopped", "error"):
                self._worker_state = "stopped"

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def status(self) -> WorkerStatus:
        with self._lock:
            return WorkerStatus(
                name=self.name,
                state=self._worker_state,
                last_stage=self._last_stage,
                last_message=self._last_message,
                error=self._error,
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _on_event(self, stage: str, message: str) -> None:
        # Logged (not printed): the shell's logging setup routes this to
        # the ring buffer + file, keeping the prompt clean. The redaction
        # filter applies as usual.
        logger.info("[%s] %s: %s", self.name, stage, message)
        with self._lock:
            self._last_stage = stage
            self._last_message = message
            new_state = _STAGE_TO_STATE.get(stage)
            if new_state is not None:
                self._worker_state = new_state
            if stage == "limits_rejected":
                self._error = message

    def _run(self) -> None:
        try:
            self._run_fn(
                self._cfg,
                self._state,
                shutdown=self._shutdown,
                on_event=self._on_event,
                save_progress=self._save_progress,
                **self._run_kwargs,
            )
        except Exception as exc:
            logger.error("[%s] stream worker exited with error: %s", self.name, exc)
            with self._lock:
                self._worker_state = "error"
                self._error = f"{exc.__class__.__name__}: {exc}"
        else:
            with self._lock:
                if self._worker_state != "error":
                    self._worker_state = "stopped"


class WorkerRegistry:
    """Name-keyed registry of stream workers."""

    def __init__(self) -> None:
        self._workers: dict[str, StreamWorker] = {}
        self._lock = threading.Lock()

    def get(self, name: str) -> StreamWorker | None:
        with self._lock:
            return self._workers.get(name)

    def add(self, worker: StreamWorker) -> None:
        with self._lock:
            self._workers[worker.name] = worker

    def remove(self, name: str) -> None:
        with self._lock:
            self._workers.pop(name, None)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._workers)

    def statuses(self) -> list[WorkerStatus]:
        with self._lock:
            workers = list(self._workers.values())
        return [w.status for w in workers]

    def running(self) -> list[StreamWorker]:
        with self._lock:
            workers = list(self._workers.values())
        return [w for w in workers if w.is_running]

    def stop_all(self, timeout: float = 10.0) -> None:
        for worker in self.running():
            worker.stop(timeout=timeout)
