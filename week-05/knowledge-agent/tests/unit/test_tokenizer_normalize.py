"""Tokenizer and normalization determinism (SPEC 8.4, 17.1)."""

from __future__ import annotations

from knowledge_agent.text.normalize import (
    dehyphenate,
    detect_language,
    normalize_text,
    render_markdown,
)
from knowledge_agent.text.tokenizer import LexicalTokenizer, count_tokens

from tests.helpers import make_document


def test_tokenizer_is_deterministic_and_unicode():
    tokenizer = LexicalTokenizer()
    text = "Hello, мир! \u00e9t\u00e9 42."
    first = tokenizer.tokenize(text)
    assert first == tokenizer.tokenize(text)
    assert "мир" in first
    assert "," in first
    assert first == ["Hello", ",", "мир", "!", "été", "42", "."]


def test_tokenize_spans_align_with_text():
    tokenizer = LexicalTokenizer()
    text = "one two"
    spans = tokenizer.tokenize_spans(text)
    assert spans == [("one", 0, 3), ("two", 4, 7)]
    assert all(text[start:end] == token for token, start, end in spans)


def test_dehyphenation_joins_lowercase_continuation():
    assert dehyphenate("autono-\nmous agents") == "autonomous agents"
    # Upper-case continuation is not a hyphen split.
    assert dehyphenate("GPT-\n4 model") == "GPT-\n4 model"


def test_normalize_expands_ligatures_and_collapses_whitespace():
    assert normalize_text("of\ufb01ce   space") == "office space"


def test_language_detection():
    assert detect_language("Автономные агенты используют память") == "ru"
    assert detect_language("Autonomous agents use memory") == "en"


def test_render_markdown_keeps_headings_and_provenance():
    document = make_document("Body text.", section_path="2.1 Memory")
    rendered = render_markdown(document)
    assert "2.1 Memory" in rendered
    assert "Body text." in rendered
    assert rendered.startswith("#")


def test_count_tokens_matches_tokenizer():
    tokenizer = LexicalTokenizer()
    text = "a b, c!"
    assert count_tokens(text) == len(tokenizer.tokenize(text))
