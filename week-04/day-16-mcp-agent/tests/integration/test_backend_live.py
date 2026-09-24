"""Integration tests of the backend running as a real process.

The healthy backend is started by ``harness/live_mcp.py`` and talks to the real
MCP server and to a deterministic model stub. The degraded backends are started
by this module itself (and stopped in teardown) so both controlled error paths —
MCP down and model down — are exercised over real HTTP.

Skipped unless ``RUN_LIVE_MCP=1``.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path

import httpx

from harness.processes import (
    ManagedProcess,
    python_module,
    sanitized_env,
    wait_http,
)
from harness.qa_bridge import PROJECT_DIR
from agent.mcp_adapter import inspect_tools

RUN_LIVE = os.environ.get("RUN_LIVE_MCP") == "1"
BACKEND_URL = os.environ.get("BACKEND_TEST_URL", "")
MCP_URL = os.environ.get("MCP_TEST_URL", "")
MODEL_ID = os.environ.get("STUB_MODEL_ID", "stub-model")
UNREACHABLE_URL = os.environ.get("MCP_UNREACHABLE_URL", "")
DEAD_MODEL_URL = "http://127.0.0.1:1/v1"

CHAT_TIMEOUT = 60.0


def _parse_sse(text: str) -> list:
    events = []
    name = None
    data_lines: list = []
    for line in text.splitlines():
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].strip())
        elif line == "":
            if data_lines:
                try:
                    events.append((name, json.loads("\n".join(data_lines))))
                except ValueError:
                    events.append((name, {"raw": "\n".join(data_lines)}))
            name = None
            data_lines = []
    if data_lines:
        try:
            events.append((name, json.loads("\n".join(data_lines))))
        except ValueError:
            events.append((name, {"raw": "\n".join(data_lines)}))
    return events


def _create_chat(url: str, title: str = "") -> str:
    response = httpx.post(f"{url}/api/chats", json={"title": title}, timeout=20.0)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _post_chat(url: str, chat_id: str, message: str, timeout: float = CHAT_TIMEOUT):
    return httpx.post(
        f"{url}/api/chat/stream",
        json={"chat_id": chat_id, "message": message},
        timeout=httpx.Timeout(connect=10.0, read=timeout, write=10.0, pool=10.0),
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(RUN_LIVE and BACKEND_URL, "set RUN_LIVE_MCP=1 to run this test")
class HealthyBackendTest(unittest.TestCase):
    """Health, MCP status, tools, chats and the streamed chat of the real backend."""

    def test_health(self):
        response = httpx.get(f"{BACKEND_URL}/api/health", timeout=10.0)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["service"], "day-16-mcp-agent")

    def test_openapi_is_served(self):
        response = httpx.get(f"{BACKEND_URL}/openapi.json", timeout=10.0)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["openapi"].split(".")[0], "3")

    def test_static_ui_is_served(self):
        response = httpx.get(f"{BACKEND_URL}/", timeout=10.0)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Day 16 MCP Agent", response.text)
        self.assertNotIn("Authorization", response.text)

    def test_mcp_status_is_connected(self):
        payload = httpx.get(f"{BACKEND_URL}/api/mcp/status", timeout=20.0).json()
        self.assertTrue(payload["connected"])
        self.assertTrue(payload["protocol_version"])
        self.assertEqual(payload["tools_count"], 7)
        self.assertEqual(payload["server"]["name"], "day-16-mcp-server")

    def test_tools_match_the_real_mcp_server(self):
        status, tools = asyncio.run(inspect_tools(MCP_URL, connect_timeout_s=10.0))
        self.assertTrue(status.connected)
        payload = httpx.get(f"{BACKEND_URL}/api/mcp/tools", timeout=20.0).json()
        self.assertTrue(payload["connected"])
        self.assertEqual(
            sorted(tool["name"] for tool in payload["tools"]),
            sorted(tool.name for tool in tools),
        )
        self.assertEqual(len(tools), 7)

    def test_chats_crud_and_history(self):
        created = httpx.post(
            f"{BACKEND_URL}/api/chats", json={"title": "Integration"}, timeout=20.0
        )
        self.assertEqual(created.status_code, 201)
        chat_id = created.json()["id"]

        listed = httpx.get(f"{BACKEND_URL}/api/chats", timeout=20.0).json()
        self.assertIn(chat_id, [chat["id"] for chat in listed["chats"]])
        self.assertEqual(listed["limit"], 5)

        renamed = httpx.patch(
            f"{BACKEND_URL}/api/chats/{chat_id}",
            json={"title": "Renamed"},
            timeout=20.0,
        )
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["title"], "Renamed")

        response = _post_chat(BACKEND_URL, chat_id, "What is 23 multiplied by 17?")
        self.assertEqual(response.status_code, 200)
        history = httpx.get(
            f"{BACKEND_URL}/api/chats/{chat_id}/messages", timeout=20.0
        ).json()
        self.assertGreaterEqual(history["count"], 1)

        cleared = httpx.post(
            f"{BACKEND_URL}/api/chats/{chat_id}/clear", timeout=20.0
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertGreaterEqual(cleared.json()["messages_deleted"], 1)

        deleted = httpx.delete(f"{BACKEND_URL}/api/chats/{chat_id}", timeout=20.0)
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json()["deleted"])

    def test_chat_stream_calls_the_mcp_tool(self):
        chat_id = _create_chat(BACKEND_URL, "Tool chat")
        response = _post_chat(BACKEND_URL, chat_id, "What is 23 multiplied by 17?")
        self.assertEqual(response.status_code, 200)
        events = _parse_sse(response.text)
        names = [name for name, _ in events]
        self.assertNotIn("error", names)
        self.assertEqual(names[-1], "done")
        self.assertLess(names.index("tool_call"), names.index("tool_result"))
        self.assertLess(names.index("tool_result"), names.index("delta"))
        tool_call = next(data for name, data in events if name == "tool_call")
        self.assertEqual(tool_call["tool"], "calculate")
        tool_result = next(data for name, data in events if name == "tool_result")
        self.assertTrue(tool_result["ok"])
        text = "".join(data["text"] for name, data in events if name == "delta")
        self.assertIn("391", text)

    def test_chat_without_a_tool_request(self):
        chat_id = _create_chat(BACKEND_URL, "Plain chat")
        response = _post_chat(BACKEND_URL, chat_id, "Hello there")
        events = _parse_sse(response.text)
        names = [name for name, _ in events]
        self.assertNotIn("tool_call", names)
        self.assertEqual(names[-1], "done")

    def test_second_request_in_the_same_chat(self):
        chat_id = _create_chat(BACKEND_URL, "Repeat chat")
        first = _post_chat(BACKEND_URL, chat_id, "What is 2 multiplied by 3?")
        second = _post_chat(BACKEND_URL, chat_id, "What is 4 multiplied by 5?")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(_parse_sse(second.text)[-1][0], "done")

    def test_unknown_chat_is_rejected_with_404(self):
        response = _post_chat(BACKEND_URL, "does-not-exist", "Hello there")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["category"], "chat_not_found")

    def test_empty_message_is_rejected(self):
        chat_id = _create_chat(BACKEND_URL, "Empty chat")
        response = _post_chat(BACKEND_URL, chat_id, "")
        self.assertEqual(response.status_code, 422)

    def test_backend_still_answers_after_the_requests(self):
        response = httpx.get(f"{BACKEND_URL}/api/health", timeout=10.0)
        self.assertEqual(response.status_code, 200)


@unittest.skipUnless(RUN_LIVE and BACKEND_URL, "set RUN_LIVE_MCP=1 to run this test")
class DegradedBackendTest(unittest.TestCase):
    """Both dependency failures are controlled and never a fake success."""

    def _start_backend(
        self, *, mcp_url: str, model_url: str, label: str, api_key: str | None = "local-e2e"
    ):
        port = _free_port()
        run_dir = Path(tempfile.mkdtemp(prefix="day16-it-"))
        process = ManagedProcess(
            name=label,
            args=python_module("agent"),
            cwd=PROJECT_DIR,
            env=sanitized_env(
                {
                    "BACKEND_HOST": "127.0.0.1",
                    "BACKEND_PORT": str(port),
                    "MCP_SERVER_URL": mcp_url,
                    "AGENT_MODEL_BASE_URL": model_url,
                    "AGENT_MODEL_NAME": MODEL_ID,
                    "AGENT_MODEL_API_KEY_ENV": "LOCAL_LLM_API_KEY",
                    "LOCAL_LLM_API_KEY": api_key,
                    "AGENT_MODEL_TIMEOUT_SECONDS": "10",
                    "AGENT_TRACE_PATH": str(run_dir / "trace.jsonl"),
                    # Keep the real data/ directory out of the test run.
                    "AGENT_DB_PATH": str(run_dir / "day18.sqlite3"),
                }
            ),
            log_path=run_dir / "backend.log",
        ).start()
        self.addCleanup(process.stop)
        url = f"http://127.0.0.1:{port}"
        if not wait_http(f"{url}/api/health", 45.0, process):
            self.fail(f"the degraded backend {label} did not start: {process.tail_log(2000)}")
        return url

    def test_mcp_unavailable_is_reported_before_the_model(self):
        if not UNREACHABLE_URL:
            self.skipTest("MCP_UNREACHABLE_URL is not configured")
        url = self._start_backend(
            mcp_url=UNREACHABLE_URL, model_url=DEAD_MODEL_URL, label="backend-mcp-down"
        )
        chat_id = _create_chat(url, "Degraded")
        response = _post_chat(url, chat_id, "What is 2 multiplied by 3?")
        events = _parse_sse(response.text)
        names = [name for name, _ in events]
        self.assertEqual(names[-1], "error")
        payload = events[-1][1]
        self.assertEqual(payload["category"], "mcp_unavailable")
        self.assertNotIn("done", names)

    def test_model_unavailable_is_reported(self):
        url = self._start_backend(
            mcp_url=MCP_URL, model_url=DEAD_MODEL_URL, label="backend-model-down"
        )
        chat_id = _create_chat(url, "Degraded")
        response = _post_chat(url, chat_id, "Hello there")
        events = _parse_sse(response.text)
        names = [name for name, _ in events]
        self.assertEqual(names[-1], "error")
        payload = events[-1][1]
        self.assertEqual(payload["category"], "model_unreachable")
        self.assertNotIn("done", names)

    def test_model_not_configured_is_reported_before_mcp(self):
        # An explicit empty value stops ``load_dotenv(override=False)`` from
        # filling the key in from a local ``.env``.
        url = self._start_backend(
            mcp_url=UNREACHABLE_URL or MCP_URL,
            model_url=DEAD_MODEL_URL,
            label="backend-no-key",
            api_key="",
        )
        chat_id = _create_chat(url, "Degraded")
        response = _post_chat(url, chat_id, "Hello there")
        events = _parse_sse(response.text)
        names = [name for name, _ in events]
        self.assertEqual(names[-1], "error")
        payload = events[-1][1]
        self.assertEqual(payload["category"], "model_not_configured")
        self.assertNotIn("done", names)
        self.assertNotIn("mcp_unavailable", [data.get("category") for name, data in events])

    def test_mcp_status_endpoint_stays_reachable_when_mcp_is_down(self):
        if not UNREACHABLE_URL:
            self.skipTest("MCP_UNREACHABLE_URL is not configured")
        url = self._start_backend(
            mcp_url=UNREACHABLE_URL, model_url=DEAD_MODEL_URL, label="backend-mcp-status"
        )
        response = httpx.get(f"{url}/api/mcp/status", timeout=20.0)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["connected"])
        self.assertEqual(payload["tools_count"], 0)
        self.assertIsNotNone(payload["error"])


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
