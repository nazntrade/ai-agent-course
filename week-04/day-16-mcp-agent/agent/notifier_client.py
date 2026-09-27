"""Host-side reads from the notifier server (server B).

The backend never opens the notifier database directly: every read goes through
server B's MCP tools. This module adds the two reads the API and the UI need,
injects the ``chat_id`` itself (the model and the browser never choose it) and
turns any B failure into a controlled :class:`NotifierUnavailable` so a missing
server becomes ``available:false`` instead of an HTTP 5xx.
"""

from __future__ import annotations

import asyncio

from agent.mcp_adapter import McpClient, McpError

CATEGORY_NOT_CONFIGURED = "not_configured"
CATEGORY_UNREACHABLE = "unreachable"
CATEGORY_TIMEOUT = "timeout"
CATEGORY_PROTOCOL = "protocol"
CATEGORY_TOOL_ERROR = "tool_error"

DEFAULT_TIMEOUT_SECONDS = 5.0

LIST_WATCHES_TOOL = "list_notification_watches"
GET_DELIVERY_STATUS_TOOL = "get_delivery_status"


class NotifierUnavailable(Exception):
    """A controlled notifier failure with a sanitized category."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = str(category)
        self.message = str(message)


class NotifierClient:
    """Read-only host→B client for watches and deliveries."""

    def __init__(self, client: McpClient | None, *, timeout_s: float = DEFAULT_TIMEOUT_SECONDS):
        self._client = client
        self._timeout_s = max(float(timeout_s), 0.1)

    @property
    def configured(self) -> bool:
        return self._client is not None

    async def list_watches(self, chat_id: str) -> dict:
        """Return B's watch list for ``chat_id`` (``{count, watches}``)."""
        payload = await self._call(LIST_WATCHES_TOOL, {"chat_id": str(chat_id)})
        if not isinstance(payload, dict):
            raise NotifierUnavailable(
                CATEGORY_PROTOCOL, "The notifier returned no structured watches"
            )
        return payload

    async def list_deliveries(self, chat_id: str) -> dict:
        """Return B's recent deliveries for ``chat_id`` (``{count, deliveries}``)."""
        payload = await self._call(
            GET_DELIVERY_STATUS_TOOL, {"watch_id": "", "chat_id": str(chat_id)}
        )
        if not isinstance(payload, dict):
            raise NotifierUnavailable(
                CATEGORY_PROTOCOL, "The notifier returned no structured deliveries"
            )
        return payload

    async def _call(self, name: str, arguments: dict) -> dict | None:
        if self._client is None:
            raise NotifierUnavailable(
                CATEGORY_NOT_CONFIGURED, "The notifier server is not configured"
            )
        try:
            async with asyncio.timeout(self._timeout_s):
                result = await self._client.call_tool(name, arguments)
        except asyncio.TimeoutError as exc:
            raise NotifierUnavailable(
                CATEGORY_TIMEOUT, "The notifier server did not answer in time"
            ) from exc
        except McpError as exc:
            raise NotifierUnavailable(exc.category, exc.message) from exc
        if not result.ok:
            message = result.text or "The notifier tool failed"
            raise NotifierUnavailable(CATEGORY_TOOL_ERROR, message)
        return result.structured if isinstance(result.structured, dict) else None
