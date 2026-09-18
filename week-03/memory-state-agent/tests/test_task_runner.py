"""Tests for the background step-run registry (``task_runner``).

The unit tests drive the registry with plain worker callables, so they check the
record lifecycle without Streamlit or a provider. The integration tests use a
temporary SQLite database, a real ``TaskOrchestrator`` and the ``FakeClient`` of
``tests.test_agent``: they prove that a run started by ``start_step_run`` stores
its artifact and journal event exactly like a synchronous call, and that a
provider failure settles the record as an error. Every test joins its worker
thread and clears the process registry.
"""

import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

import task_runner
from agent import AgentConfig
from storage import ChatStore
from task_orchestrator import TaskOrchestrator
from task_runner import (
    STEP_RUN_DONE,
    STEP_RUN_ERROR,
    StepRunRecord,
    execute_step_action,
    get_registry,
    start_step_run,
    step_run_busy,
    step_run_record,
)
from task_storage import TaskRepository
from tasks import (
    ACTION_RETRY,
    ACTION_RUN_STEP,
    ARTIFACT_EXECUTION_RESULT,
    EVENT_API_ERROR,
    EVENT_STEP_COMPLETED,
)
from tests.test_agent import FakeClient
from tests.test_task_orchestrator import plan_response, raises, stream_step


def ok_result(text):
    """A duck-typed successful ``TaskActionResult`` for the registry unit tests."""
    return SimpleNamespace(
        ok=True,
        stage_result=SimpleNamespace(text=text),
        error_message=None,
    )


def error_result(message):
    """A duck-typed failed ``TaskActionResult`` for the registry unit tests."""
    return SimpleNamespace(ok=False, stage_result=None, error_message=message)


def wait_until(predicate, timeout=5.0):
    """Poll ``predicate`` until it is true or the timeout passes."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


_REAL_THREAD = threading.Thread


class _GatedThread:
    """A ``threading.Thread`` stand-in that holds the registry claim open.

    ``StepRunRegistry.start`` constructs it while it owns the claim, so the
    first instance blocks in the constructor until the test releases the gate.
    A second instance can only be constructed when a second ``start`` wrongly
    entered the claim window, so ``second_entered`` is the signal that the
    double-start bug happened. The real worker still runs in a real thread.
    """

    lock = threading.Lock()
    created = []

    @classmethod
    def reset(cls):
        cls.first_entered = threading.Event()
        cls.second_entered = threading.Event()
        cls.gate = threading.Event()
        cls.created = []

    @classmethod
    def release(cls):
        cls.gate.set()

    def __init__(self, target=None, args=(), name=None, daemon=None, **kwargs):
        self._target = target
        self._args = args
        self.name = name
        self.daemon = daemon
        self._thread = None
        with _GatedThread.lock:
            index = len(_GatedThread.created)
            _GatedThread.created.append(self)
        if index == 0:
            _GatedThread.first_entered.set()
            _GatedThread.gate.wait(5)
        else:
            _GatedThread.second_entered.set()

    def start(self):
        self._thread = _REAL_THREAD(
            target=self._target, args=self._args, daemon=True
        )
        self._thread.start()

    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)


class _OrchestratorSpy:
    """Records the calls ``execute_step_action`` makes."""

    def __init__(self):
        self.calls = []

    def run_step(self, task_id, on_chunk=None):
        self.calls.append(("run_step", task_id, on_chunk))
        return "run"

    def retry(self, task_id, on_chunk=None):
        self.calls.append(("retry", task_id, on_chunk))
        return "retry"


class StepRunRegistryTest(unittest.TestCase):
    """The record and registry lifecycle, without Streamlit or a provider."""

    def tearDown(self):
        get_registry().clear()

    def test_start_runs_the_worker_and_finishes_done(self):
        started = threading.Event()
        release = threading.Event()

        def worker(on_chunk):
            on_chunk("streamed text")
            started.set()
            release.wait(5)
            return ok_result("final text")

        record = get_registry().start(1, 10, ACTION_RUN_STEP, worker)
        self.assertIsNotNone(record)
        try:
            self.assertTrue(started.wait(5))
            self.assertEqual(record.snapshot()["text"], "streamed text")
            self.assertTrue(record.is_live())
        finally:
            release.set()
            record.thread.join(5)

        self.assertFalse(record.is_live())
        self.assertEqual(record.status, STEP_RUN_DONE)
        self.assertEqual(record.text, "final text")
        self.assertEqual(record.snapshot()["status"], STEP_RUN_DONE)

    def test_second_start_is_refused_while_the_first_is_live(self):
        release = threading.Event()
        calls = []

        def worker(on_chunk):
            calls.append(1)
            release.wait(5)
            return ok_result("done")

        first = get_registry().start(2, 20, ACTION_RUN_STEP, worker)
        try:
            self.assertTrue(wait_until(lambda: len(calls) == 1))
            second = get_registry().start(2, 20, ACTION_RUN_STEP, worker)
            self.assertIsNone(second)
            self.assertEqual(len(calls), 1)
            self.assertIs(get_registry().active(2), first)
        finally:
            release.set()
            first.thread.join(5)

        self.assertFalse(step_run_busy(2))

    def test_concurrent_start_cannot_replace_a_record_inside_the_claim(self):
        registry = get_registry()
        calls = []
        release = threading.Event()

        def worker(on_chunk):
            calls.append(1)
            release.wait(5)
            return ok_result("done")

        _GatedThread.reset()
        original_thread = task_runner.threading.Thread
        task_runner.threading.Thread = _GatedThread
        results = {}

        def first():
            results["first"] = registry.start(42, 420, ACTION_RUN_STEP, worker)

        def second():
            results["second"] = registry.start(42, 420, ACTION_RUN_STEP, worker)

        caller_a = _REAL_THREAD(target=first)
        caller_b = _REAL_THREAD(target=second)
        try:
            caller_a.start()
            # The first start is held inside the claim (thread constructed but
            # not started). The second start races it from another thread.
            self.assertTrue(_GatedThread.first_entered.wait(5))
            caller_b.start()
            # With the fixed claim the second start blocks on the registry lock
            # and never constructs a thread, so this bounded wait just keeps the
            # correct path fast. A second constructor would set the event.
            _GatedThread.second_entered.wait(1.0)
            _GatedThread.release()
            caller_a.join(5)
            caller_b.join(5)
        finally:
            _GatedThread.release()
            release.set()
            task_runner.threading.Thread = original_thread
            for thread in _GatedThread.created:
                if thread._thread is not None:
                    thread._thread.join(5)

        self.assertIsNotNone(results.get("first"))
        self.assertIsNone(results.get("second"))
        self.assertEqual(len(calls), 1)

    def test_start_reaps_a_finished_record_of_another_task(self):
        registry = get_registry()
        # A: a run on one task finishes but its session token is never read.
        first = registry.start(
            100, 1000, ACTION_RUN_STEP, lambda on_chunk: ok_result("a")
        )
        first.thread.join(5)
        self.assertEqual(first.status, STEP_RUN_DONE)
        self.assertEqual(registry.record_count(), 1)

        # B: a start for another task must not leave A's finished record behind.
        release = threading.Event()

        def worker(on_chunk):
            release.wait(5)
            return ok_result("b")

        second = registry.start(101, 1010, ACTION_RUN_STEP, worker)
        try:
            self.assertIsNone(registry.get(first.token))
            self.assertIs(registry.active(101), second)
            self.assertTrue(second.is_live())
            self.assertEqual(registry.record_count(), 1)
        finally:
            release.set()
            second.thread.join(5)

    def test_start_keeps_live_records_of_other_tasks(self):
        registry = get_registry()
        release = threading.Event()

        def worker_a(on_chunk):
            release.wait(5)
            return ok_result("a")

        first = registry.start(102, 1020, ACTION_RUN_STEP, worker_a)
        try:
            self.assertTrue(first.is_live())

            second = registry.start(
                103, 1030, ACTION_RUN_STEP, lambda on_chunk: ok_result("b")
            )
            try:
                # A new start reaps only finished records: the live A survives.
                self.assertIs(registry.active(102), first)
                self.assertTrue(first.is_live())
                self.assertIsNotNone(registry.get(first.token))
                self.assertGreaterEqual(registry.record_count(), 2)

                # A second start for the live task is still refused and never
                # reaches its worker.
                calls = []
                refused = registry.start(
                    102,
                    1020,
                    ACTION_RUN_STEP,
                    lambda on_chunk: calls.append(1),
                )
                self.assertIsNone(refused)
                self.assertEqual(calls, [])
            finally:
                second.thread.join(5)
        finally:
            release.set()
            first.thread.join(5)

        self.assertFalse(step_run_busy(102))

    def test_start_is_allowed_after_the_previous_run_finished(self):
        def worker(text):
            def inner(on_chunk):
                on_chunk(text)
                return ok_result(text)

            return inner

        first = get_registry().start(3, 30, ACTION_RUN_STEP, worker("first"))
        first.thread.join(5)
        self.assertEqual(first.status, STEP_RUN_DONE)

        second = get_registry().start(3, 30, ACTION_RUN_STEP, worker("second"))
        self.assertIsNotNone(second)
        second.thread.join(5)
        self.assertEqual(second.status, STEP_RUN_DONE)
        self.assertEqual(second.text, "second")
        self.assertIs(get_registry().active(3), second)

    def test_error_result_sets_the_error_status(self):
        record = get_registry().start(
            4, 40, ACTION_RUN_STEP, lambda on_chunk: error_result("boom")
        )
        record.thread.join(5)

        self.assertEqual(record.status, STEP_RUN_ERROR)
        self.assertEqual(record.error, "boom")

    def test_worker_exception_is_recorded_as_an_error(self):
        def worker(on_chunk):
            raise RuntimeError("kaboom")

        record = get_registry().start(5, 50, ACTION_RUN_STEP, worker)
        record.thread.join(5)

        self.assertEqual(record.status, STEP_RUN_ERROR)
        self.assertIn("kaboom", record.error)

    def test_snapshot_is_a_copy_of_the_record(self):
        record = StepRunRecord(
            token="token", task_id=1, chat_id=2, action=ACTION_RUN_STEP
        )
        record.set_text("original")

        snapshot = record.snapshot()
        snapshot["text"] = "changed"

        self.assertEqual(record.snapshot()["text"], "original")

    def test_is_live_requires_a_running_status_and_a_live_thread(self):
        orphan = StepRunRecord(
            token="token", task_id=1, chat_id=2, action=ACTION_RUN_STEP
        )
        self.assertFalse(orphan.is_live())

        release = threading.Event()

        def worker(on_chunk):
            release.wait(5)
            return ok_result("done")

        record = get_registry().start(6, 60, ACTION_RUN_STEP, worker)
        try:
            self.assertTrue(record.is_live())
        finally:
            release.set()
            record.thread.join(5)
        self.assertFalse(record.is_live())

    def test_step_run_busy_is_false_after_finish_and_for_an_orphan(self):
        release = threading.Event()

        def worker(on_chunk):
            release.wait(5)
            return ok_result("done")

        record = get_registry().start(7, 70, ACTION_RUN_STEP, worker)
        self.assertTrue(step_run_busy(7))
        release.set()
        record.thread.join(5)
        self.assertFalse(step_run_busy(7))

        orphan = StepRunRecord(
            token="orphan", task_id=8, chat_id=80, action=ACTION_RUN_STEP
        )
        get_registry()._records["orphan"] = orphan
        get_registry()._by_task[8] = "orphan"
        self.assertFalse(step_run_busy(8))

    def test_execute_step_action_dispatches_and_forwards_on_chunk(self):
        spy = _OrchestratorSpy()
        seen = []

        retried = execute_step_action(spy, 1, ACTION_RETRY, seen.append)
        ran = execute_step_action(spy, 1, ACTION_RUN_STEP, seen.append)

        self.assertEqual(retried, "retry")
        self.assertEqual(ran, "run")
        self.assertEqual(
            [(name, task_id) for name, task_id, _ in spy.calls],
            [("retry", 1), ("run_step", 1)],
        )
        self.assertTrue(all(on_chunk is not None for _, _, on_chunk in spy.calls))

    def test_start_step_run_registers_the_wrapper_record(self):
        class Orchestrator:
            def __init__(self):
                self.calls = []

            def run_step(self, task_id, on_chunk=None):
                self.calls.append(task_id)
                on_chunk("wrapped text")
                return ok_result("wrapped final")

        orchestrator = Orchestrator()
        record = start_step_run(orchestrator, 9, 90, ACTION_RUN_STEP)
        record.thread.join(5)

        self.assertEqual(orchestrator.calls, [9])
        self.assertEqual(record.status, STEP_RUN_DONE)
        self.assertEqual(record.text, "wrapped final")
        self.assertIs(step_run_record(record.token), record)


class StepRunIntegrationTest(unittest.TestCase):
    """A real orchestrator, a temporary database and a FakeClient."""

    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ChatStore(os.path.join(self._tmp.name, "runner.db"))
        self.chat_id = self.store.create_chat(AgentConfig())
        self.repo = TaskRepository(self.store.db_path)

    def tearDown(self):
        get_registry().clear()

    def make_orchestrator(self, script):
        return TaskOrchestrator(
            self.store, repository=self.repo, client=FakeClient(script=script)
        )

    def prepared_task(self, orchestrator):
        task = orchestrator.create_task(
            self.chat_id, "Report task", "Build the report"
        ).task
        orchestrator.run_planning(task.id)
        orchestrator.accept_plan(task.id)
        return task

    def test_background_step_run_stores_the_result(self):
        orchestrator = self.make_orchestrator(
            [plan_response(), stream_step("Step one done")]
        )
        task = self.prepared_task(orchestrator)

        record = start_step_run(orchestrator, task.id, self.chat_id, ACTION_RUN_STEP)
        self.assertIsNotNone(record)
        record.thread.join(10)

        self.assertFalse(record.is_live())
        self.assertEqual(record.status, STEP_RUN_DONE)
        self.assertEqual(record.text, "Step one done")
        results = self.repo.list_artifacts(task.id, kind=ARTIFACT_EXECUTION_RESULT)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].content["text"], "Step one done")
        self.assertEqual(self.repo.latest_event_type(task.id), EVENT_STEP_COMPLETED)
        self.assertFalse(step_run_busy(task.id))

    def test_background_step_run_records_a_provider_error(self):
        orchestrator = self.make_orchestrator(
            [plan_response(), raises(RuntimeError("provider down"))]
        )
        task = self.prepared_task(orchestrator)

        record = start_step_run(orchestrator, task.id, self.chat_id, ACTION_RUN_STEP)
        record.thread.join(10)

        self.assertEqual(record.status, STEP_RUN_ERROR)
        self.assertIn("provider down", record.error)
        self.assertEqual(self.repo.latest_event_type(task.id), EVENT_API_ERROR)
        self.assertEqual(
            self.repo.list_artifacts(task.id, kind=ARTIFACT_EXECUTION_RESULT), []
        )


if __name__ == "__main__":
    unittest.main()
