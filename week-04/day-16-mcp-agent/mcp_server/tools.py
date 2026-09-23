"""Pure implementations of the MCP tools.

The arithmetic and informational functions are plain, synchronous and
side-effect free so they can be unit tested without a server.
``mcp_server/server.py`` only registers them on the MCP server; the server has
no shell, filesystem or Git access. ``search_web`` delegates to
:mod:`mcp_server.web_search`, the single module that performs one outgoing
HTTPS request.

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
from mcp_server import web_search

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


# Concrete return annotation keeps the result structured (see ``calculate``).
# ``max_results`` stays a plain ``int`` default instead of ``int | None``: the
# server sanitizes the generated schema, which keeps only the first type of a
# union, and ``0`` already means "use the server default".
def search_web(query: str, max_results: int = 0) -> dict[str, Any]:
    """Search the web for current information.

    Returns short result titles, links and snippets for a query. The pages are
    not opened: the result is a snippet list, not their full text.
    """
    if not isinstance(query, str) or not query.strip():
        raise ToolError("Argument 'query' must be a non-empty string")
    if isinstance(max_results, bool) or not isinstance(max_results, int):
        raise ToolError("Argument 'max_results' must be an integer")

    trimmed = query.strip()
    if len(trimmed) > web_search.MAX_QUERY_LENGTH:
        trimmed = trimmed[: web_search.MAX_QUERY_LENGTH]
    if max_results > web_search.SEARCH_MAX_RESULTS_CAP:
        max_results = web_search.SEARCH_MAX_RESULTS_CAP

    try:
        return web_search.default_service().search(trimmed, max_results)
    except web_search.SearchError as exc:
        raise ToolError(exc.message) from exc
