"""Unit tests of the MCP adapter helpers and of the discovery CLI formatting."""

from __future__ import annotations

import asyncio
import types
import unittest

from agent.mcp_adapter import (
    CATEGORY_PROTOCOL,
    CATEGORY_TIMEOUT,
    CATEGORY_UNREACHABLE,
    McpError,
    McpTool,
    SdkMcpClient,
    _classify,
    _content_text,
    _to_mcp_tool,
    is_loopback_url,
    tool_result_as_text,
)


class ClassifyTest(unittest.TestCase):
    """Adapter failures carry a coarse category and a safe message."""

    def test_connection_refused_is_unreachable(self):
        category, message = _classify(ConnectionRefusedError("refused"), "http://127.0.0.1:1/mcp")
        self.assertEqual(category, CATEGORY_UNREACHABLE)
        self.assertIn("127.0.0.1:1", message)

    def test_timeout_is_timeout(self):
        category, _message = _classify(asyncio.TimeoutError(), "http://127.0.0.1:1/mcp")
        self.assertEqual(category, CATEGORY_TIMEOUT)

    def test_exception_group_is_flattened(self):
        group = ExceptionGroup("boom", [ConnectionRefusedError("x")])
        category, _message = _classify(group, "http://127.0.0.1:1/mcp")
        self.assertEqual(category, CATEGORY_UNREACHABLE)

    def test_unknown_error_is_protocol(self):
        category, message = _classify(ValueError("weird"), "http://127.0.0.1:1/mcp")
        self.assertEqual(category, CATEGORY_PROTOCOL)
        self.assertNotIn("weird", message)

    def test_message_never_contains_credentials(self):
        _category, message = _classify(
            ConnectionRefusedError("Authorization: Bearer sk-secret"), "http://127.0.0.1:1/mcp"
        )
        self.assertNotIn("sk-secret", message)
        self.assertNotIn("Authorization", message)

    def test_sdk_request_timeout_is_timeout(self):
        from mcp.shared.exceptions import MCPError
        from mcp_types import REQUEST_TIMEOUT

        category, _message = _classify(
            MCPError(REQUEST_TIMEOUT, "timed out"), "http://127.0.0.1:1/mcp"
        )
        self.assertEqual(category, CATEGORY_TIMEOUT)


class ToolMappingTest(unittest.TestCase):
    """SDK tool objects become transport-agnostic value objects."""

    def test_mapping_from_attributes(self):
        raw = types.SimpleNamespace(
            name="calculate",
            title=None,
            description="math",
            input_schema={"type": "object"},
            annotations=None,
        )
        tool = _to_mcp_tool(raw)
        self.assertIsInstance(tool, McpTool)
        self.assertEqual(tool.name, "calculate")
        self.assertEqual(tool.input_schema, {"type": "object"})

    def test_title_falls_back_to_annotations(self):
        raw = types.SimpleNamespace(
            name="x",
            title=None,
            description="",
            input_schema=None,
            annotations=types.SimpleNamespace(title="Nice title"),
        )
        self.assertEqual(_to_mcp_tool(raw).title, "Nice title")

    def test_missing_name_is_dropped(self):
        self.assertIsNone(_to_mcp_tool(types.SimpleNamespace(name=None)))

    def test_content_text_joins_parts(self):
        content = [
            types.SimpleNamespace(text="a"),
            types.SimpleNamespace(text="b"),
        ]
        self.assertEqual(_content_text(content), "a\nb")

    def test_content_text_of_none_is_empty(self):
        self.assertEqual(_content_text(None), "")

    def test_tool_result_as_text_prefers_text(self):
        from agent.mcp_adapter import McpCallResult

        self.assertEqual(
            tool_result_as_text(McpCallResult(ok=True, text="hello", structured={"a": 1})),
            "hello",
        )
        self.assertEqual(
            tool_result_as_text(McpCallResult(ok=True, text="", structured={"a": 1})),
            '{"a": 1}',
        )


class LoopbackTest(unittest.TestCase):
    """Only loopback endpoints are accepted."""

    def test_loopback_hosts(self):
        self.assertTrue(is_loopback_url("http://127.0.0.1:8765/mcp"))
        self.assertTrue(is_loopback_url("http://localhost:8765/mcp"))

    def test_remote_hosts_are_rejected(self):
        self.assertFalse(is_loopback_url("http://example.com/mcp"))


class PaginationTest(unittest.IsolatedAsyncioTestCase):
    """``tools/list`` is followed until the cursor is exhausted."""

    async def test_all_pages_are_read(self):
        class FakeSession:
            def __init__(self):
                self.calls = []

            async def list_tools(self, cursor=None):
                self.calls.append(cursor)
                if cursor is None:
                    return types.SimpleNamespace(
                        tools=[
                            types.SimpleNamespace(name="a", description="", input_schema={}),
                            types.SimpleNamespace(name="b", description="", input_schema={}),
                        ],
                        next_cursor="page-2",
                    )
                return types.SimpleNamespace(
                    tools=[types.SimpleNamespace(name="c", description="", input_schema={})],
                    next_cursor=None,
                )

        session = FakeSession()
        client = SdkMcpClient("http://127.0.0.1:1/mcp")
        tools = await client._list_all(session)
        self.assertEqual([tool.name for tool in tools], ["a", "b", "c"])
        self.assertEqual(session.calls, [None, "page-2"])

    async def test_an_invalid_tools_payload_is_rejected(self):
        class BrokenSession:
            async def list_tools(self, cursor=None):
                return types.SimpleNamespace(tools="not-a-list", next_cursor=None)

        client = SdkMcpClient("http://127.0.0.1:1/mcp")
        with self.assertRaises(McpError) as caught:
            await client._list_all(BrokenSession())
        self.assertEqual(caught.exception.category, "invalid_response")


class ProbeSessionTest(unittest.IsolatedAsyncioTestCase):
    """``probe`` opens exactly one session and lists the tools once.

    Regression: the orchestrator and the API used to call ``status()`` and then
    ``list_tools()``, opening two MCP sessions (four HTTP requests) per probe.
    """

    class FakeClient:
        def __init__(self, entries, protocol_version="2026-07-28"):
            self.entries = entries
            self.list_calls = 0
            self.protocol_version = protocol_version
            self.server_info = types.SimpleNamespace(
                name="day-16-mcp-server", version="1.0.0"
            )

        async def __aenter__(self):
            self.entries.append(1)
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def list_tools(self, cursor=None):
            self.list_calls += 1
            return types.SimpleNamespace(
                tools=[
                    types.SimpleNamespace(
                        name="calculate", description="", input_schema={"type": "object"}
                    )
                ],
                next_cursor=None,
            )

        async def call_tool(self, name, arguments, read_timeout_seconds=None):
            return types.SimpleNamespace(
                is_error=False,
                content=[],
                structured_content={"result": 391},
            )

    async def test_probe_opens_one_session_and_lists_once(self):
        entries = []
        clients = []

        def factory(_url, _read_timeout):
            client = self.FakeClient(entries)
            clients.append(client)
            return client

        client = SdkMcpClient(
            "http://127.0.0.1:1/mcp", client_factory=factory, connect_timeout_s=3.0
        )
        status, tools = await client.probe()
        self.assertTrue(status.connected)
        self.assertEqual(status.protocol_version, "2026-07-28")
        self.assertEqual(status.server_name, "day-16-mcp-server")
        self.assertEqual([tool.name for tool in tools], ["calculate"])
        self.assertEqual(len(clients), 1)
        self.assertEqual(len(entries), 1)
        self.assertEqual(clients[0].list_calls, 1)

    async def test_status_delegates_to_probe(self):
        entries = []

        def factory(_url, _read_timeout):
            return self.FakeClient(entries)

        client = SdkMcpClient(
            "http://127.0.0.1:1/mcp", client_factory=factory, connect_timeout_s=3.0
        )
        status = await client.status()
        self.assertTrue(status.connected)
        self.assertEqual(len(entries), 1)


class UnreachableEndpointTest(unittest.IsolatedAsyncioTestCase):
    """A closed loopback port yields a controlled unreachable status.

    The connection is refused locally, so the test stays offline.
    """

    async def test_status_is_disconnected(self):
        client = SdkMcpClient(
            "http://127.0.0.1:1/mcp", connect_timeout_s=3.0, call_timeout_s=3.0
        )
        status = await client.status()
        self.assertFalse(status.connected)
        self.assertIsNotNone(status.error)
        self.assertIn(status.error.category, (CATEGORY_UNREACHABLE, CATEGORY_PROTOCOL))
        self.assertNotIn("sk-", status.error.message)

    async def test_call_tool_raises_a_sanitized_error(self):
        client = SdkMcpClient(
            "http://127.0.0.1:1/mcp", connect_timeout_s=3.0, call_timeout_s=3.0
        )
        with self.assertRaises(McpError) as caught:
            await client.call_tool("calculate", {"a": 1})
        self.assertIn(
            caught.exception.category, (CATEGORY_UNREACHABLE, CATEGORY_PROTOCOL)
        )


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
