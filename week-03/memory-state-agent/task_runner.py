"""Process-level registry of background task step runs.

A step run (``run_step``/``retry``) is a provider call that must survive any
Streamlit rerun and a repeated click. The run therefore lives in an ordinary
worker thread of the server process, not in ``st.session_state``: Streamlit can
stop the script and start a fresh run at any moment, so session state cannot own
a call that is already in flight. The registry is a process singleton shared by
every session and task; the token is only the session's handle on its own run.

There is deliberately no TTL and no session-based busy flag. A live run is
recognized by ``StepRunRecord.is_live()`` (the status is ``running`` *and* the
worker thread is still alive), so a restart of the process drops the whole
registry and can never leave a false busy state behind. ``step_run_busy`` is
derived from the live record, so a finished or orphaned entry does not disable
the button forever; the UI is expected to discard an entry once it has read the
outcome.

The module imports no Streamlit and touches no session state: the worker only
writes to its record under a lock, and the UI reads copies through
``snapshot()``.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from tasks import ACTION_RETRY

STEP_RUN_RUNNING = "running"
STEP_RUN_DONE = "done"
STEP_RUN_ERROR = "error"

_STOPPED_UNEXPECTEDLY = "The step run stopped unexpectedly."


@dataclass
class StepRunRecord:
    """Mutable state of one background step run.

    The ``worker`` thread owns ``_finish``/``_fail``/``set_text``; the UI thread
    only reads ``snapshot()`` and ``is_live()``. Every mutation and the snapshot
    copy are guarded by ``_lock``, so a streamed chunk cannot tear the state the
    app renders in parallel.
    """

    token: str
    task_id: object
    chat_id: object
    action: str
    status: str = STEP_RUN_RUNNING
    text: str = ""
    error: str | None = None
    result: object | None = None
    thread: object | None = None
    started_at: float = field(default_factory=time.time)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def set_text(self, text) -> None:
        """Record the accumulated streamed text (called from the worker)."""
        with self._lock:
            self.text = "" if text is None else str(text)

    def snapshot(self) -> dict:
        """Return a plain copy of the readable fields, safe for the UI thread."""
        with self._lock:
            return {
                "token": self.token,
                "task_id": self.task_id,
                "chat_id": self.chat_id,
                "action": self.action,
                "status": self.status,
                "text": self.text,
                "error": self.error,
                "started_at": self.started_at,
            }

    def is_live(self) -> bool:
        """Whether the worker is still running; false for a finished/orphan."""
        return (
            self.status == STEP_RUN_RUNNING
            and self.thread is not None
            and self.thread.is_alive()
        )

    def _finish(self, result) -> None:
        """Settle a returned action result as done or error."""
        with self._lock:
            self.result = result
            if result is not None and result.ok and result.stage_result is not None:
                self.status = STEP_RUN_DONE
                text = result.stage_result.text
                if text:
                    self.text = text
            else:
                self.status = STEP_RUN_ERROR
                self.error = (
                    getattr(result, "error_message", None)
                    or "The action could not be completed."
                )

    def _fail(self, message) -> None:
        """Settle an exception raised by the worker."""
        with self._lock:
            self.status = STEP_RUN_ERROR
            self.error = str(message)


class StepRunRegistry:
    """Thread-safe table of step runs keyed by task id and token.

    Only one live run per task is allowed: a second ``start`` while the current
    record is live returns ``None`` and starts nothing, so a double click cannot
    create a second provider call. A record that is not live (finished but not
    yet discarded, or orphaned by a stopped thread) is replaced by the new run.

    Every successful claim also reaps the finished/orphan records of other
    tasks, so a long-lived process cannot accumulate them when their session
    token was overwritten or lost. A live record is never reaped: its provider
    call is still in flight and deleting it would allow a second parallel run.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._records: dict = {}
        self._by_task: dict = {}

    def start(self, task_id, chat_id, action, worker) -> StepRunRecord | None:
        """Start a daemon worker for the task, or ``None`` if one is live."""
        with self._lock:
            existing = self._records.get(self._by_task.get(task_id))
            if existing is not None and existing.is_live():
                return None
            # Reap finished records (including the one this start replaces)
            # before the new claim; a live record is always kept.
            self._reap_finished_locked()
            record = StepRunRecord(
                token=uuid.uuid4().hex,
                task_id=task_id,
                chat_id=chat_id,
                action=action,
            )
            # Create, assign, register and start the thread under the registry
            # lock: another ``start`` can only observe a record that is already
            # live, so the claim window can never look replaceable. There is no
            # deadlock because ``_run`` never takes the registry lock, only the
            # record lock.
            thread = threading.Thread(
                target=self._run,
                args=(record, worker),
                name=f"step-run-{task_id}",
                daemon=True,
            )
            record.thread = thread
            self._records[record.token] = record
            self._by_task[task_id] = record.token
            thread.start()
        return record

    def _reap_finished_locked(self) -> None:
        """Drop every non-live record; caller must hold ``self._lock``.

        The check runs under the registry lock, so a record observed as live
        here cannot be removed: its provider call is still running.
        """
        for token, record in list(self._records.items()):
            if not record.is_live():
                del self._records[token]
        for task_id, token in list(self._by_task.items()):
            if token not in self._records:
                del self._by_task[task_id]

    def record_count(self) -> int:
        """Return the number of records the registry currently keeps."""
        with self._lock:
            return len(self._records)

    def get(self, token) -> StepRunRecord | None:
        """Return the record of one token, or ``None``."""
        with self._lock:
            return self._records.get(token)

    def active(self, task_id) -> StepRunRecord | None:
        """Return the current record of a task, live or not, or ``None``."""
        with self._lock:
            return self._records.get(self._by_task.get(task_id))

    def discard(self, token) -> None:
        """Forget one record and its task index."""
        with self._lock:
            self._records.pop(token, None)
            for task_id, current in list(self._by_task.items()):
                if current == token:
                    del self._by_task[task_id]

    def clear(self) -> None:
        """Drop every record; used by tests to isolate the process registry."""
        with self._lock:
            self._records.clear()
            self._by_task.clear()

    @staticmethod
    def _run(record, worker) -> None:
        """Run the worker and settle the record for every outcome."""
        try:
            result = worker(record.set_text)
        except Exception as exc:  # noqa: BLE001 - the worker must never leak
            record._fail(f"Error: {exc}")
        else:
            record._finish(result)
        finally:
            # A worker that returned without finishing (a stopped thread) must
            # not keep the task busy: the UI reports the orphan once.
            with record._lock:
                if record.status == STEP_RUN_RUNNING:
                    record.status = STEP_RUN_ERROR
                    record.error = _STOPPED_UNEXPECTEDLY


_registry = StepRunRegistry()


def get_registry() -> StepRunRegistry:
    """Return the process-wide registry (one instance per server process)."""
    return _registry


def execute_step_action(orchestrator, task_id, action, on_chunk):
    """Dispatch one card action to the matching orchestrator use case."""
    if action == ACTION_RETRY:
        return orchestrator.retry(task_id, on_chunk=on_chunk)
    return orchestrator.run_step(task_id, on_chunk=on_chunk)


def start_step_run(orchestrator, task_id, chat_id, action) -> StepRunRecord | None:
    """Start a background step run, or ``None`` if the task is already busy."""

    def worker(on_chunk):
        return execute_step_action(orchestrator, task_id, action, on_chunk)

    return _registry.start(task_id, chat_id, action, worker)


def step_run_record(token) -> StepRunRecord | None:
    """Return the record a session token points at, or ``None``."""
    return _registry.get(token)


def step_run_busy(task_id) -> bool:
    """Whether a live run currently owns the task (drives the disabled button)."""
    record = _registry.active(task_id)
    return bool(record is not None and record.is_live())


def discard_step_run(token) -> None:
    """Forget a finished run the UI has already read."""
    _registry.discard(token)
