"""Fast, browser-free guards for the day-20 UI sources.

The live UI statuses (``NOTIFIER_SERVERS_UI_STATUS``, ``NOTIFICATION_UI_STATUS``)
come from ``test.bat acceptance``; these guards keep the wiring and the safety
properties (both servers, ``[A]``/``[B]`` marks, no watch polling, delivery status
from B) in the committed sources.
"""

from __future__ import annotations

import unittest

from harness.qa_bridge import PROJECT_DIR

INDEX_HTML = (PROJECT_DIR / "static" / "index.html").read_text(encoding="utf-8")
APP_JS = (PROJECT_DIR / "static" / "app.js").read_text(encoding="utf-8")
STYLES_CSS = (PROJECT_DIR / "static" / "styles.css").read_text(encoding="utf-8")


class McpServersUiSourceGuardTest(unittest.TestCase):
    """``MCP status`` shows both servers through one endpoint."""

    def test_status_uses_the_servers_endpoint(self):
        self.assertIn("/api/mcp/servers", APP_JS)
        self.assertIn("refreshStatus", APP_JS)

    def test_status_renders_every_server_with_its_label_and_count(self):
        start = APP_JS.index("async function refreshStatus")
        end = APP_JS.index("formEl.addEventListener")
        body = APP_JS[start:end]
        self.assertIn("payload.servers", body)
        self.assertIn("server.label", body)
        self.assertIn("tools_count", body)
        self.assertIn('server.connected ? "connected" : "disconnected"', body)


class TechnicalLineServerTagTest(unittest.TestCase):
    """Tool lines are marked with the owning server, e.g. ``[A]``/``[B]``."""

    def test_server_tag_helper_brackets_the_label(self):
        self.assertIn("function serverTag", APP_JS)
        self.assertIn("`[${data.server}] `", APP_JS)

    def test_tool_lines_use_the_tag(self):
        self.assertIn("MCP tool call: ${serverTag(data)}", APP_JS)
        self.assertIn("${serverTag(data)}${data.tool} ${data.ok", APP_JS)


class NotificationWatchesUiSourceGuardTest(unittest.TestCase):
    """The watches panel reads server B on selection/refresh only."""

    def test_index_has_the_watches_panel(self):
        for marker in (
            'id="watches-panel"',
            'id="watches-list"',
            'id="watches-refresh-button"',
            "Notification watches",
        ):
            self.assertIn(marker, INDEX_HTML)

    def test_app_reads_the_watches_endpoint(self):
        self.assertIn("/watches", APP_JS)
        self.assertIn("function loadWatches", APP_JS)
        self.assertIn("function renderWatches", APP_JS)

    def test_watches_are_loaded_on_selection_and_by_refresh_only(self):
        select = APP_JS[APP_JS.index("async function selectChat") : APP_JS.index("async function loadChats")]
        self.assertIn("/watches", select)
        self.assertIn("renderWatches", select)
        self.assertIn(
            'watchesRefreshButton.addEventListener("click", loadWatches)', APP_JS
        )

    def test_no_periodic_watch_polling(self):
        self.assertNotIn("setInterval(loadWatches", APP_JS)
        self.assertNotIn("setInterval(refreshStatus", APP_JS)

    def test_watches_are_guarded_against_parallel_requests(self):
        start = APP_JS.index("async function loadWatches")
        end = APP_JS.index("// Idempotent: a second call while the timer already runs")
        body = APP_JS[start:end]
        self.assertIn("watchesRequestInFlight", body)
        self.assertIn("token !== selectionToken", body)
        self.assertIn("chatId !== state.activeChatId", body)

    def test_empty_and_unavailable_states_exist(self):
        self.assertIn("No notification watches in this chat.", APP_JS)
        self.assertIn("notification service is unavailable", APP_JS.lower())

    def test_delivery_status_comes_from_b_not_from_the_model_text(self):
        start = APP_JS.index("function watchDelivery")
        end = APP_JS.index("function renderWatches")
        body = APP_JS[start:end]
        self.assertIn("last_delivery", body)
        self.assertIn("deliveries", body)

    def test_styles_exist_for_the_watches_panel(self):
        for selector in (".watches", ".watch-card", ".watch-query", ".watch-delivery"):
            self.assertIn(selector, STYLES_CSS)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
