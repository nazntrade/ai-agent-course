"""Unit tests of the discovery CLI formatting and exit codes (no MCP server)."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from agent.mcp_adapter import McpError, McpStatus, McpTool
from mcp.types.version import LATEST_PROTOCOL_VERSION
import discovery_cli


def _status(connected=True, error=None):
    if not connected:
        return McpStatus(connected=False, error=error)
    return McpStatus(
        connected=True,
        protocol_version=LATEST_PROTOCOL_VERSION,
        server_name="day-16-mcp-server",
        server_version="1.0.0",
        tools_count=2,
    )


def _tools():
    return [
        McpTool(
            name="calculate",
            title=None,
            description="Do a basic arithmetic operation.",
            input_schema={"type": "object", "properties": {}},
        ),
        McpTool(
            name="get_server_info",
            title=None,
            description="Report server information.",
            input_schema={"type": "object", "properties": {}},
        ),
    ]


def _run_main(argv, status, tools):
    async def fake_discover(url, *, connect_timeout_s=10.0):
        return status, tools

    buffer = io.StringIO()
    with mock.patch.object(discovery_cli, "discover", fake_discover):
        with contextlib.redirect_stdout(buffer):
            code = discovery_cli.main(argv)
    return code, buffer.getvalue()


class FormatTest(unittest.TestCase):
    """The printed report carries the required keys and nothing secret."""

    def test_success_report(self):
        text = discovery_cli.format_success(_status(), _tools())
        for expected in (
            "CONNECTED",
            f"PROTOCOL_VERSION: {LATEST_PROTOCOL_VERSION}",
            "SERVER_INFO name=day-16-mcp-server version=1.0.0",
            "TOOLS_COUNT: 2",
            "TOOL: calculate",
            "DESCRIPTION: Do a basic arithmetic operation.",
            "INPUT_SCHEMA:",
        ):
            self.assertIn(expected, text)

    def test_failure_report_hides_the_raw_error(self):
        error = McpError("unreachable", "The MCP server is not reachable (127.0.0.1:1)")
        text = discovery_cli.format_failure(error)
        self.assertIn("CONNECTED: false", text)
        self.assertIn("ERROR_CATEGORY: unreachable", text)
        self.assertIn("ERROR: The MCP server is not reachable", text)

    def test_deserialized_error_is_reported_as_protocol(self):
        error = McpError("tool_error", "tool failed")
        text = discovery_cli.format_failure(error)
        self.assertIn("ERROR_CATEGORY: protocol", text)

    def test_missing_error_is_handled(self):
        text = discovery_cli.format_failure(None)
        self.assertIn("ERROR_CATEGORY: protocol", text)


class ExitCodeTest(unittest.TestCase):
    """Exit codes and quiet/JSON modes."""

    def test_success_returns_zero(self):
        code, output = _run_main([], _status(), _tools())
        self.assertEqual(code, 0)
        self.assertIn("CONNECTED", output)

    def test_failure_returns_two(self):
        code, output = _run_main(
            [], _status(False, McpError("unreachable", "nope")), []
        )
        self.assertEqual(code, 2)
        self.assertIn("CONNECTED: false", output)

    def test_check_mode_is_quiet_on_success(self):
        code, output = _run_main(["--check"], _status(), _tools())
        self.assertEqual(code, 0)
        self.assertEqual(output, "")

    def test_check_mode_is_quiet_on_failure(self):
        code, output = _run_main(["--check"], _status(False, McpError("timeout", "x")), [])
        self.assertEqual(code, 2)
        self.assertEqual(output, "")

    def test_json_mode_is_valid_json(self):
        code, output = _run_main(["--json"], _status(), _tools())
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["connected"])
        self.assertEqual(payload["tools_count"], 2)
        self.assertEqual(payload["tools"][0]["name"], "calculate")

    def test_json_failure_mode(self):
        code, output = _run_main(["--json"], _status(False, McpError("unreachable", "nope")), [])
        self.assertEqual(code, 2)
        payload = json.loads(output)
        self.assertFalse(payload["connected"])
        self.assertEqual(payload["error"]["category"], "unreachable")


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
