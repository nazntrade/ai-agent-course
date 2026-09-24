"""Unit tests of the scheduled-search task service (no server, no network)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mcp_server import web_search
from mcp_server.config import SearchConfig
from mcp_server.tasks import (
    MAX_TASKS_PER_CHAT,
    MAX_TASKS_TOTAL,
    NO_TASKS_NOTE,
    TASK_MAX_INTERVAL_SECONDS,
    TaskService,
)
from mcp_server.tools import ToolError
from mcp_server.web_search import HttpResponse, WebSearchService
from storage.chats import ChatRepository
from storage.db import Database
from storage.tasks import TaskRepository

_API_KEY = "unit-test-key"


class _FakeTransport:
    """A scripted transport that records the JSON body of every request."""

    def __init__(self, response):
        self.response = response
        self.calls: list = []

    def post_json(self, url, *, headers, json_body, timeout_s):
        self.calls.append(dict(json_body))
        return self.response


def _results(count: int) -> list:
    return [
        {
            "title": f"Docs {index}",
            "url": f"https://docs.example.test/{index}",
            "content": f"Snippet {index}.",
            "score": 0.5,
        }
        for index in range(1, count + 1)
    ]


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
            "count": 2,
            "results": [
                {"title": "A", "url": "https://docs.example.test/1", "description": "a"},
                {"title": "B", "url": "https://docs.example.test/2", "description": "b"},
            ],
            "more_results_available": False,
            "note": web_search.SEARCH_NOTE,
        }


class _UnconfiguredSearch(_StubSearch):
    configured = False


class TaskServiceTest(unittest.TestCase):
    """Creation rules, limits, isolation and run payloads."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database = Database(Path(self._tmp.name) / "tasks.sqlite3")
        self.chats = ChatRepository(self.database)
        self.repository = TaskRepository(self.database)
        self.chats.create_chat("Chat A", chat_id="chat-a")
        self.chats.create_chat("Chat B", chat_id="chat-b")

    def _service(self, search=None):
        return TaskService(
            self.database, search=search or _StubSearch(), clock=lambda: 1000.0
        )

    def test_create_returns_the_documented_payload(self):
        service = self._service()
        result = service.create_task("chat-a", "  python news  ", 86400)
        self.assertTrue(result["created"])
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["query"], "python news")
        self.assertEqual(result["interval_seconds"], 86400)
        self.assertEqual(result["created_at"], "1970-01-01T00:16:40Z")
        self.assertEqual(result["next_run_at"], "1970-01-01T00:16:40Z")
        self.assertIn("first run", result["note"])

    def test_empty_chat_context_is_rejected(self):
        service = self._service()
        for candidate in ("", "   ", None):
            with self.subTest(candidate=candidate):
                with self.assertRaises(ToolError) as caught:
                    service.create_task(candidate, "news", 60)
                self.assertIn("active chat context", str(caught.exception))

    def test_unknown_chat_is_rejected(self):
        service = self._service()
        with self.assertRaises(ToolError) as caught:
            service.create_task("missing", "news", 60)
        self.assertIn("active chat context", str(caught.exception))

    def test_unconfigured_search_is_rejected_before_creating(self):
        service = self._service(search=_UnconfiguredSearch())
        with self.assertRaises(ToolError) as caught:
            service.create_task("chat-a", "news", 60)
        self.assertIn("not configured", str(caught.exception))
        self.assertEqual(self.repository.list_tasks("chat-a"), [])

    def test_empty_query_is_rejected(self):
        service = self._service()
        with self.assertRaises(ToolError):
            service.create_task("chat-a", "   ", 60)

    def test_overlong_query_is_truncated(self):
        service = self._service()
        result = service.create_task("chat-a", "x" * 900, 60)
        self.assertEqual(len(result["query"]), web_search.MAX_QUERY_LENGTH)

    def test_minimum_interval_is_enforced(self):
        service = self._service()
        with self.assertRaises(ToolError) as caught:
            service.create_task("chat-a", "news", 59)
        self.assertIn("minimum interval", str(caught.exception))

    def test_boolean_interval_is_rejected(self):
        service = self._service()
        with self.assertRaises(ToolError) as caught:
            service.create_task("chat-a", "news", True)
        self.assertIn("integer", str(caught.exception))

    def test_maximum_interval_is_clamped(self):
        service = self._service()
        result = service.create_task("chat-a", "news", TASK_MAX_INTERVAL_SECONDS + 1000)
        self.assertEqual(result["interval_seconds"], TASK_MAX_INTERVAL_SECONDS)

    def test_duplicate_query_does_not_create_a_second_task(self):
        service = self._service()
        first = service.create_task("chat-a", "news", 60)
        second = service.create_task("chat-a", "  news  ", 120)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["task_id"], first["task_id"])
        self.assertEqual(len(self.repository.list_tasks("chat-a")), 1)

    def test_create_stores_max_results_and_returns_it(self):
        service = self._service()
        result = service.create_task("chat-a", "news", 60, 3)
        self.assertEqual(result["max_results"], 3)
        self.assertEqual(self.repository.get_task(result["task_id"])["max_results"], 3)
        listing = service.list_tasks("chat-a")
        self.assertEqual(listing["tasks"][0]["max_results"], 3)

    def test_max_results_defaults_to_zero(self):
        service = self._service()
        result = service.create_task("chat-a", "news", 60)
        self.assertEqual(result["max_results"], 0)

    def test_max_results_above_the_cap_is_clamped(self):
        service = self._service()
        result = service.create_task("chat-a", "news", 60, 11)
        self.assertEqual(result["max_results"], web_search.SEARCH_MAX_RESULTS_CAP)

    def test_non_integer_max_results_is_rejected(self):
        service = self._service()
        for candidate in (True, 1.5, "3", None):
            with self.subTest(candidate=repr(candidate)):
                with self.assertRaises(ToolError) as caught:
                    service.create_task("chat-a", "news", 60, candidate)
                self.assertIn("must be an integer", str(caught.exception))
        self.assertEqual(self.repository.list_tasks("chat-a"), [])

    def test_negative_max_results_is_rejected(self):
        service = self._service()
        with self.assertRaises(ToolError) as caught:
            service.create_task("chat-a", "news", 60, -1)
        self.assertIn("must not be negative", str(caught.exception))

    def test_duplicate_does_not_change_the_limit(self):
        service = self._service()
        first = service.create_task("chat-a", "news", 60, 3)
        second = service.create_task("chat-a", "news", 120, 9)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["max_results"], 3)
        self.assertEqual(self.repository.get_task(first["task_id"])["max_results"], 3)

    def test_max_results_survives_a_new_database_handle(self):
        service = self._service()
        first = service.create_task("chat-a", "news", 60, 4)
        reopened = TaskRepository(Database(self.database.path))
        self.assertEqual(reopened.get_task(first["task_id"])["max_results"], 4)

    def test_per_chat_limit_is_enforced(self):
        service = self._service()
        for index in range(MAX_TASKS_PER_CHAT):
            service.create_task("chat-a", f"news {index}", 60)
        with self.assertRaises(ToolError) as caught:
            service.create_task("chat-a", "one more", 60)
        self.assertIn("already has 3 scheduled tasks", str(caught.exception))

    def test_total_limit_is_enforced(self):
        service = self._service()
        for name in ("chat-2", "chat-3"):
            self.chats.create_chat(name, chat_id=name)
        cycle = ["chat-a", "chat-b", "chat-2", "chat-3"]
        for index in range(MAX_TASKS_TOTAL):
            service.create_task(cycle[index % 4], f"news {index}", 60)
        # ``chat-2`` holds two tasks, under the per-chat limit, so this attempt
        # reaches the total limit instead.
        with self.assertRaises(ToolError) as caught:
            service.create_task("chat-2", "the last one", 60)
        self.assertIn("server already has 10", str(caught.exception))

    def test_list_is_scoped_to_the_chat(self):
        service = self._service()
        service.create_task("chat-a", "alpha", 60)
        service.create_task("chat-b", "beta", 60)
        listing = service.list_tasks("chat-a")
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["tasks"][0]["query"], "alpha")

    def test_list_empty_note(self):
        service = self._service()
        listing = service.list_tasks("chat-a")
        self.assertEqual(listing, {"count": 0, "tasks": [], "note": NO_TASKS_NOTE})

    def test_latest_run_is_pending_without_a_run(self):
        service = self._service()
        service.create_task("chat-a", "news", 60)
        result = service.get_latest_run("chat-a")
        self.assertEqual(result["status"], "pending")

    def test_latest_run_returns_ok_with_links(self):
        service = self._service()
        service.create_task("chat-a", "news", 60)
        claim = service.claim_due_tasks(now=1000.0)[0]
        service.execute_claimed(claim)
        result = service.get_latest_run("chat-a")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["result_count"], 2)
        self.assertEqual(result["results"][0]["url"], "https://docs.example.test/1")

    def test_execute_claimed_uses_the_saved_limit(self):
        stub = _StubSearch()
        service = self._service(search=stub)
        service.create_task("chat-a", "news", 60, 3)
        service.execute_claimed(service.claim_due_tasks(now=1000.0)[0])
        self.assertEqual(stub.calls, [("news", 3)])

    def test_execute_claimed_uses_zero_as_the_server_default(self):
        stub = _StubSearch()
        service = self._service(search=stub)
        service.create_task("chat-a", "news", 60)
        service.execute_claimed(service.claim_due_tasks(now=1000.0)[0])
        self.assertEqual(stub.calls, [("news", 0)])

    def test_real_search_service_honours_the_saved_limit(self):
        transport = _FakeTransport(
            HttpResponse(
                200, json.dumps({"query": "many", "results": _results(10)})
            )
        )
        search = WebSearchService(SearchConfig(api_key=_API_KEY), transport)
        service = TaskService(self.database, search=search, clock=lambda: 1000.0)
        service.create_task("chat-a", "news", 60, 3)
        service.execute_claimed(service.claim_due_tasks(now=1000.0)[0])
        self.assertEqual(transport.calls[0]["max_results"], 3)
        result = service.get_latest_run("chat-a")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["result_count"], 3)
        self.assertEqual(len(result["results"]), 3)

    def test_empty_run_is_saved_as_empty(self):
        payload = {
            "query": "news",
            "count": 0,
            "results": [],
            "more_results_available": False,
            "note": web_search.EMPTY_NOTE,
        }
        service = self._service(search=_StubSearch(payload=payload))
        service.create_task("chat-a", "news", 60)
        service.execute_claimed(service.claim_due_tasks(now=1000.0)[0])
        result = service.get_latest_run("chat-a")
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["results"], [])

    def test_error_run_is_saved_without_inventing_a_summary(self):
        error = web_search.SearchError(
            "api_error", "The web search service failed (HTTP 500)."
        )
        service = self._service(search=_StubSearch(error=error))
        service.create_task("chat-a", "news", 60)
        service.execute_claimed(service.claim_due_tasks(now=1000.0)[0])
        result = service.get_latest_run("chat-a")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["results"], [])
        self.assertIn("HTTP 500", result["error"])

    def test_latest_run_is_isolated_per_chat(self):
        service = self._service()
        task_a = service.create_task("chat-a", "news", 60)["task_id"]
        service.execute_claimed(service.claim_due_tasks(now=1000.0)[0])
        self.assertEqual(service.get_latest_run("chat-b")["status"], "pending")
        with self.assertRaises(ToolError):
            service.get_latest_run("chat-b", task_a)

    def test_stop_is_idempotent_and_scoped(self):
        service = self._service()
        task_id = service.create_task("chat-a", "news", 60)["task_id"]
        first = service.stop_task("chat-a", task_id)
        second = service.stop_task("chat-a", task_id)
        self.assertEqual(first["status"], "stopped")
        self.assertIn("No further runs", first["note"])
        self.assertIn("already stopped", second["note"])
        with self.assertRaises(ToolError):
            service.stop_task("chat-b", task_id)

    def test_stopped_task_is_not_claimed(self):
        service = self._service()
        task_id = service.create_task("chat-a", "news", 60)["task_id"]
        service.stop_task("chat-a", task_id)
        self.assertEqual(service.claim_due_tasks(now=1000.0), [])


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
