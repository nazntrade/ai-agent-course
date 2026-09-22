"""Pure implementations of the MCP tools.

The functions are plain, synchronous and side-effect free so they can be unit
tested without a server. ``mcp_server/server.py`` only registers them on the
MCP server; no shell, filesystem, network or Git access is available here.

A controlled failure (for example division by zero) raises the SDK's
:class:`ToolError`. The MCP layer turns that into a tool error result instead of
crashing the server, and the message is safe to show to a caller: it contains no
configuration, paths or secrets.
"""

from __future__ import annotations

import math
import time
from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError

from mcp_server import SERVER_NAME, SERVER_VERSION

Operation = Literal["add", "subtract", "multiply", "divide"]

# Monotonic clock of the process that hosts the tools, used for the uptime.
_STARTED_AT = time.time()


def _as_number(value, name: str) -> float:
    """Return ``value`` as a finite float or raise a controlled error."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"Argument '{name}' must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ToolError(f"Argument '{name}' must be a finite number")
    return number


def _plain(number: float):
    """Drop the trailing ``.0`` of integral results for a compact payload."""
    if number == int(number):
        return int(number)
    return number


# The concrete ``dict[str, Any]`` return annotation is deliberate: a bare
# ``dict`` makes the MCP server serve the payload as text only, while the SPEC
# requires a structured tool result. The docstring stays the model-facing tool
# description and must not mention implementation details.
def calculate(operation: Operation, a: float, b: float) -> dict[str, Any]:
    """Do a basic arithmetic operation on two numbers.

    Supported operations: add, subtract, multiply and divide.
    """
    left = _as_number(a, "a")
    right = _as_number(b, "b")

    if operation == "add":
        result = left + right
    elif operation == "subtract":
        result = left - right
    elif operation == "multiply":
        result = left * right
    elif operation == "divide":
        if right == 0:
            raise ToolError("Division by zero is not allowed")
        result = left / right
    else:
        raise ToolError(f"Unsupported operation: {operation}")

    return {
        "operation": operation,
        "a": _plain(left),
        "b": _plain(right),
        "result": _plain(result),
    }


# Concrete return annotation keeps the result structured (see ``calculate``).
def get_server_info() -> dict[str, Any]:
    """Report the name, version, status and uptime of this MCP server.

    The payload deliberately carries no environment variables, filesystem
    paths, configuration or credentials.
    """
    return {
        "name": SERVER_NAME,
        "version": SERVER_VERSION,
        "status": "ok",
        "uptime_seconds": round(max(time.time() - _STARTED_AT, 0.0), 3),
    }
