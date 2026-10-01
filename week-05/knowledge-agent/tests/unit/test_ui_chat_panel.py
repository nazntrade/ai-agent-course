"""Regression: the D22 RAG chat panel stays first with enlarged inputs.

The chat panel is the primary Day 22 surface. It must remain the first panel
under the header, the question field must be a multiline textarea, and every
element id consumed by ``app.js`` must stay present.
"""

from __future__ import annotations

import re
from pathlib import Path

UI_DIR = Path(__file__).resolve().parents[2] / "knowledge_agent" / "ui"

REQUIRED_IDS = (
    "chat-strategy",
    "chat-top-k",
    "chat-max-context",
    "chat-question",
    "chat-plain",
    "chat-rag",
    "chat-compare",
    "chat-status",
    "chat-sources",
    "chat-answer",
    "chat-compare-result",
)


def _html() -> str:
    return (UI_DIR / "index.html").read_text(encoding="utf-8")


def _main() -> str:
    return _html().split("<main>", 1)[1].split("</main>", 1)[0]


def test_chat_panel_is_the_first_panel():
    main = _main()
    panels = re.findall(r'<section class="panel">(.*?)</section>', main, re.S)
    assert panels, "At least one panel is expected."
    assert "Чат (RAG)" in panels[0]
    assert 'id="chat-question"' in panels[0]
    # It must precede the other D21 panels, notably the collection selector.
    assert main.index("Чат (RAG)") < main.index('id="collection-select"')


def test_chat_question_is_a_multiline_textarea():
    match = re.search(r'<textarea id="chat-question"[^>]*></textarea>', _html())
    assert match, "The chat question must be a textarea."
    assert "rows=" in match.group(0)


def test_all_ids_consumed_by_app_js_are_present():
    html = _html()
    for element_id in REQUIRED_IDS:
        assert f'id="{element_id}"' in html, f"missing #{element_id}"
