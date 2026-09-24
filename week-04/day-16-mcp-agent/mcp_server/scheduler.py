"""Background scheduler that runs due search tasks inside the MCP server.

The scheduler lives in the MCP server process, next to ``search_web``, so a run
uses the same configured search service and the same search API key, and works
even when the backend and the browser are stopped. It is started only from
:meth:`mcp_server.server.MCPServer.run`, never from ``_build``: building the
server or listing tools must stay side-effect free.

The schedule is stored in the database, so a restart resumes it. Each due slot
is claimed with a compare-and-swap before the run, which makes the claim
idempotent across ticks and across two processes.
"""

from __future__ import annotations

import logging
import threading
import time

from mcp_server.config import resolve_task_tick_seconds

DEFAULT_LIMIT_PER_TICK = 5


class TaskScheduler:
    """A daemon thread that wakes up every tick and runs due tasks."""

    def __init__(
        self,
        task_service,
        *,
        tick_seconds=None,
        clock=time.time,
        logger=None,
        limit_per_tick: int = DEFAULT_LIMIT_PER_TICK,
    ):
        self._service = task_service
        self._tick_seconds = float(
            tick_seconds if tick_seconds is not None else resolve_task_tick_seconds()
        )
        self._clock = clock
        self._logger = logger or logging.getLogger("mcp_server.scheduler")
        self._limit = max(int(limit_per_tick), 1)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> "TaskScheduler":
        if self.running:
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="task-scheduler", daemon=True
        )
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self._tick_seconds):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - a tick error must not kill it
                self._logger.warning("scheduler tick failed: %s", exc)

    def tick(self) -> list:
        """Run every task due now; return the outcomes of the tasks it ran."""
        now = float(self._clock())
        outcomes = []
        for task in self._service.claim_due_tasks(now=now, limit=self._limit):
            try:
                outcomes.append(self._service.execute_claimed(task))
            except Exception as exc:  # noqa: BLE001 - one task must not stop the tick
                self._logger.warning("scheduler run failed: %s", exc)
        return outcomes
