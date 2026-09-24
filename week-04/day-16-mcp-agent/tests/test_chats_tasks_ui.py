"""Fast, browser-free guards for the chats and scheduled-tasks UI (D18-19).

They do not run the page: they keep the wiring and the safety properties of
``static/index.html`` and ``static/app.js`` in the sources, so a regression is
caught even when no system browser is installed. The live UI statuses
(``CHATS_UI_STATUS``, ``TASKS_UI_STATUS``) are produced by ``test.bat
acceptance``; these guards are the permanent, cheap half of the same contract.
"""

from __future__ import annotations

import re
import unittest

from harness.qa_bridge import PROJECT_DIR

INDEX_HTML = (PROJECT_DIR / "static" / "index.html").read_text(encoding="utf-8")
APP_JS = (PROJECT_DIR / "static" / "app.js").read_text(encoding="utf-8")
STYLES_CSS = (PROJECT_DIR / "static" / "styles.css").read_text(encoding="utf-8")


class ChatsUiSourceGuardTest(unittest.TestCase):
    """The chats panel and its API calls stay wired."""

    def test_index_has_the_chat_panel_ids(self):
        for element_id in (
            'id="chats-panel"',
            'id="new-chat-button"',
            'id="chats-list"',
            'id="chat-limit-message"',
        ):
            self.assertIn(element_id, INDEX_HTML)

    def test_clear_button_is_labelled_clear_chat(self):
        match = re.search(
            r'id="clear-button"[^>]*>([^<]*)<', INDEX_HTML
        )
        self.assertIsNotNone(match, "the clear button is missing")
        self.assertEqual(match.group(1).strip(), "Clear chat")
        self.assertNotIn("Clear session", INDEX_HTML)

    def test_app_js_calls_the_chat_endpoints(self):
        for token in (
            "/api/chats",
            "createChatRequest",
            "renameChat",
            "deleteChat",
            "force=true",
            "/clear",
            "dataset.chatId",
        ):
            self.assertIn(token, APP_JS)

    def test_chat_items_carry_the_data_attribute_selector(self):
        self.assertIn("dataset.chatId", APP_JS)
        self.assertIn(".chat-item", STYLES_CSS)
        self.assertIn(".chat-item.active", STYLES_CSS)

    def test_chat_limit_message_comes_from_the_backend_detail(self):
        self.assertIn("payload.detail", APP_JS)
        self.assertIn("detail.message", APP_JS)

    def test_stream_request_sends_chat_id(self):
        self.assertIn("chat_id: state.activeChatId", APP_JS)
        self.assertNotIn("session_id", APP_JS)

    def test_chat_actions_are_disabled_while_busy(self):
        self.assertIn("setChatActionsEnabled", APP_JS)
        self.assertIn("state.busy", APP_JS)


class TasksUiSourceGuardTest(unittest.TestCase):
    """The tasks panel reads the per-chat tasks and renders links safely."""

    def test_index_has_the_tasks_panel_ids(self):
        for element_id in (
            'id="tasks-panel"',
            'id="tasks-list"',
            'id="tasks-refresh-button"',
        ):
            self.assertIn(element_id, INDEX_HTML)

    def test_app_js_reads_the_tasks_endpoint(self):
        self.assertIn("/tasks", APP_JS)
        self.assertIn("renderTasks", APP_JS)
        self.assertIn("last_run", APP_JS)

    def test_task_links_are_created_through_the_dom_api(self):
        self.assertIn('createElement("a")', APP_JS)
        self.assertIn('rel = "noopener noreferrer"', APP_JS)
        # No HTML sink may be used to inject task results.
        offenders = [
            line for line in APP_JS.splitlines() if ".innerHTML" in line and "loader" not in line
        ]
        self.assertEqual(offenders, [])

    def test_styles_exist_for_task_cards(self):
        for selector in (".task-card", ".task-query", ".task-links", ".tasks"):
            self.assertIn(selector, STYLES_CSS)

    def _load_tasks_body(self) -> str:
        start = APP_JS.index("async function loadTasks")
        end = APP_JS.index("function startTasksPolling")
        return APP_JS[start:end]

    def test_tasks_panel_auto_refreshes_while_the_page_is_open(self):
        self.assertIn("TASKS_POLL_INTERVAL_MS = 10000", APP_JS)
        self.assertIn("setInterval(loadTasks, TASKS_POLL_INTERVAL_MS)", APP_JS)
        self.assertIn("clearInterval(tasksPollTimer)", APP_JS)

    def test_tasks_polling_is_guarded_against_parallel_requests(self):
        body = self._load_tasks_body()
        self.assertIn("tasksRequestInFlight", body)
        self.assertIn("if (tasksRequestInFlight)", body)
        self.assertIn("finally", body)
        self.assertIn("tasksRequestInFlight = false", body)

    def test_the_poll_timer_is_cleared_when_the_page_is_left(self):
        self.assertIn('"pagehide"', APP_JS)
        self.assertIn('"beforeunload"', APP_JS)
        self.assertIn("stopTasksPolling", APP_JS)
        self.assertIn("startTasksPolling", APP_JS)

    def test_the_refresh_button_stays_wired(self):
        self.assertIn("tasksRefreshButton.addEventListener", APP_JS)

    def test_load_tasks_calls_only_the_tasks_endpoint(self):
        body = self._load_tasks_body()
        self.assertIn("/tasks", body)
        # The poll must never trigger a chat turn, a model call or a tool call.
        self.assertNotIn("chat/stream", body)
        self.assertNotIn("mcp", body)

    def test_load_tasks_discards_a_stale_selection(self):
        body = self._load_tasks_body()
        self.assertIn("selectionToken", body)
        self.assertIn("token !== selectionToken", body)
        self.assertIn("chatId !== state.activeChatId", body)

    def test_identifiers_are_never_rendered_as_visible_text(self):
        # The opaque ids may only be used as attributes/keys, never printed.
        patterns = (
            "textContent = chat.id",
            "textContent = task.task_id",
            "textContent = chatId",
        )
        for pattern in patterns:
            self.assertNotIn(pattern, APP_JS)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
