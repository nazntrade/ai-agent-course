"""Day 20 notifier MCP server (server B) package metadata.

The server name and version live here so the tool implementations and the
server assembly share a single source of truth. The package is a separate
process with its own database and tools; it never imports ``agent.*``, exactly
like ``mcp_server`` (server A).
"""

SERVER_NAME = "day-20-notifier"
SERVER_VERSION = "1.0.0"
