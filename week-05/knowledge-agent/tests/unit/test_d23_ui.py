"""D23-C08: the retrieval/filter/rewrite and four-mode comparison UI exists.

The UI is a static HTML/JS surface; this test asserts the elements consumed by
``app.js`` are present and that new D23 elements are English while the existing
Russian D22 panels stay untranslated.
"""

from __future__ import annotations

from pathlib import Path

UI_DIR = Path(__file__).resolve().parents[2] / "knowledge_agent" / "ui"

REQUIRED_D23_IDS = (
    "d23-mode",
    "d23-min-score",
    "d23-prefilter",
    "d23-postfilter",
    "d23-ask",
    "chat-search-query",
    "d23-trace",
    "d23-compare",
    "d23-compare-result",
)


def _html() -> str:
    return (UI_DIR / "index.html").read_text(encoding="utf-8")


def test_d23_elements_are_present():
    html = _html()
    for element_id in REQUIRED_D23_IDS:
        assert f'id="{element_id}"' in html, f"missing #{element_id}"


def test_d23_panel_is_english_and_d22_panels_are_unchanged():
    html = _html()
    assert "Retrieval settings" in html
    assert "Compare four modes" in html
    # Existing D22 Russian panels must not be translated by this task.
    for russian in ("Чат (RAG)", "Без RAG", "С RAG", "Сравнить"):
        assert russian in html


def test_app_js_wires_the_d23_controls():
    script = (UI_DIR / "app.js").read_text(encoding="utf-8")
    for element_id in ("d23-ask", "d23-compare"):
        assert f'$("{element_id}")' in script
    assert "/api/chat/compare-modes" in script


def test_render_d23_compare_shows_sources_reason_and_metrics():
    script = (UI_DIR / "app.js").read_text(encoding="utf-8")
    body = script.split("function renderD23Compare", 1)[1].split(
        "async function runD23Compare", 1
    )[0]
    for needle in ("retrieval.passed", "finish_reason", "truncated", "rewrite.usage", "latency_ms"):
        assert needle in body, f"renderD23Compare must show {needle}"
