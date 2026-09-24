"""Unit tests of the in-process MCP server (no port, no network).

The server is exercised through the SDK v2 ``Client`` pointed at the in-process
``MCPServer`` instance, so the real tool registration, schema generation and
error conversion run without opening a socket.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcp import Client

from mcp_server import tasks as task_module
from mcp_server import web_search
from mcp_server.server import MCPServer
from storage.chats import ChatRepository
from storage.db import Database


def _payload(result) -> dict:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    text = ""
    for item in getattr(result, "content", None) or []:
        piece = getattr(item, "text", None)
        if piece:
            text += piece
    return json.loads(text)


def _content_text(result) -> str:
    text = ""
    for item in getattr(result, "content", None) or []:
        piece = getattr(item, "text", None)
        if piece:
            text += piece
    return text


def _is_error(result) -> bool:
    return bool(getattr(result, "is_error", False))


class InProcessMcpTest(unittest.IsolatedAsyncioTestCase):
    """The MCP server behaves as the agent expects."""

    def _server(self) -> MCPServer:
        return MCPServer(host="127.0.0.1", port=0)

    async def test_tools_are_listed_with_schemas(self):
        server = self._server()
        async with Client(server.app) as client:
            result = await client.list_tools()
        names = [tool.name for tool in result.tools]
        self.assertIn("calculate", names)
        self.assertIn("get_server_info", names)
        calculate = next(tool for tool in result.tools if tool.name == "calculate")
        self.assertEqual(calculate.input_schema.get("type"), "object")
        self.assertIn("operation", calculate.input_schema.get("properties", {}))
        self.assertTrue(calculate.description)

    async def test_all_seven_tools_are_listed(self):
        server = self._server()
        async with Client(server.app) as client:
            result = await client.list_tools()
        names = sorted(tool.name for tool in result.tools)
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
        schedule = next(
            tool for tool in result.tools if tool.name == "schedule_search_task"
        )
        properties = schedule.input_schema.get("properties", {})
        self.assertIn("chat_id", properties)
        self.assertEqual(properties["max_results"].get("type"), "integer")

    async def test_calculate_returns_a_structured_result(self):
        server = self._server()
        async with Client(server.app) as client:
            result = await client.call_tool(
                "calculate", {"operation": "multiply", "a": 23, "b": 17}
            )
        self.assertFalse(_is_error(result))
        payload = _payload(result)
        self.assertEqual(payload["result"], 391)

    async def test_divide_by_zero_is_a_controlled_tool_error(self):
        server = self._server()
        async with Client(server.app) as client:
            result = await client.call_tool(
                "calculate", {"operation": "divide", "a": 1, "b": 0}
            )
        self.assertTrue(_is_error(result))
        self.assertIn("Division by zero", _content_text(result))

    async def test_invalid_arguments_do_not_crash_the_server(self):
        server = self._server()
        async with Client(server.app) as client:
            try:
                result = await client.call_tool(
                    "calculate", {"operation": "power", "a": 2, "b": 3}
                )
            except Exception as exc:  # noqa: BLE001 - an error result is acceptable
                self.assertTrue(str(exc))
            else:
                self.assertTrue(_is_error(result))
            # The server must still answer afterwards.
            follow_up = await client.call_tool("get_server_info", {})
        self.assertIn("day-16-mcp-server", json.dumps(_payload(follow_up)))

    async def test_unknown_tool_is_rejected(self):
        server = self._server()
        async with Client(server.app) as client:
            try:
                result = await client.call_tool("does_not_exist", {})
            except Exception:  # noqa: BLE001 - an exception is a valid rejection
                result = None
            if result is not None:
                self.assertTrue(_is_error(result))

    async def test_server_info_has_no_secrets(self):
        server = self._server()
        async with Client(server.app) as client:
            result = await client.call_tool("get_server_info", {})
        payload = _payload(result)
        self.assertEqual(payload["name"], "day-16-mcp-server")
        self.assertEqual(payload["status"], "ok")
        for forbidden in ("env", "path", "api_key", "token"):
            self.assertNotIn(forbidden, payload)


class InProcessSearchWebTest(unittest.IsolatedAsyncioTestCase):
    """``search_web`` is advertised and served through the real SDK surface."""

    def _server(self) -> MCPServer:
        return MCPServer(host="127.0.0.1", port=0)

    async def test_tool_is_listed_with_a_query_required_schema(self):
        server = self._server()
        async with Client(server.app) as client:
            result = await client.list_tools()
        names = [tool.name for tool in result.tools]
        self.assertIn("search_web", names)
        tool = next(tool for tool in result.tools if tool.name == "search_web")
        schema = tool.input_schema
        self.assertEqual(schema.get("type"), "object")
        properties = schema.get("properties", {})
        self.assertIn("query", properties)
        self.assertEqual(properties["query"].get("type"), "string")
        self.assertEqual(properties["max_results"].get("type"), "integer")
        self.assertEqual(set(schema.get("required", [])), {"query"})

    async def test_structured_success_has_no_secrets(self):
        payload = {
            "query": "cats",
            "count": 1,
            "results": [
                {
                    "title": "Docs",
                    "url": "https://docs.example.test/1",
                    "description": "snippet",
                }
            ],
            "more_results_available": False,
            "note": web_search.SEARCH_NOTE,
        }
        service = mock.Mock()
        service.search.return_value = payload
        server = self._server()
        with mock.patch.object(web_search, "default_service", return_value=service):
            async with Client(server.app) as client:
                result = await client.call_tool("search_web", {"query": "cats"})
        self.assertFalse(_is_error(result))
        self.assertEqual(_payload(result)["results"][0]["url"], payload["results"][0]["url"])
        raw = _content_text(result)
        for marker in ("api_key", "token", "authorization", "subscription"):
            self.assertNotIn(marker, raw.lower())

    async def test_search_error_is_a_controlled_tool_error(self):
        service = mock.Mock()
        service.search.side_effect = web_search.SearchError(
            "not_configured", web_search.NOT_CONFIGURED_MESSAGE
        )
        server = self._server()
        with mock.patch.object(web_search, "default_service", return_value=service):
            async with Client(server.app) as client:
                result = await client.call_tool("search_web", {"query": "cats"})
        self.assertTrue(_is_error(result))
        self.assertIn("not configured", _content_text(result))


class _StubSearch:
    """A configured search service returning one link."""

    configured = True

    def search(self, query, max_results=0):
        return {
            "query": query,
            "count": 1,
            "results": [
                {
                    "title": "Docs",
                    "url": "https://docs.example.test/1",
                    "description": "snippet",
                }
            ],
            "more_results_available": False,
            "note": web_search.SEARCH_NOTE,
        }


class InProcessScheduledTaskTest(unittest.IsolatedAsyncioTestCase):
    """The four scheduled tools work through the real SDK surface."""

    def _server(self) -> MCPServer:
        return MCPServer(host="127.0.0.1", port=0)

    def _service(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        database = Database(Path(tmp.name) / "tasks.sqlite3")
        ChatRepository(database).create_chat("Chat", chat_id="chat-1")
        return task_module.TaskService(database, search=_StubSearch())

    async def test_schedule_and_list_return_structured_results(self):
        service = self._service()
        server = self._server()
        with mock.patch.object(
            task_module, "default_task_service", return_value=service
        ):
            async with Client(server.app) as client:
                created = await client.call_tool(
                    "schedule_search_task",
                    {"query": "python news", "interval_seconds": 86400, "chat_id": "chat-1"},
                )
                listed = await client.call_tool(
                    "list_search_tasks", {"chat_id": "chat-1"}
                )
        self.assertFalse(_is_error(created))
        payload = _payload(created)
        self.assertTrue(payload["created"])
        self.assertEqual(payload["query"], "python news")
        self.assertEqual(payload["status"], "active")
        listed_payload = _payload(listed)
        self.assertEqual(listed_payload["count"], 1)
        self.assertEqual(listed_payload["tasks"][0]["query"], "python news")
        self.assertEqual(listed_payload["tasks"][0]["max_results"], 0)

    async def test_schedule_carries_the_result_limit(self):
        service = self._service()
        server = self._server()
        with mock.patch.object(
            task_module, "default_task_service", return_value=service
        ):
            async with Client(server.app) as client:
                created = await client.call_tool(
                    "schedule_search_task",
                    {
                        "query": "python news",
                        "interval_seconds": 86400,
                        "chat_id": "chat-1",
                        "max_results": 3,
                    },
                )
        self.assertFalse(_is_error(created))
        self.assertEqual(_payload(created)["max_results"], 3)

    async def test_latest_run_is_pending_before_the_first_run(self):
        service = self._service()
        server = self._server()
        with mock.patch.object(
            task_module, "default_task_service", return_value=service
        ):
            async with Client(server.app) as client:
                result = await client.call_tool(
                    "get_latest_search_run", {"chat_id": "chat-1"}
                )
        self.assertFalse(_is_error(result))
        self.assertEqual(_payload(result)["status"], "pending")

    async def test_empty_chat_context_is_a_controlled_tool_error(self):
        service = self._service()
        server = self._server()
        with mock.patch.object(
            task_module, "default_task_service", return_value=service
        ):
            async with Client(server.app) as client:
                result = await client.call_tool(
                    "schedule_search_task",
                    {"query": "news", "interval_seconds": 60, "chat_id": ""},
                )
        self.assertTrue(_is_error(result))
        self.assertIn("active chat context", _content_text(result))


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
