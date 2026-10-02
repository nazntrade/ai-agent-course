"""D24-14/D24-15: sources and citation UI exists, is English and wraps long lines.

This is a static inspection; the actual browser check is performed manually by
Developer and independently by Tester (run_app.bat).
"""

from __future__ import annotations

from pathlib import Path

UI_DIR = Path(__file__).resolve().parents[2] / "knowledge_agent" / "ui"


def _html() -> str:
    return (UI_DIR / "index.html").read_text(encoding="utf-8")


def _script() -> str:
    return (UI_DIR / "app.js").read_text(encoding="utf-8")


def test_grounding_container_is_present():
    assert 'id="chat-grounding"' in _html()


def test_app_js_renders_english_sources_and_citations():
    script = _script()
    for needle in (
        "Sources & citations",
        "Show fragment",
        "Meaning support:",
        "Verification failed",
        "No relevant sources found",
        "grounding.citations",
        "chunk_id",
    ):
        assert needle in script, needle


def test_show_fragment_uses_the_chunks_endpoint():
    assert "/chunks?chunk_id=" in _script()


def test_meaning_status_is_not_claimed_checked():
    # The UI must not present the formal check as a semantic/meaning check.
    assert "meaning_check" in _script()
    assert "not checked" in _script()


def test_existing_russian_panels_remain_untranslated():
    html = _html()
    for russian in ("Чат (RAG)", "Без RAG", "С RAG", "Сравнить"):
        assert russian in html


def test_long_citation_lines_wrap():
    styles = (UI_DIR / "styles.css").read_text(encoding="utf-8")
    assert "overflow-wrap: anywhere" in styles
    assert ".citation" in styles
