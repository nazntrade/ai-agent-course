"""MCP server assembly and process entry point.

The server is a separate loopback process speaking Streamable HTTP on its own
port. It exposes only the two read-only tools of ``mcp_server.tools``: there is
no shell, filesystem, network or Git access.

Run it with ``python -m mcp_server``. A port that is already in use is a
prerequisite error: the process prints a short explanation and exits with code
2 without touching the process that owns the port.
"""

from __future__ import annotations

import os
import socket
import sys

from mcp.server import MCPServer as SdkMCPServer

from mcp_server import SERVER_NAME, SERVER_VERSION
from mcp_server import tools

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

HOST_ENV = "MCP_SERVER_HOST"
PORT_ENV = "MCP_SERVER_PORT"

# Exit codes: 0 normal stop, 2 prerequisite (busy port / bind failure).
EXIT_OK = 0
EXIT_PREREQUISITE = 2


def resolve_host_port(env=None) -> tuple[str, int]:
    """Read the bind address from the environment, falling back to defaults."""
    environment = os.environ if env is None else env
    host = str(environment.get(HOST_ENV) or DEFAULT_HOST).strip() or DEFAULT_HOST
    raw_port = str(environment.get(PORT_ENV) or "").strip()
    try:
        port = int(raw_port) if raw_port else DEFAULT_PORT
    except ValueError:
        port = DEFAULT_PORT
    if not 1 <= port <= 65535:
        port = DEFAULT_PORT
    return host, port


def port_is_available(host: str, port: int) -> bool:
    """Whether a loopback port can still be bound right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, int(port)))
        except OSError:
            return False
    return True


class MCPServer:
    """The MCP server: the SDK ``MCPServer`` plus the two registered tools."""

    def __init__(
        self,
        name: str = SERVER_NAME,
        version: str = SERVER_VERSION,
        host: str | None = None,
        port: int | None = None,
    ):
        self.name = name
        self.version = version
        self.host = host or DEFAULT_HOST
        self.port = int(port or DEFAULT_PORT)
        self.app = self._build()

    def _build(self) -> SdkMCPServer:
        # v2 keeps transport settings out of the constructor: only ``run()``
        # receives the bind address.
        server = SdkMCPServer(self.name, version=self.version)
        server.tool()(tools.calculate)
        server.tool()(tools.get_server_info)
        return server

    def run(self) -> None:
        """Serve Streamable HTTP in the current process."""
        self.app.run(transport="streamable-http", host=self.host, port=self.port)


def main(argv=None) -> int:
    """Entry point of ``python -m mcp_server``."""
    host, port = resolve_host_port()
    if not port_is_available(host, port):
        print(
            f"MCP server error: {host}:{port} is already in use. "
            "Stop the process that owns the port or change MCP_SERVER_PORT.",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_PREREQUISITE
    try:
        MCPServer(host=host, port=port).run()
    except OSError as exc:
        print(
            f"MCP server error: could not bind {host}:{port} ({exc}).",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_PREREQUISITE
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
