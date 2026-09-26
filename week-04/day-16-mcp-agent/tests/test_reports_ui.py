"""Fast, browser-free guards for the Saved reports UI (D19-15).

The live UI status (``REPORTS_UI_STATUS``) is produced by ``test.bat
acceptance``; these guards keep the wiring and safety properties in the sources.
"""

from __future__ import annotations

import re
import unittest

from harness.qa_bridge import PROJECT_DIR

INDEX_HTML = (PROJECT_DIR / "static" / "index.html").read_text(encoding="utf-8")
APP_JS = (PROJECT_DIR / "static" / "app.js").read_text(encoding="utf-8")
STYLES_CSS = (PROJECT_DIR / "static" / "styles.css").read_text(encoding="utf-8")


class ReportsUiSourceGuardTest(unittest.TestCase):
    """The reports panel reads the per-chat reports and renders text safely."""

    def test_index_has_the_reports_panel_ids(self):
        for element_id in (
            'id="reports-panel"',
            'id="reports-list"',
            'id="reports-refresh-button"',
        ):
            self.assertIn(element_id, INDEX_HTML)
        self.assertIn("Saved reports", INDEX_HTML)

    def test_app_js_reads_the_reports_endpoints(self):
        self.assertIn("/reports", APP_JS)
        self.assertIn("renderReports", APP_JS)
        self.assertIn("loadReports", APP_JS)

    def test_report_card_is_a_details_element_with_the_id_attribute(self):
        self.assertIn(".report-card", STYLES_CSS)
        self.assertIn('createElement("details")', APP_JS)
        self.assertIn('className = "report-card"', APP_JS)
        self.assertIn("dataset.reportId", APP_JS)

    def test_summary_and_sources_use_text_content_only(self):
        body_start = APP_JS.index("function buildReportSource")
        body_end = APP_JS.index("async function loadReports")
        body = APP_JS[body_start:body_end]
        self.assertIn("textContent", body)
        self.assertNotIn("innerHTML", body)

    def test_sources_render_as_an_ordered_list(self):
        body_start = APP_JS.index("function renderReportDetail")
        body_end = APP_JS.index("async function loadReportDetail")
        body = APP_JS[body_start:body_end]
        self.assertIn('createElement("ol")', body)
        self.assertIn('className = "report-sources"', body)

    def test_links_are_restricted_to_http_https(self):
        body_start = APP_JS.index("function buildReportSource")
        body_end = APP_JS.index("function renderReportDetail")
        body = APP_JS[body_start:body_end]
        self.assertIn(r"/^https?:\/\//i.test(url)", body)
        self.assertIn('rel = "noopener noreferrer"', body)
        self.assertIn('target = "_blank"', body)

    def test_detail_is_loaded_lazily_on_first_open(self):
        self.assertIn("loadReportDetail", APP_JS)
        self.assertIn('addEventListener("toggle"', APP_JS)
        self.assertIn("card.open", APP_JS)
        self.assertIn('dataset.loaded', APP_JS)

    def test_empty_state_text(self):
        self.assertIn("No reports in this chat.", APP_JS)

    def test_reports_are_guarded_against_parallel_requests(self):
        start = APP_JS.index("async function loadReports")
        end = APP_JS.index("// Idempotent: a second call while the timer already runs")
        body = APP_JS[start:end]
        self.assertIn("reportsRequestInFlight", body)
        self.assertIn("if (reportsRequestInFlight)", body)
        self.assertIn("finally", body)
        self.assertIn("reportsRequestInFlight = false", body)
        self.assertIn("token !== selectionToken", body)
        self.assertIn("chatId !== state.activeChatId", body)

    def test_reports_are_reloaded_on_selection_after_an_answer_and_deletion(self):
        select = APP_JS[APP_JS.index("async function selectChat") : APP_JS.index("async function loadChats")]
        self.assertIn("/reports", select)
        send = APP_JS[APP_JS.index("async function send") : APP_JS.index("function pill(")]
        self.assertIn("await loadReports()", send)
        delete = APP_JS[APP_JS.index("async function deleteChat") : APP_JS.index("async function clearChat")]
        self.assertIn("await loadReports()", delete)
        self.assertIn('reportsRefreshButton.addEventListener("click", loadReports)', APP_JS)

    def test_styles_exist_for_report_cards(self):
        for selector in (
            ".reports",
            ".report-card",
            ".report-topic",
            ".report-meta",
            ".report-summary",
            ".report-sources",
        ):
            self.assertIn(selector, STYLES_CSS)
        match = re.search(r"\.report-summary\s*\{[^}]*white-space:\s*pre-wrap", STYLES_CSS)
        self.assertIsNotNone(match, "the report summary must preserve line breaks")

    def test_identifiers_are_never_rendered_as_visible_text(self):
        offenders = [
            line
            for line in APP_JS.splitlines()
            if "textContent = report.report_id" in line
            or "textContent = reportId" in line
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
