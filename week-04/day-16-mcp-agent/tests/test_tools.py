"""Unit tests of the pure MCP tool implementations (no server, no network)."""

from __future__ import annotations

import unittest
from unittest import mock

from mcp_server import SERVER_NAME, SERVER_VERSION
from mcp_server import tools, web_search
from mcp_server.tools import ToolError


class CalculateTest(unittest.TestCase):
    """``calculate`` performs the four arithmetic operations."""

    def test_add(self):
        self.assertEqual(
            tools.calculate(operation="add", a=2, b=3),
            {"operation": "add", "a": 2, "b": 3, "result": 5},
        )

    def test_subtract(self):
        self.assertEqual(tools.calculate(operation="subtract", a=2, b=3)["result"], -1)

    def test_multiply(self):
        self.assertEqual(
            tools.calculate(operation="multiply", a=23, b=17)["result"], 391
        )

    def test_divide_returns_float(self):
        self.assertEqual(tools.calculate(operation="divide", a=7, b=2)["result"], 3.5)

    def test_divide_by_zero_is_controlled(self):
        with self.assertRaises(ToolError) as caught:
            tools.calculate(operation="divide", a=1, b=0)
        self.assertEqual(str(caught.exception), "Division by zero is not allowed")

    def test_negative_zero_divisor_is_controlled(self):
        with self.assertRaises(ToolError):
            tools.calculate(operation="divide", a=1, b=-0.0)

    def test_non_numeric_argument_is_controlled(self):
        with self.assertRaises(ToolError):
            tools.calculate(operation="add", a="2", b=3)

    def test_boolean_argument_is_controlled(self):
        with self.assertRaises(ToolError):
            tools.calculate(operation="add", a=True, b=1)

    def test_infinite_argument_is_controlled(self):
        with self.assertRaises(ToolError):
            tools.calculate(operation="add", a=float("inf"), b=1)

    def test_unsupported_operation_is_controlled(self):
        with self.assertRaises(ToolError):
            tools.calculate(operation="power", a=2, b=3)

    def test_tool_error_is_a_plain_exception(self):
        self.assertTrue(issubclass(ToolError, Exception))


class ServerInfoTest(unittest.TestCase):
    """``get_server_info`` is descriptive and leaks nothing."""

    def test_reports_name_version_status_uptime(self):
        info = tools.get_server_info()
        self.assertEqual(info["name"], SERVER_NAME)
        self.assertEqual(info["version"], SERVER_VERSION)
        self.assertEqual(info["status"], "ok")
        self.assertIsInstance(info["uptime_seconds"], float)
        self.assertGreaterEqual(info["uptime_seconds"], 0.0)

    def test_has_no_environment_paths_or_secrets(self):
        info = tools.get_server_info()
        forbidden = {"env", "environment", "path", "cwd", "api_key", "token", "secret"}
        self.assertFalse(forbidden & set(info))
        rendered = repr(info).lower()
        for marker in ("api_key", "authorization", "secret", "password"):
            self.assertNotIn(marker, rendered)


class _StubService:
    """A scripted ``WebSearchService`` for the tool-boundary tests."""

    def __init__(self, result=None, error=None):
        self.result = result or {
            "query": "cats",
            "count": 0,
            "results": [],
            "more_results_available": False,
            "note": "No results found for this query.",
        }
        self.error = error
        self.calls: list = []

    def search(self, query, max_results=0):
        self.calls.append((query, max_results))
        if self.error is not None:
            raise self.error
        return self.result


def _patch(service):
    return mock.patch.object(web_search, "default_service", return_value=service)


class SearchWebTest(unittest.TestCase):
    """``search_web`` validates its arguments and delegates to the service."""

    def test_delegates_and_returns_the_structured_result(self):
        service = _StubService(
            result={
                "query": "cats",
                "count": 1,
                "results": [
                    {"title": "Docs", "url": "https://docs.example.test/1", "description": "s"}
                ],
                "more_results_available": False,
                "note": web_search.SEARCH_NOTE,
            }
        )
        with _patch(service):
            result = tools.search_web("cats", 3)
        self.assertEqual(service.calls, [("cats", 3)])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["url"], "https://docs.example.test/1")

    def test_whitespace_is_trimmed_before_delegating(self):
        service = _StubService()
        with _patch(service):
            tools.search_web("  cats  ")
        self.assertEqual(service.calls, [("cats", 0)])

    def test_empty_query_is_rejected(self):
        for value in ("", "   "):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ToolError) as caught:
                    tools.search_web(value)
                self.assertEqual(
                    str(caught.exception),
                    "Argument 'query' must be a non-empty string",
                )

    def test_non_string_query_is_rejected(self):
        with self.assertRaises(ToolError):
            tools.search_web(None)
        with self.assertRaises(ToolError):
            tools.search_web(123)

    def test_boolean_max_results_is_rejected(self):
        with self.assertRaises(ToolError) as caught:
            tools.search_web("cats", True)
        self.assertEqual(
            str(caught.exception), "Argument 'max_results' must be an integer"
        )

    def test_non_integer_max_results_is_rejected(self):
        with self.assertRaises(ToolError):
            tools.search_web("cats", "3")

    def test_max_results_is_capped_before_delegating(self):
        service = _StubService()
        with _patch(service):
            tools.search_web("cats", 1000)
        self.assertEqual(service.calls, [("cats", 10)])

    def test_zero_max_results_is_forwarded_as_the_server_default(self):
        service = _StubService()
        with _patch(service):
            tools.search_web("cats", 0)
        self.assertEqual(service.calls, [("cats", 0)])

    def test_overlong_query_is_truncated(self):
        service = _StubService()
        with _patch(service):
            tools.search_web("x" * 700)
        self.assertEqual(len(service.calls[0][0]), web_search.MAX_QUERY_LENGTH)

    def test_search_error_becomes_a_tool_error(self):
        service = _StubService(
            error=web_search.SearchError("api_error", "The web search service failed (HTTP 500).")
        )
        with _patch(service):
            with self.assertRaises(ToolError) as caught:
                tools.search_web("cats")
        self.assertEqual(
            str(caught.exception), "The web search service failed (HTTP 500)."
        )

    def test_not_configured_message_never_mentions_the_key(self):
        service = _StubService(
            error=web_search.SearchError(
                "not_configured", web_search.NOT_CONFIGURED_MESSAGE
            )
        )
        with _patch(service):
            with self.assertRaises(ToolError) as caught:
                tools.search_web("cats")
        message = str(caught.exception)
        self.assertIn("not configured", message)
        for marker in ("Authorization", "Bearer", "api.tavily.com"):
            self.assertNotIn(marker, message)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
