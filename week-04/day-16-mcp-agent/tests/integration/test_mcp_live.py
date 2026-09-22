"""Integration tests of the real MCP server.

Every test here is skipped unless ``RUN_LIVE_MCP=1`` (``smoke_test.bat`` sets
it). The MCP server, its port and its Streamable HTTP transport are real and
nothing is mocked.
"""

from __future__ import annotations

import os
import socketserver
import threading
import time
import unittest

from agent.mcp_adapter import (
    CATEGORY_TIMEOUT,
    CATEGORY_UNREACHABLE,
    McpError,
    SdkMcpClient,
    inspect_tools,
)

RUN_LIVE = os.environ.get("RUN_LIVE_MCP") == "1"
MCP_URL = os.environ.get("MCP_TEST_URL", "")
UNREACHABLE_URL = os.environ.get("MCP_UNREACHABLE_URL", "")


def _client(**kwargs):
    return SdkMcpClient(MCP_URL, **kwargs)


class _StallingHandler(socketserver.BaseRequestHandler):
    """Accept a connection and never answer, to force a client timeout."""

    def handle(self):
        try:
            self.request.recv(4096)
            time.sleep(3)
        except OSError:
            return


@unittest.skipUnless(RUN_LIVE and MCP_URL, "set RUN_LIVE_MCP=1 to run this test")
class LiveMcpTest(unittest.IsolatedAsyncioTestCase):
    """The real MCP server answers the real protocol."""

    async def test_status_reports_a_connected_server(self):
        status = await _client(connect_timeout_s=10.0).status()
        self.assertTrue(status.connected, msg=str(status.error))
        self.assertEqual(status.server_name, "day-16-mcp-server")
        self.assertTrue(status.protocol_version)
        self.assertGreaterEqual(status.tools_count, 2)

    async def test_tools_list_is_valid(self):
        status, tools = await inspect_tools(MCP_URL, connect_timeout_s=10.0)
        self.assertTrue(status.connected)
        names = [tool.name for tool in tools]
        self.assertIn("calculate", names)
        self.assertIn("get_server_info", names)
        calculate = next(tool for tool in tools if tool.name == "calculate")
        self.assertEqual(calculate.input_schema.get("type"), "object")
        self.assertIn("operation", calculate.input_schema.get("properties", {}))
        self.assertTrue(calculate.description)

    async def test_calculate_valid_arguments(self):
        client = _client(call_timeout_s=10.0)
        result = await client.call_tool(
            "calculate", {"operation": "multiply", "a": 23, "b": 17}
        )
        self.assertTrue(result.ok, msg=result.text)
        self.assertIsNotNone(result.structured)
        self.assertEqual(result.structured["result"], 391)

    async def test_all_four_operations_round_trip(self):
        client = _client(call_timeout_s=10.0)
        cases = {
            "add": (2, 3, 5),
            "subtract": (7, 4, 3),
            "multiply": (6, 7, 42),
            "divide": (9, 3, 3),
        }
        for operation, (a, b, expected) in cases.items():
            with self.subTest(operation=operation):
                result = await client.call_tool(
                    "calculate", {"operation": operation, "a": a, "b": b}
                )
                self.assertTrue(result.ok, msg=result.text)
                self.assertIsNotNone(result.structured)
                self.assertEqual(result.structured["operation"], operation)
                self.assertEqual(result.structured["result"], expected)

    async def test_get_server_info_over_the_wire(self):
        client = _client(call_timeout_s=10.0)
        result = await client.call_tool("get_server_info", {})
        self.assertTrue(result.ok, msg=result.text)
        self.assertIsNotNone(result.structured)
        self.assertEqual(result.structured["name"], "day-16-mcp-server")
        self.assertEqual(result.structured["status"], "ok")
        for forbidden in ("env", "path", "api_key", "token", "secret"):
            self.assertNotIn(forbidden, result.structured)

    async def test_calculate_divide_by_zero_is_controlled(self):
        client = _client(call_timeout_s=10.0)
        result = await client.call_tool(
            "calculate", {"operation": "divide", "a": 1, "b": 0}
        )
        self.assertFalse(result.ok)
        self.assertIn("Division by zero", result.text)

    async def test_invalid_arguments_are_rejected(self):
        client = _client(call_timeout_s=10.0)
        try:
            result = await client.call_tool(
                "calculate", {"operation": "power", "a": 2, "b": 3}
            )
        except McpError as exc:
            self.assertTrue(exc.message)
        else:
            self.assertFalse(result.ok)

    async def test_unknown_tool_is_rejected(self):
        client = _client(call_timeout_s=10.0)
        try:
            result = await client.call_tool("does_not_exist", {})
        except McpError as exc:
            self.assertTrue(exc.message)
        else:
            self.assertFalse(result.ok)

    async def test_reconnect_and_close(self):
        for _ in range(3):
            status = await _client(connect_timeout_s=10.0).status()
            self.assertTrue(status.connected)

    async def test_unreachable_endpoint_is_controlled(self):
        if not UNREACHABLE_URL:
            self.skipTest("MCP_UNREACHABLE_URL is not configured")
        status = await SdkMcpClient(UNREACHABLE_URL, connect_timeout_s=5.0).status()
        self.assertFalse(status.connected)
        self.assertIn(status.error.category, (CATEGORY_UNREACHABLE, CATEGORY_TIMEOUT))
        self.assertNotIn("Authorization", status.error.message)


@unittest.skipUnless(RUN_LIVE, "set RUN_LIVE_MCP=1 to run this test")
class LiveTimeoutTest(unittest.IsolatedAsyncioTestCase):
    """A server that accepts but never answers produces a timeout category."""

    async def test_stalled_server_times_out(self):
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _StallingHandler)
        server.daemon_threads = True
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status = await SdkMcpClient(
                f"http://127.0.0.1:{port}/mcp", connect_timeout_s=1.5
            ).status()
        finally:
            server.shutdown()
            server.server_close()
        self.assertFalse(status.connected)
        self.assertIsNotNone(status.error)
        self.assertIn(status.error.category, (CATEGORY_TIMEOUT, CATEGORY_UNREACHABLE))
        self.assertNotIn("sk-", status.error.message)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
