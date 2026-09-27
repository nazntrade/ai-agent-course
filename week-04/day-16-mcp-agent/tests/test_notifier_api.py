"""Unit tests of the day-20 notifier HTTP API (no network, no real server).

The app is built with an in-process :class:`McpHub` over two fake servers A and
B, so the real routes, response models and error mapping run while the MCP
boundary and the model stay deterministic.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agent.chats import ChatService
from agent.mcp_adapter import McpError
from agent.mcp_hub import McpHub
from agent.provider import Finished, TextDelta
from agent.server import create_app
from agent.sessions import SessionRegistry
from agent.settings import resolve_settings
from agent.trace import NullTraceWriter
from storage.db import Database
from tests.support.fakes import (
    FakeMcpClient,
    ScriptedProvider,
    notifier_call_results,
    notifier_tools,
    sample_tools,
)


def _answer_turn(text="ok"):
    return [TextDelta(text), Finished(finish_reason="stop")]


class NotifierApiTest(unittest.TestCase):
    """``/api/mcp/servers`` and ``/api/chats/{id}/watches`` contracts."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = resolve_settings(
            env={
                "AGENT_DB_PATH": str(Path(self._tmp.name) / "api.sqlite3"),
                "MCP_NOTIFIER_URL": "http://127.0.0.1:8766/mcp",
            },
            dotenv=False,
        )
        self.chats = ChatService(Database(self.settings.db_path))
        self.chat_id = self.chats.create_chat("Test chat")["id"]
        self.a = FakeMcpClient()
        self.b = FakeMcpClient(tools=notifier_tools())
        self.hub = McpHub(
            [("A", self.a, "127.0.0.1:8765"), ("B", self.b, "127.0.0.1:8766")],
            primary="A",
        )

    def _app(self):
        return create_app(
            self.settings,
            provider=ScriptedProvider([_answer_turn()]),
            mcp_client=self.hub,
            sessions=SessionRegistry(),
            trace=NullTraceWriter(),
            chat_service=self.chats,
        )

    def test_servers_lists_both_servers(self):
        with TestClient(self._app()) as client:
            response = client.get("/api/mcp/servers")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        labels = [server["label"] for server in payload["servers"]]
        self.assertEqual(labels, ["A", "B"])
        by_label = {server["label"]: server for server in payload["servers"]}
        self.assertTrue(by_label["A"]["connected"])
        self.assertEqual(by_label["A"]["tools_count"], len(sample_tools()))
        self.assertTrue(by_label["B"]["connected"])
        self.assertEqual(by_label["B"]["tools_count"], len(notifier_tools()))
        self.assertEqual(by_label["A"]["endpoint"], "127.0.0.1:8765")

    def test_servers_endpoint_probes_each_server_exactly_once(self):
        with TestClient(self._app()) as client:
            response = client.get("/api/mcp/servers")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.a.probe_calls, 1)
        self.assertEqual(self.b.probe_calls, 1)

    def test_servers_reports_b_down_without_5xx(self):
        self.b.connected = False
        self.b.status_error = McpError("unreachable", "The notifier is not reachable")
        with TestClient(self._app()) as client:
            response = client.get("/api/mcp/servers")
        self.assertEqual(response.status_code, 200)
        by_label = {s["label"]: s for s in response.json()["servers"]}
        self.assertTrue(by_label["A"]["connected"])
        self.assertFalse(by_label["B"]["connected"])
        self.assertEqual(by_label["B"]["error"]["category"], "unreachable")

    def test_status_and_tools_keep_server_a_semantics(self):
        with TestClient(self._app()) as client:
            status = client.get("/api/mcp/status")
            tools = client.get("/api/mcp/tools")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["tools_count"], len(sample_tools()))
        self.assertEqual(tools.status_code, 200)
        self.assertEqual(tools.json()["count"], len(sample_tools()))

    def test_watches_unknown_chat_is_404_before_contacting_b(self):
        with TestClient(self._app()) as client:
            response = client.get("/api/chats/does-not-exist/watches")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["category"], "chat_not_found")
        self.assertEqual(self.b.calls, [])
        self.assertEqual(self.b.probe_calls, 0)

    def test_watches_reports_available_false_when_b_is_down(self):
        self.b.call_error = McpError("unreachable", "The notifier is not reachable")
        with TestClient(self._app()) as client:
            response = client.get(f"/api/chats/{self.chat_id}/watches")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["available"])
        self.assertEqual(payload["error"]["category"], "unreachable")
        self.assertEqual(payload["watches"], [])
        self.assertEqual(payload["deliveries"], [])

    def test_watches_returns_watches_and_deliveries(self):
        self.b.call_results = notifier_call_results(
            watches={"count": 1, "watches": [{"watch_id": "w1", "status": "active"}]},
            deliveries={
                "count": 1,
                "deliveries": [
                    {"delivery_id": "d1", "watch_id": "w1", "status": "sent"}
                ],
            },
        )
        with TestClient(self._app()) as client:
            response = client.get(f"/api/chats/{self.chat_id}/watches")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["available"])
        self.assertEqual(payload["watches"][0]["watch_id"], "w1")
        self.assertEqual(payload["deliveries"][0]["status"], "sent")


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
