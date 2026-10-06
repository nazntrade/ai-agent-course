"""Verified MCP connection point (SPEC 9, R6.2) — description only.

Source studied: ``week-04/day-16-mcp-agent/agent/mcp_adapter.py``. That module is
the single place that knows MCP; everything else depends on transport-agnostic
objects. D26 does **not** implement full MCP integration; it records the narrow
port through which MCP results could later join memory + RAG in the context
extension point (R6.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

# Error categories copied from the week-04 adapter contract.
ERROR_CATEGORIES = (
    "unreachable",
    "timeout",
    "protocol",
    "invalid_response",
    "tool_error",
    "not_configured",
)


@dataclass
class McpTool:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class McpStatus:
    connected: bool
    error_category: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.error_category is not None and self.error_category not in ERROR_CATEGORIES:
            raise ValueError("unknown MCP error category")


@dataclass
class McpCallResult:
    tool: str
    ok: bool
    content: Any = None
    error_category: str | None = None


@runtime_checkable
class McpClient(Protocol):
    """The verified connection point: narrow, transport-agnostic operations."""

    def probe(self) -> McpStatus: ...

    def status(self) -> McpStatus: ...

    def list_tools(self) -> Sequence[McpTool]: ...

    def call_tool(self, name: str, arguments: dict[str, Any]) -> McpCallResult: ...


def describe_connection_point() -> dict[str, Any]:
    """Static, secret-free description used by the API/docs."""
    return {
        "source": "week-04/day-16-mcp-agent/agent/mcp_adapter.py",
        "port": ["probe", "status", "list_tools", "call_tool"],
        "error_categories": list(ERROR_CATEGORIES),
        "integration": "extension point only; no live MCP calls in D26",
        "extension": "MCP results may join task memory and RAG fragments in ContextBuilder",
    }
