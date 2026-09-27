"""Host-side aggregation of several MCP servers behind one ``McpClient``.

Day 20 connects the agent to two independent servers at once: server A
(``mcp_server``, search and scheduled runs) and server B (``notifier_server``,
watches and Telegram delivery). The hub holds a labelled client per server and
keeps the day 16-19 protocol methods **A-first**, so ``/api/mcp/status`` and
``/api/mcp/tools`` keep reporting the search server. The combined A+B view is
exposed only through :meth:`McpHub.probe_servers`.

Rules that keep the boundary observable and safe:

* ``probe_servers()`` probes every configured server **in parallel** and returns
  one :class:`ServerProbe` per server; each tool carries its ``server`` label.
* a slot with no client (an empty ``MCP_NOTIFIER_URL``) is reported as
  ``not_configured`` **without** opening a connection;
* a tool name advertised by two servers is a controlled configuration error
  (``connected=false``, category ``protocol``) instead of a silent winner;
* ``call_tool`` routes the call to the owner of the name, so the model can never
  make one server invoke the other.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from agent.mcp_adapter import (
    CATEGORY_NOT_CONFIGURED,
    CATEGORY_PROTOCOL,
    McpCallResult,
    McpError,
    McpStatus,
    McpTool,
)


@dataclass(frozen=True)
class ServerProbe:
    """One server's connectivity, identity and tools, tagged with its label.

    ``McpStatus`` is deliberately not extended: the aggregation lives in this
    wrapper, and every tool's ``server`` field equals :attr:`label`.
    """

    label: str
    status: McpStatus
    tools: list[McpTool]


class McpHub:
    """A labelled set of MCP clients presented through the ``McpClient`` protocol."""

    def __init__(self, servers, *, primary: str = "A"):
        """Build the hub from ``(label, client, endpoint)`` entries.

        ``client`` may be ``None`` to model a configured but unavailable slot
        (an empty ``MCP_NOTIFIER_URL``); ``endpoint`` is an optional display
        string such as ``127.0.0.1:8766``.
        """
        entries: list[tuple[str, object, str]] = []
        for entry in servers:
            label = str(entry[0])
            client = entry[1]
            endpoint = str(entry[2]) if len(entry) > 2 and entry[2] else ""
            entries.append((label, client, endpoint))
        self._entries = entries
        self._primary = str(primary)
        self._tool_owners: dict[str, str] = {}

    @property
    def primary_label(self) -> str:
        """The label whose result the A-first protocol methods return."""
        return self._primary

    def labels(self) -> list[str]:
        return [label for label, _client, _endpoint in self._entries]

    def client_for(self, label: str):
        """The client of ``label`` (``None`` when the slot is not configured)."""
        for entry_label, client, _endpoint in self._entries:
            if entry_label == label:
                return client
        return None

    def endpoint_for(self, label: str) -> str:
        """The display endpoint of ``label`` (empty when unknown/not configured)."""
        for entry_label, _client, endpoint in self._entries:
            if entry_label == label:
                return endpoint
        return ""

    async def probe_server(self, label: str) -> ServerProbe:
        """Probe one server; a failure is reported, never raised."""
        label = str(label)
        client = self.client_for(label)
        if client is None:
            return ServerProbe(
                label,
                McpStatus(
                    connected=False,
                    error=McpError(
                        CATEGORY_NOT_CONFIGURED,
                        f"The {label} MCP server is not configured",
                    ),
                ),
                [],
            )
        try:
            status, tools = await client.probe()
        except McpError as exc:
            return ServerProbe(label, McpStatus(connected=False, error=exc), [])
        except Exception:  # noqa: BLE001 - a broken client becomes a status
            return ServerProbe(
                label,
                McpStatus(
                    connected=False,
                    error=McpError(CATEGORY_PROTOCOL, "The MCP probe failed"),
                ),
                [],
            )
        tagged = [replace(tool, server=label) for tool in (tools or [])]
        return ServerProbe(label, status, tagged)

    async def probe_servers(self) -> list[ServerProbe]:
        """Probe every configured server in parallel and reconcile the results."""
        probes = await asyncio.gather(
            *(self.probe_server(label) for label, _client, _endpoint in self._entries)
        )
        return self._reconcile(list(probes))

    def _reconcile(self, probes: list[ServerProbe]) -> list[ServerProbe]:
        """Apply name-collision detection and refresh the tool-owner map."""
        owners: dict[str, set] = {}
        for probe in probes:
            if not probe.status.connected:
                continue
            for tool in probe.tools:
                owners.setdefault(tool.name, set()).add(probe.label)
        colliding = {
            label for labels in owners.values() if len(labels) > 1 for label in labels
        }

        reconciled: list[ServerProbe] = []
        for probe in probes:
            if probe.status.connected and probe.label in colliding:
                reconciled.append(
                    ServerProbe(
                        probe.label,
                        McpStatus(
                            connected=False,
                            error=McpError(
                                CATEGORY_PROTOCOL,
                                "Two MCP servers advertise the same tool name",
                            ),
                        ),
                        [],
                    )
                )
            else:
                reconciled.append(probe)

        self._tool_owners = {
            tool.name: probe.label
            for probe in reconciled
            if probe.status.connected
            for tool in probe.tools
        }
        return reconciled

    # -- A-first protocol methods -----------------------------------------

    async def probe(self) -> tuple[McpStatus, list[McpTool]]:
        """The primary server's status and tools (server A when present)."""
        probe = await self.probe_server(self._primary)
        return probe.status, list(probe.tools)

    async def status(self) -> McpStatus:
        """The primary server's status."""
        probe = await self.probe_server(self._primary)
        return probe.status

    async def list_tools(self) -> list[McpTool]:
        """The primary server's tools, or :class:`McpError` when it is down."""
        probe = await self.probe_server(self._primary)
        if not probe.status.connected:
            raise probe.status.error or McpError(
                CATEGORY_PROTOCOL, "The MCP server is unavailable"
            )
        return list(probe.tools)

    async def call_tool(self, name: str, arguments: dict) -> McpCallResult:
        """Route a tool call to the server that advertised the name."""
        owner = self._tool_owners.get(str(name), self._primary)
        client = self.client_for(owner)
        if client is None:
            # The owner slot is not configured: use any available server so a
            # legacy A-only setup still answers instead of failing early.
            client = next(
                (candidate for _label, candidate, _endpoint in self._entries if candidate),
                None,
            )
        if client is None:
            raise McpError(CATEGORY_NOT_CONFIGURED, "No MCP server is configured")
        return await client.call_tool(name, arguments)
