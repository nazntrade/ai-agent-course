"""Unit tests of the saved-reports HTTP API (D19-07, D19-12…D19-14)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agent.chats import ChatService
from agent.server import create_app
from agent.settings import resolve_settings
from agent.trace import NullTraceWriter
from mcp_server.reports import ReportService, build_digest
from storage.chats import ChatRepository
from storage.db import Database
from tests.support.fakes import FakeMcpClient, ScriptedProvider

SEARCH_RESULT = {
    "query": "Kotlin news",
    "count": 2,
    "results": [
        {
            "title": "Kotlin 1.9 released",
            "url": "https://kotlin.example.test/news/1",
            "description": "A new release.",
        },
        {
            "title": "Kotlin Multiplatform",
            "url": "https://kotlin.example.test/news/2",
            "description": "Cross-platform.",
        },
    ],
    "more_results_available": False,
    "note": "Snippets only; the pages were not opened.",
}


class ReportsApiTest(unittest.TestCase):
    """The list/detail endpoints stay scoped to one chat."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = resolve_settings(
            env={"AGENT_DB_PATH": str(Path(self._tmp.name) / "reports.sqlite3")},
            dotenv=False,
        )
        self.database = Database(self.settings.db_path)
        self.chats = ChatService(self.database)
        self.reports = ReportService(self.database)

    def _client(self) -> TestClient:
        app = create_app(
            self.settings,
            provider=ScriptedProvider([]),
            mcp_client=FakeMcpClient(),
            trace=NullTraceWriter(),
            chat_service=self.chats,
        )
        return TestClient(app)

    def _chat(self, title="Reports"):
        return ChatRepository(self.database).create_chat(title)["id"]

    def _save(self, chat_id, result=None):
        return self.reports.save(chat_id, build_digest(result or SEARCH_RESULT))

    def test_empty_chat_has_no_reports(self):
        chat_id = self._chat()
        with self._client() as client:
            response = client.get(f"/api/chats/{chat_id}/reports")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"chat_id": chat_id, "reports": [], "count": 0})

    def test_list_returns_newest_first(self):
        chat_id = self._chat()
        first = self._save(chat_id)
        second = self._save(chat_id)
        with self._client() as client:
            payload = client.get(f"/api/chats/{chat_id}/reports").json()
        self.assertEqual(payload["count"], 2)
        self.assertEqual(
            [report["report_id"] for report in payload["reports"]],
            [second["report_id"], first["report_id"]],
        )
        item = payload["reports"][0]
        self.assertEqual(item["topic"], "Kotlin news")
        self.assertEqual(item["source_count"], 2)
        self.assertIsInstance(item["created_at"], float)
        self.assertNotIn("summary", item)

    def test_detail_returns_summary_and_sources(self):
        chat_id = self._chat()
        saved = self._save(chat_id)
        with self._client() as client:
            response = client.get(
                f"/api/chats/{chat_id}/reports/{saved['report_id']}"
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["report_id"], saved["report_id"])
        self.assertEqual(payload["chat_id"], chat_id)
        self.assertIn("Kotlin 1.9 released", payload["summary"])
        self.assertEqual(len(payload["sources"]), 2)
        self.assertEqual(payload["sources"][0]["url"], SEARCH_RESULT["results"][0]["url"])

    def test_unknown_report_is_404_report_not_found(self):
        chat_id = self._chat()
        with self._client() as client:
            response = client.get(f"/api/chats/{chat_id}/reports/missing")
        self.assertEqual(response.status_code, 404)
        detail = response.json()["detail"]
        self.assertEqual(detail["category"], "report_not_found")
        self.assertNotIn("/", detail["message"])

    def test_a_report_of_another_chat_is_not_visible(self):
        owner = self._chat("Owner")
        other = self._chat("Other")
        saved = self._save(owner)
        with self._client() as client:
            listing = client.get(f"/api/chats/{other}/reports").json()
            detail = client.get(f"/api/chats/{other}/reports/{saved['report_id']}")
        self.assertEqual(listing["count"], 0)
        self.assertEqual(detail.status_code, 404)
        self.assertEqual(detail.json()["detail"]["category"], "report_not_found")

    def test_unknown_chat_is_404_chat_not_found(self):
        with self._client() as client:
            listing = client.get("/api/chats/missing/reports")
            detail = client.get("/api/chats/missing/reports/r1")
        self.assertEqual(listing.status_code, 404)
        self.assertEqual(listing.json()["detail"]["category"], "chat_not_found")
        self.assertEqual(detail.status_code, 404)
        self.assertEqual(detail.json()["detail"]["category"], "chat_not_found")

    def test_deleting_the_chat_removes_its_reports(self):
        chat_id = self._chat()
        saved = self._save(chat_id)
        with self._client() as client:
            deleted = client.delete(f"/api/chats/{chat_id}")
            listing = client.get(f"/api/chats/{chat_id}/reports")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(listing.status_code, 404)
        with self.database.connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM reports WHERE chat_id = ?", (chat_id,)
            ).fetchone()[0]
        self.assertEqual(count, 0)
        self.assertIsNone(self.reports._reports.get_report(chat_id, saved["report_id"]))


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
