"""Integration tests of the scheduled-search tasks over real processes.

Skipped unless ``RUN_LIVE_MCP=1``. ``harness/live_mcp.py`` starts the real MCP
server (with its scheduler), the real backend and a loopback ``FakeSearchServer``
that share one temporary SQLite file; this module then drives the real MCP
tools, the real HTTP API and the fake search API, and reads the shared database
directly. Nothing is mocked and no paid API is called.
"""

from __future__ import annotations

import asyncio
import os
import time
import unittest
import urllib.error
import urllib.request

import httpx

from agent.mcp_adapter import SdkMcpClient, inspect_tools
from storage.db import Database
from tests.support.fake_search import EMPTY_MARKER, MANY_MARKER, UNAUTHORIZED_MARKER

RUN_LIVE = os.environ.get("RUN_LIVE_MCP") == "1"
MCP_URL = os.environ.get("MCP_TEST_URL", "")
BACKEND_URL = os.environ.get("BACKEND_TEST_URL", "")
DB_PATH = os.environ.get("TASKS_TEST_DB", "")

RUN_TIMEOUT_SECONDS = 30.0
POLL_SECONDS = 0.5


def _stats_request_count() -> int:
    """Read the fake search request counter over loopback HTTP."""
    if not BACKEND_URL:
        return 0
    stats_url = os.environ.get("FAKE_SEARCH_URL", "")
    if not stats_url:
        return 0
    try:
        with urllib.request.urlopen(f"{stats_url}/__stats__", timeout=3.0) as response:
            import json

            payload = json.loads(response.read().decode("utf-8"))
            return int(payload.get("requests") or 0)
    except (urllib.error.URLError, OSError, ValueError):
        return -1


def _tools_by_name(tools) -> dict:
    return {tool.name: tool for tool in tools}


@unittest.skipUnless(RUN_LIVE and MCP_URL and BACKEND_URL, "set RUN_LIVE_MCP=1")
class ScheduledTasksLiveTest(unittest.IsolatedAsyncioTestCase):
    """The real task tools, scheduler and HTTP task panel agree."""

    def setUp(self):
        # Every test starts from a clean chat list: earlier integration modules
        # and previous tests leave chats behind, and the 5-chat limit would
        # otherwise make this module flaky.
        try:
            chats = httpx.get(f"{BACKEND_URL}/api/chats", timeout=20.0).json()
        except Exception:  # noqa: BLE001 - the prerequisite is reported by the tests
            return
        for chat in chats.get("chats", []):
            httpx.delete(
                f"{BACKEND_URL}/api/chats/{chat['id']}?force=true", timeout=20.0
            )

    def _client(self) -> SdkMcpClient:
        return SdkMcpClient(MCP_URL, call_timeout_s=30.0)

    def _create_chat(self, title: str = "Tasks chat") -> str:
        for _ in range(6):
            response = httpx.post(
                f"{BACKEND_URL}/api/chats", json={"title": title}, timeout=20.0
            )
            if response.status_code == 201:
                return response.json()["id"]
            if response.status_code != 409:
                self.fail(response.text)
            # Free a slot by deleting the least recently updated chat; the
            # cascade also removes its tasks and runs.
            chats = httpx.get(
                f"{BACKEND_URL}/api/chats", timeout=20.0
            ).json().get("chats", [])
            if not chats:
                self.fail("the chat limit is reached but there is no chat to delete")
            httpx.delete(
                f"{BACKEND_URL}/api/chats/{chats[-1]['id']}?force=true", timeout=20.0
            )
        self.fail("could not create a chat")

    async def _call(self, tool: str, arguments: dict):
        return await self._client().call_tool(tool, arguments)

    async def _wait_for_status(
        self, chat_id: str, *, task_id: str = "", statuses=("ok", "empty", "error")
    ) -> dict:
        deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
        payload: dict = {"status": "pending"}
        while time.monotonic() < deadline:
            result = await self._call(
                "get_latest_search_run",
                {"chat_id": chat_id, "task_id": task_id},
            )
            if result.ok:
                payload = result.structured or {"status": "pending"}
                if payload.get("status") in statuses:
                    return payload
            await asyncio.sleep(POLL_SECONDS)
        return payload

    async def test_seven_tools_include_the_task_tools(self):
        status, tools = await inspect_tools(MCP_URL, connect_timeout_s=10.0)
        self.assertTrue(status.connected, msg=str(status.error))
        names = sorted(tool.name for tool in tools)
        self.assertEqual(
            names,
            [
                "calculate",
                "get_latest_search_run",
                "get_server_info",
                "list_search_tasks",
                "schedule_search_task",
                "search_web",
                "stop_search_task",
            ],
        )

    async def test_schedule_and_first_run_returns_links(self):
        chat_id = self._create_chat()
        created = await self._call(
            "schedule_search_task",
            {"query": "python documentation", "interval_seconds": 3600, "chat_id": chat_id},
        )
        self.assertTrue(created.ok, msg=created.text)
        task = created.structured
        self.assertTrue(task["created"])
        self.assertEqual(task["status"], "active")

        payload = await self._wait_for_status(chat_id, task_id=task["task_id"])
        self.assertEqual(payload["status"], "ok", payload)
        self.assertGreaterEqual(payload["result_count"], 1)
        urls = [item.get("url") for item in payload.get("results", [])]
        self.assertTrue(any(str(url).startswith("https://docs.example.test/") for url in urls))

    async def test_scheduled_run_applies_the_stored_result_limit(self):
        chat_id = self._create_chat()
        created = await self._call(
            "schedule_search_task",
            {
                "query": f"{MANY_MARKER} limit",
                "interval_seconds": 3600,
                "chat_id": chat_id,
                "max_results": 3,
            },
        )
        self.assertTrue(created.ok, msg=created.text)
        self.assertEqual(created.structured["max_results"], 3)

        payload = await self._wait_for_status(
            chat_id, task_id=created.structured["task_id"]
        )
        self.assertEqual(payload["status"], "ok", payload)
        self.assertEqual(payload["result_count"], 3)
        self.assertEqual(len(payload["results"]), 3)

    async def test_scheduled_run_uses_the_server_default_without_a_limit(self):
        chat_id = self._create_chat()
        created = await self._call(
            "schedule_search_task",
            {
                "query": f"{MANY_MARKER} default",
                "interval_seconds": 3600,
                "chat_id": chat_id,
            },
        )
        self.assertTrue(created.ok, msg=created.text)
        self.assertEqual(created.structured["max_results"], 0)

        payload = await self._wait_for_status(
            chat_id, task_id=created.structured["task_id"]
        )
        self.assertEqual(payload["status"], "ok", payload)
        # The harness configures the server default to five.
        self.assertEqual(payload["result_count"], 5)
        self.assertEqual(len(payload["results"]), 5)

    async def test_empty_and_error_runs_are_saved_honestly(self):
        chat_id = self._create_chat()
        empty = await self._call(
            "schedule_search_task",
            {"query": EMPTY_MARKER, "interval_seconds": 3600, "chat_id": chat_id},
        )
        self.assertTrue(empty.ok, msg=empty.text)
        empty_payload = await self._wait_for_status(
            chat_id, task_id=empty.structured["task_id"], statuses=("empty",)
        )
        self.assertEqual(empty_payload["status"], "empty")
        self.assertEqual(empty_payload["results"], [])

        failed = await self._call(
            "schedule_search_task",
            {"query": UNAUTHORIZED_MARKER, "interval_seconds": 3600, "chat_id": chat_id},
        )
        self.assertTrue(failed.ok, msg=failed.text)
        error_payload = await self._wait_for_status(
            chat_id, task_id=failed.structured["task_id"], statuses=("error",)
        )
        self.assertEqual(error_payload["status"], "error")
        self.assertEqual(error_payload["results"], [])
        self.assertIn("HTTP 401", error_payload.get("error", ""))

    async def test_tasks_are_isolated_per_chat(self):
        chat_a = self._create_chat("A")
        chat_b = self._create_chat("B")
        created = await self._call(
            "schedule_search_task",
            {"query": "shared topic", "interval_seconds": 3600, "chat_id": chat_a},
        )
        self.assertTrue(created.ok, msg=created.text)

        listing_b = await self._call("list_search_tasks", {"chat_id": chat_b})
        self.assertTrue(listing_b.ok, msg=listing_b.text)
        self.assertEqual(listing_b.structured["count"], 0)

        latest_b = await self._call("get_latest_search_run", {"chat_id": chat_b})
        self.assertEqual(latest_b.structured["status"], "pending")

        stop_b = await self._call(
            "stop_search_task",
            {"task_id": created.structured["task_id"], "chat_id": chat_b},
        )
        self.assertFalse(stop_b.ok)

    async def test_server_rejects_an_empty_chat_context(self):
        result = await self._call(
            "schedule_search_task",
            {"query": "news", "interval_seconds": 60, "chat_id": ""},
        )
        self.assertFalse(result.ok)
        self.assertIn("active chat context", result.text)

    async def test_stop_prevents_future_runs(self):
        chat_id = self._create_chat()
        created = await self._call(
            "schedule_search_task",
            {"query": "stop me", "interval_seconds": 3600, "chat_id": chat_id},
        )
        task_id = created.structured["task_id"]
        await self._wait_for_status(chat_id, task_id=task_id, statuses=("ok",))
        stopped = await self._call(
            "stop_search_task", {"task_id": task_id, "chat_id": chat_id}
        )
        self.assertTrue(stopped.ok, msg=stopped.text)
        self.assertEqual(stopped.structured["status"], "stopped")
        again = await self._call(
            "stop_search_task", {"task_id": task_id, "chat_id": chat_id}
        )
        self.assertTrue(again.ok)
        self.assertIn("already stopped", again.structured["note"])

    async def test_deleting_the_chat_stops_new_search_calls(self):
        if not DB_PATH:
            self.skipTest("TASKS_TEST_DB is not configured")
        chat_id = self._create_chat("Delete me")
        created = await self._call(
            "schedule_search_task",
            {"query": "delete topic", "interval_seconds": 3600, "chat_id": chat_id},
        )
        task_id = created.structured["task_id"]
        await self._wait_for_status(chat_id, task_id=task_id, statuses=("ok",))

        before = _stats_request_count()
        self.assertGreaterEqual(before, 1, "the fake search should have been called")

        deleted = httpx.delete(
            f"{BACKEND_URL}/api/chats/{chat_id}?force=true", timeout=20.0
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["stopped_tasks"], 1)

        # The rows are gone from the shared database.
        database = Database(DB_PATH)
        with database.connection() as connection:
            tasks = connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE chat_id = ?", (chat_id,)
            ).fetchone()[0]
            runs = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE task_id = ?", (task_id,)
            ).fetchone()[0]
        self.assertEqual(tasks, 0)
        self.assertEqual(runs, 0)

        # No new search call starts after the deletion.
        await asyncio.sleep(3.0)
        after = _stats_request_count()
        self.assertEqual(after, before, "a new search call started after deletion")


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
