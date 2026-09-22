"""Contract test of the *installed* MCP SDK (v2).

This is deliberately the first test of the suite: the project is built on the
official SDK, so the assumptions the rest of the code makes about its API are
asserted here against the real installed package instead of being guessed.

The load-bearing assertions mirror the exact calls the production code makes:

* ``Client(url, read_timeout_seconds=..., mode="auto")`` as an async context
  manager with ``protocol_version`` / ``server_info`` / ``list_tools`` /
  ``call_tool``;
* ``mcp.server.MCPServer(name, version=...)`` with ``tool()`` and
  ``run(transport="streamable-http", host=..., port=...)``;
* the snake_case field names of ``InitializeResult``, ``ListToolsResult`` and
  ``CallToolResult`` that the adapter reads.
"""

from __future__ import annotations

import importlib.metadata
import inspect
import unittest


def _sdk_version() -> str | None:
    try:
        return importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        return None


def _params(function) -> set:
    try:
        return set(inspect.signature(function).parameters)
    except (TypeError, ValueError):  # pragma: no cover - builtins only
        return set()


class SdkContractTest(unittest.TestCase):
    """The installed MCP SDK exposes the v2 API the project relies on."""

    def test_sdk_is_installed(self):
        version = _sdk_version()
        print(f"\nInstalled mcp SDK version: {version}")
        self.assertIsNotNone(version, "the 'mcp' package is not installed")
        self.assertGreaterEqual(int(version.split(".")[0]), 2)

    def test_client_api(self):
        from mcp import Client

        print(f"\nClient.__init__: {inspect.signature(Client.__init__)}")
        for name in ("list_tools", "call_tool", "__aenter__", "__aexit__"):
            self.assertTrue(hasattr(Client, name), f"Client.{name} is missing")
        params = _params(Client.__init__)
        self.assertIn("read_timeout_seconds", params)
        self.assertIn("mode", params)

    def test_mcpserver_registration_and_run(self):
        from mcp.server import MCPServer

        print(f"\nMCPServer.__init__: {inspect.signature(MCPServer.__init__)}")
        print(f"MCPServer.run: {inspect.signature(MCPServer.run)}")
        self.assertIn("transport", _params(MCPServer.run))

        server = MCPServer("contract-probe", version="0.0.1")
        self.assertTrue(hasattr(server, "tool"), "MCPServer.tool is missing")

        @server.tool()
        def echo(value: str) -> dict:
            """Echo one string."""
            return {"echo": value}

        self.assertTrue(callable(echo))

    def test_result_field_names_are_snake_case(self):
        from mcp.types import CallToolResult, InitializeResult, ListToolsResult

        init_fields = set(InitializeResult.model_fields)
        print(f"\nInitializeResult fields: {sorted(init_fields)}")
        self.assertIn("protocol_version", init_fields)
        self.assertIn("server_info", init_fields)
        self.assertNotIn("protocolVersion", init_fields)

        list_fields = set(ListToolsResult.model_fields)
        print(f"ListToolsResult fields: {sorted(list_fields)}")
        self.assertIn("tools", list_fields)
        self.assertIn("next_cursor", list_fields)

        call_fields = set(CallToolResult.model_fields)
        print(f"CallToolResult fields: {sorted(call_fields)}")
        self.assertIn("is_error", call_fields)
        self.assertIn("content", call_fields)
        self.assertIn("structured_content", call_fields)

    def test_protocol_version_constants(self):
        from mcp.types.version import (
            KNOWN_PROTOCOL_VERSIONS,
            LATEST_PROTOCOL_VERSION,
        )

        print(f"\nLATEST_PROTOCOL_VERSION: {LATEST_PROTOCOL_VERSION}")
        self.assertIn(LATEST_PROTOCOL_VERSION, KNOWN_PROTOCOL_VERSIONS)
        self.assertTrue(LATEST_PROTOCOL_VERSION)
        self.assertNotEqual(LATEST_PROTOCOL_VERSION, "2025-11-25")

    def test_mcp_error_is_importable(self):
        from mcp.shared.exceptions import MCPError

        self.assertTrue(issubclass(MCPError, Exception))
        self.assertTrue(hasattr(MCPError, "message"))

    def test_removed_v1_helper_is_gone(self):
        import mcp.shared.memory as memory

        self.assertFalse(
            hasattr(memory, "create_connected_server_and_client_session"),
            "the v1 in-process helper should be gone; use Client(server.app)",
        )


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
