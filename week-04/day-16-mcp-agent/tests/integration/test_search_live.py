"""Integration tests of the real ``search_web`` tool over real HTTP.

Every test here is skipped unless ``RUN_LIVE_MCP=1`` (``smoke_test.bat`` and
``harness/live_mcp.py`` set it). The MCP server runs as a real process, the MCP
transport is real HTTP and the search API is the loopback ``FakeSearchServer``
started by the harness, so nothing is mocked.

The MCP server is configured by the harness with
``MCP_LOAD_DOTENV=0``, ``TAVILY_API_KEY=<fake>`` and
``MCP_SEARCH_BASE_URL=http://127.0.0.1:<fake port>``.
"""

from __future__ import annotations

import json
import os
import unittest

from agent.mcp_adapter import SdkMcpClient, inspect_tools
from tests.support.fake_search import (
    EMPTY_MARKER,
    FAKE_API_KEY,
    RATE_LIMIT_MARKER,
    TIMEOUT_MARKER,
    UNAUTHORIZED_MARKER,
)

RUN_LIVE = os.environ.get("RUN_LIVE_MCP") == "1"
MCP_URL = os.environ.get("MCP_TEST_URL", "")


def _client(**kwargs):
    return SdkMcpClient(MCP_URL, **kwargs)


@unittest.skipUnless(RUN_LIVE and MCP_URL, "set RUN_LIVE_MCP=1 to run this test")
class LiveSearchWebTest(unittest.IsolatedAsyncioTestCase):
    """The real MCP server returns real results from the fake search API."""

    async def test_tool_is_advertised_with_the_query_contract(self):
        status, tools = await inspect_tools(MCP_URL, connect_timeout_s=10.0)
        self.assertTrue(status.connected, msg=str(status.error))
        names = [tool.name for tool in tools]
        self.assertIn("search_web", names)
        tool = next(tool for tool in tools if tool.name == "search_web")
        properties = tool.input_schema.get("properties", {})
        self.assertEqual(properties.get("query", {}).get("type"), "string")
        self.assertEqual(properties.get("max_results", {}).get("type"), "integer")
        self.assertEqual(set(tool.input_schema.get("required", [])), {"query"})

    async def test_success_returns_links_and_no_secret(self):
        result = await _client(call_timeout_s=20.0).call_tool(
            "search_web", {"query": "python documentation"}
        )
        self.assertTrue(result.ok, msg=result.text)
        payload = result.structured
        self.assertIsNotNone(payload)
        self.assertGreaterEqual(payload["count"], 1)
        for item in payload["results"]:
            self.assertTrue(item["url"].startswith("https://docs.example.test/"))
            self.assertTrue(item["title"])
        self.assertIn("Snippets only", payload["note"])
        self.assertNotIn(FAKE_API_KEY, json.dumps(payload, ensure_ascii=False))

    async def test_max_results_is_respected(self):
        result = await _client(call_timeout_s=20.0).call_tool(
            "search_web", {"query": "python documentation", "max_results": 1}
        )
        self.assertTrue(result.ok, msg=result.text)
        self.assertEqual(result.structured["count"], 1)

    async def test_empty_result_is_a_success(self):
        result = await _client(call_timeout_s=20.0).call_tool(
            "search_web", {"query": EMPTY_MARKER}
        )
        self.assertTrue(result.ok, msg=result.text)
        self.assertEqual(result.structured["count"], 0)
        self.assertEqual(result.structured["results"], [])
        self.assertIn("No results", result.structured["note"])

    async def test_api_unauthorized_is_reported_without_the_key(self):
        result = await _client(call_timeout_s=20.0).call_tool(
            "search_web", {"query": UNAUTHORIZED_MARKER}
        )
        self.assertFalse(result.ok)
        self.assertIn("HTTP 401", result.text)
        self.assertNotIn(FAKE_API_KEY, result.text)

    async def test_api_rate_limit_is_reported(self):
        result = await _client(call_timeout_s=20.0).call_tool(
            "search_web", {"query": RATE_LIMIT_MARKER}
        )
        self.assertFalse(result.ok)
        self.assertIn("HTTP 429", result.text)

    async def test_timeout_is_controlled(self):
        result = await _client(call_timeout_s=20.0).call_tool(
            "search_web", {"query": TIMEOUT_MARKER}
        )
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.text)
        self.assertNotIn(FAKE_API_KEY, result.text)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
