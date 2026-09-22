"""The MCP boundary of the agent.

This is the only module that knows the Model Context Protocol. Everything else
(the CLI formatter, the orchestrator, the HTTP API) works with the small,
transport-agnostic value objects defined here.

It targets the MCP Python SDK v2: a single high-level ``mcp.Client`` owns the
transport, the handshake and the session. Every call opens a fresh session to
the loopback MCP server, does its work and closes the session. Failures are
turned into :class:`McpError` values with a coarse category and a message that
never contains a credential, a header, an environment variable or a secret.
"""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp_types import REQUEST_TIMEOUT

CATEGORY_UNREACHABLE = "unreachable"
CATEGORY_TIMEOUT = "timeout"
CATEGORY_PROTOCOL = "protocol"
CATEGORY_INVALID_RESPONSE = "invalid_response"
CATEGORY_TOOL_ERROR = "tool_error"

CATEGORIES = (
    CATEGORY_UNREACHABLE,
    CATEGORY_TIMEOUT,
    CATEGORY_PROTOCOL,
    CATEGORY_INVALID_RESPONSE,
    CATEGORY_TOOL_ERROR,
)

DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_CALL_TIMEOUT_SECONDS = 30.0
SSE_READ_TIMEOUT_SECONDS = 300.0

# Bounds of the tools/list pagination loop: a malformed server must not make the
# client loop forever.
MAX_TOOL_PAGES = 50
MAX_TOOLS = 500


class McpError(Exception):
    """A sanitized MCP failure with a coarse category."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category if category in CATEGORIES else CATEGORY_PROTOCOL
        self.message = str(message)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


@dataclass(frozen=True)
class McpTool:
    """One tool advertised by the MCP server."""

    name: str
    title: str | None
    description: str
    input_schema: dict


@dataclass(frozen=True)
class McpStatus:
    """Result of a connection + handshake + tools/list probe."""

    connected: bool
    protocol_version: str | None = None
    server_name: str | None = None
    server_version: str | None = None
    tools_count: int = 0
    error: McpError | None = None


@dataclass(frozen=True)
class McpCallResult:
    """The outcome of one ``tools/call``."""

    ok: bool
    text: str
    structured: dict | None = None


@runtime_checkable
class McpClient(Protocol):
    """The MCP operations the agent depends on."""

    async def probe(self) -> tuple[McpStatus, list[McpTool]]: ...

    async def status(self) -> McpStatus: ...

    async def list_tools(self) -> list[McpTool]: ...

    async def call_tool(self, name: str, arguments: dict) -> McpCallResult: ...


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    """Read the first present attribute or mapping key of ``obj``."""
    if obj is None:
        return default
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _endpoint_label(url: str) -> str:
    """Return ``host:port`` of the loopback endpoint, never a full URL."""
    try:
        split = urlsplit(str(url or ""))
    except ValueError:
        return "the MCP endpoint"
    host = split.hostname or "127.0.0.1"
    port = split.port
    return f"{host}:{port}" if port else host


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    """Flatten an ``ExceptionGroup``/``BaseExceptionGroup`` into its leaves."""
    leaves: list[BaseException] = []
    pending = [exc]
    seen = 0
    while pending and seen < 100:
        current = pending.pop()
        seen += 1
        children = getattr(current, "exceptions", None)
        if children:
            pending.extend(children)
        else:
            leaves.append(current)
    return leaves


def _classify(exc: BaseException, url: str) -> tuple[str, str]:
    """Map an arbitrary exception to a safe (category, message) pair."""
    leaves = _leaf_exceptions(exc) or [exc]
    names = {type(item).__name__ for item in leaves}

    if any(
        isinstance(item, MCPError) and getattr(item, "code", None) == REQUEST_TIMEOUT
        for item in leaves
    ):
        return CATEGORY_TIMEOUT, "The MCP server did not answer within the timeout"
    if "TimeoutError" in names or "TimeoutException" in names:
        return CATEGORY_TIMEOUT, "The MCP server did not answer within the timeout"
    if "ConnectError" in names or "ConnectionRefusedError" in names:
        return (
            CATEGORY_UNREACHABLE,
            f"The MCP server is not reachable ({_endpoint_label(url)})",
        )
    if "SSLError" in names or "ConnectionError" in names or "OSError" in names:
        return (
            CATEGORY_UNREACHABLE,
            f"The MCP server is not reachable ({_endpoint_label(url)})",
        )
    if "ValidationError" in names or "JSONDecodeError" in names:
        return (
            CATEGORY_INVALID_RESPONSE,
            "The MCP server returned an unexpected response",
        )
    return CATEGORY_PROTOCOL, "The MCP handshake with the server failed"


def _content_text(content: Any) -> str:
    """Join the text parts of a tool result content list."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    try:
        items = list(content)
    except TypeError:
        return str(content)
    for item in items:
        text = _attr(item, "text")
        if text is not None:
            parts.append(str(text))
            continue
        if isinstance(item, dict) and "text" in item:
            parts.append(str(item["text"]))
    return "\n".join(parts)


def _structured(result: Any) -> dict | None:
    payload = _attr(result, "structuredContent", "structured_content")
    return payload if isinstance(payload, dict) else None


def _to_mcp_tool(tool: Any) -> McpTool | None:
    name = _attr(tool, "name")
    if not isinstance(name, str) or not name:
        return None
    title = _attr(tool, "title")
    if not isinstance(title, str) or not title:
        annotations = _attr(tool, "annotations")
        candidate = _attr(annotations, "title")
        title = candidate if isinstance(candidate, str) and candidate else None
    description = _attr(tool, "description")
    schema = _attr(tool, "inputSchema", "input_schema")
    return McpTool(
        name=name,
        title=title,
        description=description if isinstance(description, str) else "",
        input_schema=dict(schema) if isinstance(schema, dict) else {},
    )


def _default_client_factory(url: str, read_timeout_seconds: float):
    """The production seam: a fresh high-level SDK client for ``url``.

    ``mode='auto'`` probes ``server/discover`` and falls back to the legacy
    ``initialize`` handshake, so one client speaks the 2026-07-28 revision and
    every earlier one.
    """
    return Client(url, read_timeout_seconds=read_timeout_seconds, mode="auto")


class SdkMcpClient:
    """The official-SDK (v2) implementation of :class:`McpClient`."""

    def __init__(
        self,
        url: str,
        *,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        call_timeout_s: float = DEFAULT_CALL_TIMEOUT_SECONDS,
        client_factory=None,
    ):
        self.url = str(url)
        self.connect_timeout_s = float(connect_timeout_s)
        self.call_timeout_s = float(call_timeout_s)
        # Tests inject a factory to count sessions; production uses ``Client``.
        self._client_factory = client_factory or _default_client_factory
        self._read_timeout_s = max(self.connect_timeout_s, SSE_READ_TIMEOUT_SECONDS)

    @asynccontextmanager
    async def session_scope(self):
        """Open a handshaken MCP session and always close it again."""
        try:
            async with self._client_factory(self.url, self._read_timeout_s) as client:
                yield client
        except McpError:
            raise
        except asyncio.TimeoutError as exc:
            raise McpError(
                CATEGORY_TIMEOUT, "The MCP server did not answer within the timeout"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - reported as a sanitized category
            category, message = _classify(exc, self.url)
            raise McpError(category, message) from exc

    async def _list_all(self, client: Any) -> list[McpTool]:
        """Read every page of ``tools/list`` until the cursor is exhausted."""
        collected: list[McpTool] = []
        cursor: str | None = None
        for _ in range(MAX_TOOL_PAGES):
            result = await client.list_tools(cursor=cursor)
            page = _attr(result, "tools") or []
            if not isinstance(page, (list, tuple)):
                raise McpError(
                    CATEGORY_INVALID_RESPONSE,
                    "The MCP server returned an unexpected tools list",
                )
            for item in page:
                tool = _to_mcp_tool(item)
                if tool is not None:
                    collected.append(tool)
            if len(collected) > MAX_TOOLS:
                collected = collected[:MAX_TOOLS]
                break
            next_cursor = _attr(result, "next_cursor", "nextCursor")
            if not next_cursor:
                return collected
            cursor = str(next_cursor)
        return collected

    @staticmethod
    def _identity(client: Any) -> tuple[str | None, str | None, str | None]:
        """Return ``(protocol_version, server_name, server_version)``."""
        version = _attr(client, "protocol_version")
        info = _attr(client, "server_info")
        name = _attr(info, "name")
        server_version = _attr(info, "version")
        return (
            str(version) if version else None,
            str(name) if name else None,
            str(server_version) if server_version else None,
        )

    async def probe(self) -> tuple[McpStatus, list[McpTool]]:
        """Handshake and read the full tool list in a single MCP session.

        This is the one entry point the orchestrator and the HTTP API use, so a
        probe is exactly one MCP connection (``server/discover`` followed by
        ``tools/list``) instead of two.
        """
        try:
            async with asyncio.timeout(self.connect_timeout_s):
                async with self.session_scope() as client:
                    tools = await self._list_all(client)
                    version, name, server_version = self._identity(client)
                    return (
                        McpStatus(
                            connected=True,
                            protocol_version=version,
                            server_name=name,
                            server_version=server_version,
                            tools_count=len(tools),
                        ),
                        tools,
                    )
        except McpError as exc:
            return McpStatus(connected=False, error=exc), []
        except asyncio.TimeoutError:
            return (
                McpStatus(
                    connected=False,
                    error=McpError(
                        CATEGORY_TIMEOUT,
                        "The MCP server did not answer within the timeout",
                    ),
                ),
                [],
            )

    async def status(self) -> McpStatus:
        """Probe connectivity, handshake, server identity and tool count."""
        status, _tools = await self.probe()
        return status

    async def list_tools(self) -> list[McpTool]:
        """Return every advertised tool, or raise :class:`McpError`."""
        try:
            async with asyncio.timeout(self.connect_timeout_s):
                async with self.session_scope() as client:
                    return await self._list_all(client)
        except McpError:
            raise
        except asyncio.TimeoutError as exc:
            raise McpError(
                CATEGORY_TIMEOUT, "The MCP server did not answer within the timeout"
            ) from exc

    async def call_tool(self, name: str, arguments: dict) -> McpCallResult:
        """Call one tool; a tool-level failure is a result, not an exception."""
        try:
            async with asyncio.timeout(self.call_timeout_s):
                async with self.session_scope() as client:
                    result = await client.call_tool(
                        name, arguments, read_timeout_seconds=self.call_timeout_s
                    )
        except McpError:
            raise
        except asyncio.TimeoutError as exc:
            raise McpError(
                CATEGORY_TIMEOUT, "The MCP tool call did not finish within the timeout"
            ) from exc

        is_error = bool(_attr(result, "is_error", "isError", default=False))
        text = _content_text(_attr(result, "content"))
        structured = _structured(result)
        if is_error and not text and structured is None:
            text = "The tool reported a failure"
        return McpCallResult(ok=not is_error, text=text, structured=structured)


async def inspect_tools(
    url: str,
    *,
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
) -> tuple[McpStatus, list[McpTool]]:
    """Handshake once and read the full tool list for the discovery CLI."""
    client = SdkMcpClient(url, connect_timeout_s=connect_timeout_s)
    return await client.probe()


def is_loopback_url(url: str) -> bool:
    """Whether ``url`` points at the loopback interface."""
    try:
        split = urlsplit(str(url or ""))
    except ValueError:
        return False
    return (split.hostname or "") in ("127.0.0.1", "localhost", "::1")


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    """Whether nothing is listening on a loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, int(port))) != 0


def tool_result_as_text(result: McpCallResult) -> str:
    """Return the model-facing text of a tool result."""
    if result.text:
        return result.text
    if result.structured is not None:
        return json.dumps(result.structured, ensure_ascii=False)
    return "{}"
