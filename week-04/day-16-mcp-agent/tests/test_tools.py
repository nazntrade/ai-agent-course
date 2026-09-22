"""Unit tests of the pure MCP tool implementations (no server, no network)."""

from __future__ import annotations

import unittest

from mcp_server import SERVER_NAME, SERVER_VERSION
from mcp_server import tools
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


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
