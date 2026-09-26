"""Reusable test doubles for the agent boundary tests.

The doubles implement exactly the protocols the orchestrator depends on
(``agent.mcp_adapter.McpClient`` and ``agent.provider.ModelProvider``), so the
unit tests exercise the real loop without a network, a port or a model.
"""

from __future__ import annotations

from mcp.types.version import LATEST_PROTOCOL_VERSION

from agent.mcp_adapter import McpCallResult, McpError, McpStatus, McpTool


def sample_tools():
    """The nine tools the real server advertises, as value objects."""
    return [
        McpTool(
            name="calculate",
            title=None,
            description="Do a basic arithmetic operation on two numbers.",
            input_schema={
                "type": "object",
                "properties": {
                    "operation": {"type": "string"},
                    "a": {"type": "number"},
                    "b": {"type": "number"},
                },
                "required": ["operation", "a", "b"],
            },
        ),
        McpTool(
            name="get_server_info",
            title=None,
            description="Report the name, version, status and uptime.",
            input_schema={"type": "object", "properties": {}},
        ),
        McpTool(
            name="search_web",
            title=None,
            description="Search the web for current information.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
                "required": ["query"],
            },
        ),
        McpTool(
            name="schedule_search_task",
            title=None,
            description="Schedule a repeating web search for the current chat.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "interval_seconds": {"type": "integer"},
                    "chat_id": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
                "required": ["query", "interval_seconds"],
            },
        ),
        McpTool(
            name="list_search_tasks",
            title=None,
            description="List the scheduled searches of the current chat.",
            input_schema={
                "type": "object",
                "properties": {"chat_id": {"type": "string"}},
            },
        ),
        McpTool(
            name="get_latest_search_run",
            title=None,
            description="Get the latest saved result of a scheduled search.",
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "chat_id": {"type": "string"},
                },
            },
        ),
        McpTool(
            name="stop_search_task",
            title=None,
            description="Stop a scheduled search of the current chat.",
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "chat_id": {"type": "string"},
                },
                "required": ["task_id"],
            },
        ),
        McpTool(
            name="digest_search_results",
            title=None,
            description="Turn a search_web result into a compact digest with sources.",
            input_schema={
                "type": "object",
                "properties": {"search_result": {"type": "object"}},
                "required": ["search_result"],
            },
        ),
        McpTool(
            name="save_report",
            title=None,
            description="Save a digest as a report of the current chat.",
            input_schema={
                "type": "object",
                "properties": {
                    "digest": {"type": "object"},
                    "chat_id": {"type": "string"},
                },
                "required": ["digest"],
            },
        ),
    ]


class FakeMcpClient:
    """A scripted ``McpClient``."""

    def __init__(
        self,
        *,
        connected=True,
        tools=None,
        status_error=None,
        list_error=None,
        call_result=None,
        call_error=None,
        call_results=None,
    ):
        self.connected = connected
        self.tools = sample_tools() if tools is None else tools
        self.status_error = status_error
        self.list_error = list_error
        self.call_result = call_result or McpCallResult(
            ok=True, text='{"result": 391}', structured={"result": 391}
        )
        self.call_error = call_error
        self.call_results = dict(call_results or {})
        self.calls = []
        self.probe_calls = 0
        self.status_calls = 0
        self.list_calls = 0

    def _unavailable(self):
        return self.status_error or McpError(
            "unreachable", "The MCP server is not reachable"
        )

    async def probe(self):
        """One handshake + tools/list, matching the real single-session probe."""
        self.probe_calls += 1
        if not self.connected:
            return McpStatus(connected=False, error=self._unavailable()), []
        if self.list_error is not None:
            # The real client reports a failed tools/list as a disconnected probe.
            return McpStatus(connected=False, error=self.list_error), []
        return (
            McpStatus(
                connected=True,
                protocol_version=LATEST_PROTOCOL_VERSION,
                server_name="day-16-mcp-server",
                server_version="1.0.0",
                tools_count=len(self.tools),
            ),
            list(self.tools),
        )

    async def status(self):
        self.status_calls += 1
        status, _tools = await self.probe()
        return status

    async def list_tools(self):
        self.list_calls += 1
        if self.list_error is not None:
            raise self.list_error
        if not self.connected:
            raise self._unavailable()
        return list(self.tools)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name in self.call_results:
            return self.call_results[name]
        if self.call_error is not None:
            raise self.call_error
        return self.call_result


class ScriptedProvider:
    """A ``ModelProvider`` that replays scripted turns of model events."""

    name = "scripted"
    model = "scripted-model"
    configured = True

    def __init__(self, turns):
        self.turns = [list(turn) for turn in turns]
        self.requests = []
        self._index = 0

    async def stream(self, messages, tools, *, timeout_s=None):
        self.requests.append({"messages": list(messages), "tools": list(tools)})
        turn = self.turns[min(self._index, len(self.turns) - 1)]
        self._index += 1
        for event in turn:
            yield event


class BlockingProvider:
    """A provider that waits on an ``asyncio.Event`` before answering."""

    name = "blocking"
    model = "blocking-model"
    configured = True

    def __init__(self, gate):
        self.gate = gate
        self.calls = 0

    async def stream(self, messages, tools, *, timeout_s=None):
        self.calls += 1
        await self.gate.wait()
        from agent.provider import Finished, TextDelta

        yield TextDelta("done")
        yield Finished(finish_reason="stop")
