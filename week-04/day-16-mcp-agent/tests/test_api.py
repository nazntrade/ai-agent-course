"""Unit tests of the FastAPI backend through the in-process test client.

The MCP server and the model are replaced by the shared doubles, so these tests
need no port and no network. The HTTP contract, the SSE framing and the
controlled error behaviour are still the real application code.
"""

from __future__ import annotations

import json
import unittest

from fastapi.testclient import TestClient
from mcp.types.version import LATEST_PROTOCOL_VERSION

from agent.mcp_adapter import McpError
from agent.provider import Finished, TextDelta, ToolCallDelta
from agent.server import create_app
from agent.sessions import SessionStore
from agent.settings import resolve_settings
from agent.trace import NullTraceWriter
from tests.support.fakes import FakeMcpClient, ScriptedProvider, sample_tools


def _parse_sse(text: str) -> list:
    events = []
    name = None
    data_lines = []
    for line in text.splitlines():
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].strip())
        elif line == "":
            if data_lines:
                events.append((name, json.loads("\n".join(data_lines))))
            name = None
            data_lines = []
    if data_lines:
        events.append((name, json.loads("\n".join(data_lines))))
    return events


def _build_app(*, provider=None, mcp_client=None):
    settings = resolve_settings(env={}, dotenv=False)
    return create_app(
        settings,
        provider=provider or ScriptedProvider([_answer_turn()]),
        mcp_client=mcp_client or FakeMcpClient(),
        sessions=SessionStore(),
        trace=NullTraceWriter(),
    )


def _answer_turn(text="Hello from the model."):
    return [TextDelta(text), Finished(finish_reason="stop")]


def _tool_turn():
    return [
        ToolCallDelta(
            index=0,
            id="call_1",
            name="calculate",
            arguments='{"operation": "multiply", "a": 23, "b": 17}',
        ),
        Finished(finish_reason="tool_calls"),
    ]


class HealthTest(unittest.TestCase):
    """The health endpoint reports the service identity."""

    def test_health(self):
        with TestClient(_build_app()) as client:
            response = client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["service"], "day-16-mcp-agent")
        self.assertTrue(payload["version"])


class McpStatusTest(unittest.TestCase):
    """MCP status and tools reflect the real client."""

    def test_status_connected(self):
        with TestClient(_build_app()) as client:
            response = client.get("/api/mcp/status")
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["connected"])
        self.assertEqual(payload["protocol_version"], LATEST_PROTOCOL_VERSION)
        self.assertEqual(payload["tools_count"], 2)
        self.assertEqual(payload["server"]["name"], "day-16-mcp-server")
        self.assertIsNone(payload["error"])
        self.assertIn("checked_at", payload)

    def test_status_disconnected(self):
        app = _build_app(
            mcp_client=FakeMcpClient(
                connected=False,
                status_error=McpError("unreachable", "The MCP server is not reachable"),
            )
        )
        with TestClient(app) as client:
            response = client.get("/api/mcp/status")
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(payload["connected"])
        self.assertEqual(payload["tools_count"], 0)
        self.assertEqual(payload["error"]["category"], "unreachable")

    def test_tools_match_the_mcp_server(self):
        client_tools = sample_tools()
        app = _build_app(mcp_client=FakeMcpClient(tools=client_tools))
        with TestClient(app) as client:
            response = client.get("/api/mcp/tools")
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["connected"])
        self.assertEqual(payload["count"], len(client_tools))
        self.assertEqual(
            [tool["name"] for tool in payload["tools"]],
            [tool.name for tool in client_tools],
        )
        self.assertTrue(payload["tools"][0]["description"])
        self.assertIn("input_schema", payload["tools"][0])

    def test_tools_are_an_empty_list_when_mcp_is_down(self):
        app = _build_app(
            mcp_client=FakeMcpClient(
                connected=False, status_error=McpError("timeout", "The MCP server timed out")
            )
        )
        with TestClient(app) as client:
            response = client.get("/api/mcp/tools")
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(payload["connected"])
        self.assertEqual(payload["tools"], [])
        self.assertEqual(payload["error"]["category"], "timeout")

    def test_tools_endpoint_opens_exactly_one_probe(self):
        mcp = FakeMcpClient()
        app = _build_app(mcp_client=mcp)
        with TestClient(app) as client:
            response = client.get("/api/mcp/tools")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["connected"])
        self.assertEqual(mcp.probe_calls, 1)
        self.assertEqual(mcp.list_calls, 0)


class StaticAssetsTest(unittest.TestCase):
    """The static UI is served with cache revalidation."""

    def test_stylesheet_disables_caching(self):
        with TestClient(_build_app()) as client:
            response = client.get("/styles.css")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("cache-control"), "no-cache")

    def test_index_requests_a_versioned_stylesheet(self):
        with TestClient(_build_app()) as client:
            response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/styles.css?v=", response.text)


class ChatStreamTest(unittest.TestCase):
    """The chat stream follows the documented SSE contract."""

    def test_full_tool_chain(self):
        provider = ScriptedProvider([_tool_turn(), _answer_turn("The result is 391.")])
        mcp = FakeMcpClient()
        app = _build_app(provider=provider, mcp_client=mcp)
        with TestClient(app) as client:
            response = client.post(
                "/api/chat/stream",
                json={"session_id": "s1", "message": "What is 23 multiplied by 17?"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        events = _parse_sse(response.text)
        names = [name for name, _ in events]
        self.assertIn("status", names)
        self.assertIn("delta", names)
        self.assertEqual(names[-1], "done")
        self.assertLess(names.index("tool_call"), names.index("tool_result"))
        self.assertLess(names.index("tool_result"), names.index("delta"))
        self.assertEqual(
            mcp.calls,
            [("calculate", {"operation": "multiply", "a": 23, "b": 17})],
        )
        text = "".join(
            data["text"] for name, data in events if name == "delta"
        )
        self.assertIn("391", text)

    def test_status_events_carry_the_request_id_of_done(self):
        provider = ScriptedProvider([_tool_turn(), _answer_turn("The result is 391.")])
        app = _build_app(provider=provider)
        with TestClient(app) as client:
            response = client.post(
                "/api/chat/stream",
                json={"session_id": "s1", "message": "What is 23 multiplied by 17?"},
            )
        events = _parse_sse(response.text)
        statuses = [data for name, data in events if name == "status"]
        done = [data for name, data in events if name == "done"]
        self.assertTrue(statuses)
        self.assertEqual(len(done), 1)
        request_id = done[0]["request_id"]
        self.assertTrue(request_id)
        for status in statuses:
            self.assertIn("stage", status)
            self.assertEqual(status["request_id"], request_id)

    def test_second_request_in_the_same_session_reuses_history(self):
        provider = ScriptedProvider([_answer_turn("First."), _answer_turn("Second.")])
        app = _build_app(provider=provider)
        with TestClient(app) as client:
            client.post(
                "/api/chat/stream", json={"session_id": "same", "message": "one"}
            )
            client.post(
                "/api/chat/stream", json={"session_id": "same", "message": "two"}
            )
        second_call = provider.requests[1]["messages"]
        roles = [message["role"] for message in second_call]
        self.assertEqual(roles.count("user"), 2)
        self.assertIn("First.", json.dumps(second_call))

    def test_empty_message_is_rejected(self):
        with TestClient(_build_app()) as client:
            response = client.post(
                "/api/chat/stream", json={"session_id": "s1", "message": ""}
            )
        self.assertEqual(response.status_code, 422)

    def test_missing_session_id_is_rejected(self):
        with TestClient(_build_app()) as client:
            response = client.post("/api/chat/stream", json={"message": "hi"})
        self.assertEqual(response.status_code, 422)

    def test_mcp_unavailable_error_event(self):
        app = _build_app(
            mcp_client=FakeMcpClient(
                connected=False, status_error=McpError("unreachable", "The MCP server is not reachable")
            )
        )
        with TestClient(app) as client:
            response = client.post(
                "/api/chat/stream", json={"session_id": "s1", "message": "hi"}
            )
        events = _parse_sse(response.text)
        names = [name for name, _ in events]
        self.assertEqual(names[-1], "error")
        self.assertNotIn("done", names)
        payload = events[-1][1]
        self.assertEqual(payload["category"], "mcp_unavailable")

    def test_model_unavailable_error_event(self):
        class FailingProvider:
            name = "failing"
            model = "failing"
            configured = True

            async def stream(self, messages, tools, *, timeout_s=None):
                from agent.provider import ModelError

                raise ModelError(
                    "The model endpoint is not reachable", "model_unreachable"
                )
                yield  # pragma: no cover

        app = _build_app(provider=FailingProvider())
        with TestClient(app) as client:
            response = client.post(
                "/api/chat/stream", json={"session_id": "s1", "message": "hi"}
            )
        events = _parse_sse(response.text)
        payload = events[-1][1]
        self.assertEqual(events[-1][0], "error")
        self.assertEqual(payload["category"], "model_unreachable")
        self.assertNotIn("done", [name for name, _ in events])

    def test_model_not_configured_error_event_before_mcp(self):
        class UnconfiguredProvider:
            name = "unconfigured"
            model = "qwen3.8-27b-local"
            configured = False

            async def stream(self, messages, tools, *, timeout_s=None):
                raise AssertionError("the model must not be called")
                yield  # pragma: no cover

        mcp = FakeMcpClient()
        app = _build_app(provider=UnconfiguredProvider(), mcp_client=mcp)
        with TestClient(app) as client:
            response = client.post(
                "/api/chat/stream", json={"session_id": "s1", "message": "hi"}
            )
        events = _parse_sse(response.text)
        names = [name for name, _ in events]
        self.assertEqual(names[-1], "error")
        self.assertNotIn("done", names)
        self.assertEqual(events[-1][1]["category"], "model_not_configured")
        self.assertEqual(mcp.probe_calls, 0)

    def test_unicode_message_round_trip(self):
        provider = ScriptedProvider([_answer_turn("Привет! 2 + 2 = 4")])
        app = _build_app(provider=provider)
        with TestClient(app) as client:
            response = client.post(
                "/api/chat/stream",
                json={"session_id": "s1", "message": "Привет, посчитай 2 + 2"},
            )
        events = _parse_sse(response.text)
        text = "".join(data["text"] for name, data in events if name == "delta")
        self.assertIn("Привет", text)
        self.assertIn("Привет, посчитай 2 + 2", json.dumps(provider.requests[0]["messages"], ensure_ascii=False))


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
