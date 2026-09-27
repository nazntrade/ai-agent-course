"""Unit tests of the day-20 MCP hub (no network, no real server).

The hub aggregates server A (search) and server B (notifier) behind the day 16-19
``McpClient`` protocol. These tests pin the aggregation contract: parallel
probing, per-server labels, the A-first protocol methods, controlled name
collisions and an unconfigured B slot.
"""

from __future__ import annotations

import asyncio
import unittest

from agent.mcp_adapter import McpCallResult, McpError, McpStatus, McpTool
from agent.mcp_hub import McpHub, ServerProbe


def _tool(name: str, *, server: str | None = None) -> McpTool:
    return McpTool(
        name=name,
        title=None,
        description=f"tool {name}",
        input_schema={"type": "object", "properties": {}},
        server=server,
    )


class _ProbeClient:
    """A probing ``McpClient`` with a per-probe hook and call recording."""

    def __init__(self, tools, *, connected=True, error=None, on_probe=None):
        self.tools = list(tools)
        self.connected = connected
        self.error = error
        self.on_probe = on_probe
        self.calls: list = []
        self.probe_calls = 0

    async def probe(self):
        self.probe_calls += 1
        if self.on_probe is not None:
            await self.on_probe()
        if not self.connected:
            return McpStatus(connected=False, error=self.error or McpError("unreachable", "down")), []
        return (
            McpStatus(
                connected=True,
                protocol_version="2026-07-28",
                server_name="fake",
                server_version="1.0.0",
                tools_count=len(self.tools),
            ),
            list(self.tools),
        )

    async def status(self):
        status, _tools = await self.probe()
        return status

    async def list_tools(self):
        if not self.connected:
            raise self.error or McpError("unreachable", "down")
        return list(self.tools)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return McpCallResult(ok=True, text="ok", structured={"tool": name})


class ProbeServersTest(unittest.IsolatedAsyncioTestCase):
    """``probe_servers`` is parallel, labelled and A-first aware."""

    async def test_servers_are_probed_in_parallel(self):
        state = {"active": 0, "max": 0}

        async def hook():
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
            await asyncio.sleep(0.05)
            state["active"] -= 1

        a = _ProbeClient([_tool("a_only")], on_probe=hook)
        b = _ProbeClient([_tool("b_only")], on_probe=hook)
        hub = McpHub([("A", a), ("B", b)])
        probes = await hub.probe_servers()
        self.assertEqual([probe.label for probe in probes], ["A", "B"])
        self.assertEqual(state["max"], 2, "the two servers must be probed in parallel")

    async def test_tools_carry_their_server_label(self):
        a = _ProbeClient([_tool("a_only")])
        b = _ProbeClient([_tool("b_only")])
        hub = McpHub([("A", a), ("B", b)])
        probes = await hub.probe_servers()
        by_label = {probe.label: probe for probe in probes}
        self.assertIsInstance(by_label["A"], ServerProbe)
        self.assertEqual(by_label["A"].tools[0].server, "A")
        self.assertEqual(by_label["B"].tools[0].server, "B")

    async def test_an_unavailable_b_keeps_a_available(self):
        a = _ProbeClient([_tool("a_only")])
        b = _ProbeClient([], connected=False, error=McpError("unreachable", "down"))
        hub = McpHub([("A", a), ("B", b)])
        probes = await hub.probe_servers()
        by_label = {probe.label: probe for probe in probes}
        self.assertTrue(by_label["A"].status.connected)
        self.assertEqual(by_label["A"].tools[0].name, "a_only")
        self.assertFalse(by_label["B"].status.connected)
        self.assertEqual(by_label["B"].status.error.category, "unreachable")
        self.assertEqual(by_label["B"].tools, [])

    async def test_empty_url_is_not_configured_without_a_probe(self):
        a = _ProbeClient([_tool("a_only")])
        hub = McpHub([("A", a), ("B", None)])
        probe = await hub.probe_server("B")
        self.assertFalse(probe.status.connected)
        self.assertEqual(probe.status.error.category, "not_configured")
        self.assertEqual(probe.tools, [])
        probes = await hub.probe_servers()
        labels = {item.label: item for item in probes}
        self.assertEqual(labels["B"].status.error.category, "not_configured")

    async def test_name_collision_is_a_controlled_protocol_error(self):
        a = _ProbeClient([_tool("shared")])
        b = _ProbeClient([_tool("shared")])
        hub = McpHub([("A", a), ("B", b)])
        probes = await hub.probe_servers()
        for probe in probes:
            self.assertFalse(probe.status.connected)
            self.assertEqual(probe.status.error.category, "protocol")
            self.assertEqual(probe.tools, [])


class AFirstProtocolTest(unittest.IsolatedAsyncioTestCase):
    """The protocol methods keep the day 16-19 server-A semantics."""

    def _hub(self):
        self.a = _ProbeClient([_tool("a_tool")])
        self.b = _ProbeClient([_tool("b_tool")])
        return McpHub([("A", self.a), ("B", self.b)], primary="A")

    async def test_probe_and_list_tools_return_a_only(self):
        hub = self._hub()
        status, tools = await hub.probe()
        self.assertTrue(status.connected)
        self.assertEqual([tool.name for tool in tools], ["a_tool"])
        self.assertEqual((await hub.status()).connected, True)
        self.assertEqual([tool.name for tool in await hub.list_tools()], ["a_tool"])
        self.assertEqual(self.a.probe_calls, 3)
        self.assertEqual(self.b.probe_calls, 0)

    async def test_call_tool_routes_by_name_to_the_owner(self):
        hub = self._hub()
        await hub.probe_servers()
        await hub.call_tool("a_tool", {"x": 1})
        await hub.call_tool("b_tool", {"y": 2})
        self.assertEqual(self.a.calls, [("a_tool", {"x": 1})])
        self.assertEqual(self.b.calls, [("b_tool", {"y": 2})])


class FakeClientCompatibilityTest(unittest.IsolatedAsyncioTestCase):
    """Existing fake clients keep constructing tools without a ``server``."""

    async def test_tool_without_server_defaults_to_none(self):
        self.assertIsNone(_tool("plain").server)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
