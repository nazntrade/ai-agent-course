"""MCP server assembly and process entry point of the notifier (server B).

The server is a separate loopback process speaking Streamable HTTP on its own
port with its own ``tools/list`` at ``/mcp``. It exposes the six notification
tools of ``notifier_server.tools`` and owns its own SQLite file; it never
imports ``agent.*``.

Run it with ``python -m notifier_server``. A port that is already in use is a
prerequisite error: the process prints a short explanation and exits with code
2 without touching the process that owns the port.
"""

from __future__ import annotations

import os
import socket
import sys

from mcp.server import MCPServer as SdkMCPServer

from notifier_server import SERVER_NAME, SERVER_VERSION
from notifier_server import tools

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766

# The documented bind address is ``MCP_NOTIFIER_HOST``/``MCP_NOTIFIER_PORT``
# (SPEC §3.12, ACCEPTANCE D20-18, .env.example). The legacy
# ``NOTIFIER_HOST``/``NOTIFIER_PORT`` names stay as a fallback so an existing
# harness configuration keeps working; the primary names win when both are set.
HOST_ENV = "MCP_NOTIFIER_HOST"
PORT_ENV = "MCP_NOTIFIER_PORT"
LEGACY_HOST_ENV = "NOTIFIER_HOST"
LEGACY_PORT_ENV = "NOTIFIER_PORT"

# Exit codes: 0 normal stop, 2 prerequisite (busy port / bind failure).
EXIT_OK = 0
EXIT_PREREQUISITE = 2


def _first_env(environment, names) -> str:
    """Return the first non-empty value among ``names``, or an empty string."""
    for name in names:
        value = str(environment.get(name) or "").strip()
        if value:
            return value
    return ""


def resolve_host_port(env=None) -> tuple[str, int]:
    """Read the bind address from the environment, falling back to defaults.

    ``MCP_NOTIFIER_HOST``/``MCP_NOTIFIER_PORT`` are the documented names and take
    precedence; ``NOTIFIER_HOST``/``NOTIFIER_PORT`` are accepted as a legacy
    fallback so an existing harness keeps working.
    """
    environment = os.environ if env is None else env
    host = _first_env(environment, (HOST_ENV, LEGACY_HOST_ENV)) or DEFAULT_HOST
    raw_port = _first_env(environment, (PORT_ENV, LEGACY_PORT_ENV))
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


class NotifierServer:
    """The notifier MCP server: the SDK ``MCPServer`` plus six tools."""

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
        # receives the bind address. Registering the tools must not open the
        # database or read the Telegram configuration.
        server = SdkMCPServer(self.name, version=self.version)
        server.tool()(tools.create_notification_watch)
        server.tool()(tools.list_notification_watches)
        server.tool()(tools.evaluate_run)
        server.tool()(tools.send_notification)
        server.tool()(tools.get_delivery_status)
        server.tool()(tools.stop_notification_watch)
        return server

    def run(self) -> None:
        """Serve Streamable HTTP on ``/mcp`` (the SDK default path)."""
        self.app.run(
            transport="streamable-http", host=self.host, port=self.port
        )


def main(argv=None) -> int:
    """Entry point of ``python -m notifier_server``."""
    host, port = resolve_host_port()
    if not port_is_available(host, port):
        print(
            f"Notifier server error: {host}:{port} is already in use. "
            "Stop the process that owns the port or change MCP_NOTIFIER_PORT.",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_PREREQUISITE
    try:
        NotifierServer(host=host, port=port).run()
    except OSError as exc:
        print(
            f"Notifier server error: could not bind {host}:{port} ({exc}).",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_PREREQUISITE
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
