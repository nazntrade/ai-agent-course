"""Unit tests of the notifier MCP server assembly (no port, no network).

The server is exercised through the SDK v2 in-process ``Client``, so tool
registration, schema generation and structured results run for real without
opening a socket. A source guard asserts that server B never imports ``agent.*``.
"""

from __future__ import annotations

import inspect
import json
import socket
import unittest
from unittest import mock

from mcp import Client
from mcp.server import MCPServer as SdkMCPServer

from notifier_server import SERVER_NAME, SERVER_VERSION
from notifier_server import tools
from notifier_server import watches as watch_module
from notifier_server.server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    NotifierServer,
    port_is_available,
    resolve_host_port,
)

EXPECTED_TOOLS = (
    "create_notification_watch",
    "evaluate_run",
    "get_delivery_status",
    "list_notification_watches",
    "send_notification",
    "stop_notification_watch",
)


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


def _is_error(result) -> bool:
    return bool(getattr(result, "is_error", False))


class _StubWatchService:
    """A scripted watch service returning structured payloads."""

    def create_watch(self, *args, **kwargs):
        return {
            "watch_id": "w1",
            "status": "active",
            "query": "xbox news",
            "keywords": ["xbox"],
            "exclude": [],
            "interval_seconds": 60,
            "summary_interval_seconds": 3600,
            "source_task_id": "",
            "created_at": "2026-01-01T00:00:00Z",
            "next_check_at": "2026-01-01T00:00:00Z",
            "note": "created",
        }

    def list_watches(self, chat_id=""):
        return {"count": 0, "watches": [], "note": "No notification watches in this chat."}

    def evaluate_run(self, watch_id, run, chat_id=""):
        return {
            "status": "ok",
            "new_items": [],
            "matched_count": 0,
            "known_count": 0,
            "is_baseline": False,
            "should_notify": False,
            "note": "No new matching items.",
        }

    def send_notification(self, watch_id, chat_id="", kind="new_items", items=None):
        return {"delivery_id": "d1", "status": "sent", "kind": kind, "items_count": 0}

    def get_delivery_status(self, watch_id="", chat_id=""):
        return {"count": 0, "deliveries": []}

    def stop_watch(self, watch_id, chat_id=""):
        return {"watch_id": "w1", "status": "stopped", "note": "stopped"}


class ServerAssemblyTest(unittest.TestCase):
    """The server registers exactly the six tools at the default ``/mcp`` path."""

    def test_resolve_host_port_defaults(self):
        self.assertEqual(resolve_host_port(env={}), (DEFAULT_HOST, DEFAULT_PORT))

    def test_resolve_host_port_reads_the_documented_names(self):
        env = {"MCP_NOTIFIER_HOST": "0.0.0.0", "MCP_NOTIFIER_PORT": "9001"}
        self.assertEqual(resolve_host_port(env=env), ("0.0.0.0", 9001))

    def test_legacy_notifier_names_still_work(self):
        env = {"NOTIFIER_HOST": "0.0.0.0", "NOTIFIER_PORT": "9002"}
        self.assertEqual(resolve_host_port(env=env), ("0.0.0.0", 9002))

    def test_documented_names_win_over_the_legacy_fallback(self):
        env = {
            "MCP_NOTIFIER_HOST": "127.0.0.1",
            "MCP_NOTIFIER_PORT": "9003",
            "NOTIFIER_HOST": "0.0.0.0",
            "NOTIFIER_PORT": "9004",
        }
        self.assertEqual(resolve_host_port(env=env), ("127.0.0.1", 9003))

    def test_invalid_port_falls_back(self):
        self.assertEqual(DEFAULT_PORT, 8766)
        for env in (
            {"MCP_NOTIFIER_PORT": "nope"},
            {"MCP_NOTIFIER_PORT": "70000"},
            {"NOTIFIER_PORT": "nope"},
            {"NOTIFIER_PORT": "70000"},
        ):
            with self.subTest(env=env):
                self.assertEqual(resolve_host_port(env=env)[1], DEFAULT_PORT)

    def test_port_is_available_reports_a_bound_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            self.assertFalse(port_is_available("127.0.0.1", port))
        self.assertTrue(port_is_available("127.0.0.1", port))

    def test_run_uses_streamable_http_without_overriding_the_default_path(self):
        server = NotifierServer(host="127.0.0.1", port=8766)
        with mock.patch.object(server.app, "run") as run:
            server.run()
        run.assert_called_once_with(
            transport="streamable-http", host="127.0.0.1", port=8766
        )

    def test_sdk_default_streamable_path_is_mcp(self):
        parameter = inspect.signature(
            SdkMCPServer.run_streamable_http_async
        ).parameters["streamable_http_path"]
        self.assertEqual(parameter.default, "/mcp")

    def test_server_metadata(self):
        server = NotifierServer()
        self.assertEqual(server.name, SERVER_NAME)
        self.assertEqual(server.version, SERVER_VERSION)


class InProcessServerTest(unittest.IsolatedAsyncioTestCase):
    """The real SDK surface lists six tools and serves structured results."""

    def _server(self) -> NotifierServer:
        return NotifierServer(host="127.0.0.1", port=0)

    async def test_six_tools_are_listed_with_schemas(self):
        server = self._server()
        async with Client(server.app) as client:
            result = await client.list_tools()
        names = sorted(tool.name for tool in result.tools)
        self.assertEqual(names, sorted(EXPECTED_TOOLS))
        for tool in result.tools:
            with self.subTest(tool=tool.name):
                self.assertTrue(tool.description)
                self.assertEqual(tool.input_schema.get("type"), "object")

    async def test_create_watch_returns_a_structured_result(self):
        server = self._server()
        with mock.patch.object(
            watch_module, "default_watch_service", return_value=_StubWatchService()
        ):
            async with Client(server.app) as client:
                result = await client.call_tool(
                    "create_notification_watch",
                    {
                        "query": "xbox news",
                        "keywords": ["xbox"],
                        "interval_seconds": 60,
                        "summary_interval_seconds": 3600,
                        "chat_id": "chat-1",
                    },
                )
        self.assertFalse(_is_error(result))
        payload = _payload(result)
        self.assertEqual(payload["watch_id"], "w1")
        self.assertEqual(payload["status"], "active")
        self.assertEqual(payload["keywords"], ["xbox"])

    async def test_send_notification_is_listed_with_expected_schema(self):
        server = self._server()
        async with Client(server.app) as client:
            result = await client.list_tools()
        tool = next(t for t in result.tools if t.name == "send_notification")
        properties = tool.input_schema.get("properties", {})
        self.assertIn("watch_id", properties)
        self.assertIn("kind", properties)
        self.assertIn("items", properties)

    async def test_invalid_arguments_do_not_crash_the_server(self):
        server = self._server()
        with mock.patch.object(
            watch_module, "default_watch_service", return_value=_StubWatchService()
        ):
            async with Client(server.app) as client:
                try:
                    result = await client.call_tool("create_notification_watch", {})
                except Exception:  # noqa: BLE001 - an error result is acceptable
                    result = None
                else:
                    self.assertTrue(_is_error(result))
                follow_up = await client.call_tool("get_delivery_status", {"chat_id": "c"})
        self.assertFalse(_is_error(follow_up))


class SourceGuardTest(unittest.TestCase):
    """Server B is an independent process: no ``agent.*`` import anywhere."""

    def test_no_agent_import(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "notifier_server"
        for path in sorted(root.glob("*.py")):
            with self.subTest(path=path.name):
                source = path.read_text(encoding="utf-8")
                for line in source.splitlines():
                    stripped = line.strip()
                    self.assertFalse(
                        stripped.startswith("import agent")
                        or stripped.startswith("from agent"),
                        f"{path.name}: {line}",
                    )

    def test_tools_module_uses_the_notifier_service(self):
        self.assertIs(tools.watch_service, watch_module)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
