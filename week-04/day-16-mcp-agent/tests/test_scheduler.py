"""Unit tests of the task scheduler (due tasks, CAS, catch-up, resilience)."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from mcp_server import web_search
from mcp_server.scheduler import TaskScheduler
from mcp_server.tasks import TaskService
from storage.chats import ChatRepository
from storage.db import Database
from storage.tasks import TaskRepository


class _StubSearch:
    """A configured search service with a scripted result or error."""

    configured = True

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls: list = []

    def search(self, query, max_results=0):
        self.calls.append((query, max_results))
        if self.error is not None:
            raise self.error
        if self.payload is not None:
            return self.payload
        return {
            "query": query,
            "count": 1,
            "results": [
                {"title": "A", "url": "https://docs.example.test/1", "description": "a"}
            ],
            "more_results_available": False,
            "note": web_search.SEARCH_NOTE,
        }


class _RecordingService:
    """A scheduler collaborator that records claims and executions."""

    def __init__(self, claimed=None, fail=False):
        self.claimed = list(claimed or [])
        self.fail = fail
        self.executed: list = []

    def claim_due_tasks(self, *, now=None, limit=5):
        claimed = self.claimed
        self.claimed = []
        return claimed

    def execute_claimed(self, task):
        if self.fail:
            raise RuntimeError("boom")
        self.executed.append(task)
        return {"task_id": task.get("id")}


class _SchedulerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database = Database(Path(self._tmp.name) / "scheduler.sqlite3")
        self.chats = ChatRepository(self.database)
        self.repository = TaskRepository(self.database)
        self.chats.create_chat("Chat", chat_id="chat-a")
        self.chats.create_chat("Other", chat_id="chat-b")

    def _service(self, search=None):
        return TaskService(
            self.database, search=search or _StubSearch(), clock=lambda: 1000.0
        )


class DueSelectionTest(_SchedulerTestCase):
    """Only active tasks whose slot has arrived are claimed."""

    def test_future_and_stopped_tasks_are_not_claimed(self):
        service = self._service()
        service.create_task("chat-a", "due", 60)
        self.repository.create_task(
            "chat-a", "future", 60, now=1000.0, next_run_at=2000.0
        )
        stopped = service.create_task("chat-a", "stopped", 60)["task_id"]
        service.stop_task("chat-a", stopped)

        claimed = service.claim_due_tasks(now=1000.0)
        self.assertEqual([task["query"] for task in claimed], ["due"])

    def test_second_claim_in_the_same_moment_finds_nothing(self):
        service = self._service()
        service.create_task("chat-a", "due", 60)
        self.assertEqual(len(service.claim_due_tasks(now=1000.0)), 1)
        self.assertEqual(service.claim_due_tasks(now=1000.0), [])


class CatchUpTest(_SchedulerTestCase):
    """A missed schedule runs exactly once, without backfill."""

    def test_one_tick_runs_once_and_reschedules(self):
        service = self._service()
        task = self.repository.create_task(
            "chat-a", "news", 60, now=100.0, next_run_at=100.0
        )
        scheduler = TaskScheduler(service, tick_seconds=60.0, clock=lambda: 1000.0)
        outcomes = scheduler.tick()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["status"], "ok")
        updated = self.repository.get_task(task["id"])
        self.assertEqual(updated["next_run_at"], 1060.0)
        self.assertEqual(updated["run_count"], 1)

        # No time has passed, so there is no second catch-up run.
        self.assertEqual(scheduler.tick(), [])
        self.assertEqual(self.repository.run_count(task["id"]), 1)


class RunPersistenceTest(_SchedulerTestCase):
    """Runs are persisted with the honest outcome."""

    def test_error_is_saved(self):
        error = web_search.SearchError(
            "timeout", "The web search request timed out."
        )
        service = self._service(search=_StubSearch(error=error))
        task = self.repository.create_task(
            "chat-a", "news", 60, now=100.0, next_run_at=100.0
        )
        scheduler = TaskScheduler(service, tick_seconds=60.0, clock=lambda: 1000.0)
        scheduler.tick()
        latest = self.repository.latest_run("chat-a", task["id"])
        self.assertEqual(latest["status"], "error")
        self.assertIn("timed out", latest["error"])

    def test_result_of_a_deleted_task_is_dropped(self):
        service = self._service()
        self.repository.create_task(
            "chat-a", "news", 60, now=100.0, next_run_at=100.0
        )
        claimed = service.claim_due_tasks(now=1000.0)
        self.assertEqual(len(claimed), 1)
        # The chat (and its task) is deleted while the run was in flight.
        self.chats.delete_chat("chat-a")
        self.assertIsNone(service.execute_claimed(claimed[0]))
        self.assertEqual(self.repository.latest_run("chat-a"), None)


class SchedulerThreadTest(unittest.TestCase):
    """The thread runs ticks, survives a failing tick and stops cleanly."""

    def test_tick_swallows_a_task_failure(self):
        scheduler = TaskScheduler(
            _RecordingService(claimed=[{"id": "t1"}], fail=True),
            tick_seconds=60.0,
            clock=lambda: 1.0,
        )
        self.assertEqual(scheduler.tick(), [])

    def test_start_runs_ticks_and_stop_is_clean(self):
        started = threading.Event()

        class _Signalling(_RecordingService):
            def execute_claimed(self, task):
                started.set()
                return super().execute_claimed(task)

        scheduler = TaskScheduler(
            _Signalling(claimed=[{"id": "t1"}]),
            tick_seconds=0.05,
            clock=lambda: 1.0,
        )
        scheduler.start()
        try:
            self.assertTrue(scheduler.running)
            self.assertTrue(started.wait(5.0))
        finally:
            scheduler.stop()
        self.assertFalse(scheduler.running)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
