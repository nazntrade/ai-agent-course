"""Unit tests of the chat HTTP API through the in-process test client."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agent.chats import ChatService, MAX_CHATS
from agent.server import create_app
from agent.settings import resolve_settings
from agent.trace import NullTraceWriter
from storage.db import Database
from storage.tasks import TaskRepository
from tests.support.fakes import FakeMcpClient, ScriptedProvider


class ChatApiTest(unittest.TestCase):
    """CRUD, limits, isolation and the controlled error format."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = resolve_settings(
            env={"AGENT_DB_PATH": str(Path(self._tmp.name) / "chats.sqlite3")},
            dotenv=False,
        )
        self.database = Database(self.settings.db_path)
        self.chats = ChatService(self.database)
        self.tasks = TaskRepository(self.database)

    def _client(self) -> TestClient:
        app = create_app(
            self.settings,
            provider=ScriptedProvider([]),
            mcp_client=FakeMcpClient(),
            trace=NullTraceWriter(),
            chat_service=self.chats,
        )
        return TestClient(app)

    def _create(self, client, title=""):
        return client.post("/api/chats", json={"title": title})

    def test_create_and_list(self):
        with self._client() as client:
            response = self._create(client, "Work")
        self.assertEqual(response.status_code, 201)
        created = response.json()
        self.assertEqual(created["title"], "Work")
        self.assertEqual(created["message_count"], 0)
        self.assertEqual(created["active_tasks_count"], 0)

        with self._client() as client:
            listing = client.get("/api/chats").json()
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["limit"], MAX_CHATS)
        self.assertFalse(listing["limit_reached"])
        self.assertEqual(listing["chats"][0]["id"], created["id"])

    def test_empty_title_gets_the_default(self):
        with self._client() as client:
            created = self._create(client).json()
        self.assertEqual(created["title"], "New chat")

    def test_automatic_title_from_the_first_message(self):
        with self._client() as client:
            chat_id = self._create(client).json()["id"]
        self.chats.append_exchange(chat_id, "Plan my   trip   abroad", "Sure.")
        with self._client() as client:
            listing = client.get("/api/chats").json()
        self.assertEqual(listing["chats"][0]["title"], "Plan my trip abroad")

    def test_sixth_chat_is_rejected_with_the_limit_error(self):
        with self._client() as client:
            for index in range(MAX_CHATS):
                response = self._create(client, f"Chat {index}")
                self.assertEqual(response.status_code, 201)
            sixth = self._create(client, "Too many")
        self.assertEqual(sixth.status_code, 409)
        detail = sixth.json()["detail"]
        self.assertEqual(detail["category"], "chat_limit")
        self.assertIn("limit of 5", detail["message"])

    def test_rename(self):
        with self._client() as client:
            chat_id = self._create(client, "Old").json()["id"]
            response = client.patch(f"/api/chats/{chat_id}", json={"title": "New"})
            empty = client.patch(f"/api/chats/{chat_id}", json={"title": "   "})
            missing = client.patch("/api/chats/missing", json={"title": "X"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "New")
        self.assertEqual(empty.status_code, 422)
        self.assertEqual(empty.json()["detail"]["category"], "invalid_title")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"]["category"], "chat_not_found")

    def test_delete_and_missing(self):
        with self._client() as client:
            chat_id = self._create(client).json()["id"]
            response = client.delete(f"/api/chats/{chat_id}")
            again = client.delete(f"/api/chats/{chat_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"deleted": True, "stopped_tasks": 0})
        self.assertEqual(again.status_code, 404)

    def test_clear_only_clears_the_selected_chat(self):
        with self._client() as client:
            first = self._create(client, "First").json()["id"]
            second = self._create(client, "Second").json()["id"]
        self.chats.append_exchange(first, "one", "answer one")
        self.chats.append_exchange(second, "two", "answer two")

        with self._client() as client:
            cleared = client.post(f"/api/chats/{first}/clear")
            first_messages = client.get(f"/api/chats/{first}/messages").json()
            second_messages = client.get(f"/api/chats/{second}/messages").json()
        self.assertEqual(cleared.status_code, 200)
        self.assertTrue(cleared.json()["cleared"])
        self.assertEqual(cleared.json()["messages_deleted"], 2)
        self.assertEqual(first_messages["count"], 0)
        self.assertEqual(second_messages["count"], 2)

    def test_messages_are_isolated_per_chat(self):
        with self._client() as client:
            first = self._create(client, "First").json()["id"]
            second = self._create(client, "Second").json()["id"]
        self.chats.append_exchange(first, "first user", "first assistant")
        self.chats.append_exchange(second, "second user", "second assistant")

        with self._client() as client:
            payload = client.get(f"/api/chats/{first}/messages").json()
        rendered = [message["content"] for message in payload["messages"]]
        self.assertEqual(rendered, ["first user", "first assistant"])

    def test_unknown_chat_messages_is_404(self):
        with self._client() as client:
            response = client.get("/api/chats/missing/messages")
        self.assertEqual(response.status_code, 404)

    def test_delete_with_active_task_requires_force(self):
        with self._client() as client:
            chat_id = self._create(client, "Busy").json()["id"]
        self.tasks.create_task(chat_id, "news", 60, now=100.0)

        with self._client() as client:
            blocked = client.delete(f"/api/chats/{chat_id}")
            forced = client.delete(f"/api/chats/{chat_id}?force=true")
        self.assertEqual(blocked.status_code, 409)
        detail = blocked.json()["detail"]
        self.assertEqual(detail["category"], "chat_has_active_tasks")
        self.assertEqual(detail["active_tasks"], 1)
        self.assertEqual(forced.status_code, 200)
        self.assertEqual(forced.json()["stopped_tasks"], 1)
        self.assertIsNone(self.tasks.get_task("missing"))
        self.assertEqual(self.tasks.list_tasks(chat_id), [])

    def test_tasks_endpoint_lists_the_chat_tasks(self):
        with self._client() as client:
            chat_id = self._create(client, "Scheduled").json()["id"]
        self.tasks.create_task(chat_id, "python news", 86400, now=100.0)

        with self._client() as client:
            response = client.get(f"/api/chats/{chat_id}/tasks")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["tasks"][0]["query"], "python news")
        self.assertEqual(payload["tasks"][0]["last_run"], None)

    def test_unknown_chat_tasks_is_404(self):
        with self._client() as client:
            response = client.get("/api/chats/missing/tasks")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
