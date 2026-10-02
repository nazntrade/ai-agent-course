"""D24-03..D24-07: whitespace normalization, strict parsing and formal verification."""

from __future__ import annotations

import pytest

from knowledge_agent.chat.citations import (
    GroundingVerifier,
    normalize_whitespace,
    parse_grounded_response,
)
from knowledge_agent.domain.contracts import GroundedAnswer
from knowledge_agent.domain.errors import ChatInvalidResponse

PASSED = "a" * 64
CHUNK_TEXT = "Memory stores observations. Planning decomposes goals into steps."


def _passed():
    return [
        {
            "chunk_id": PASSED,
            "text": CHUNK_TEXT,
            "metadata": {
                "source_label": "agents.md",
                "section_path": "2.1 Memory",
                "page_start": 5,
                "page_end": 6,
            },
        }
    ]


def _verify(citations, **kwargs):
    grounded = GroundedAnswer(answer="answer", citations=citations, **kwargs)
    return GroundingVerifier(_passed()).verify(grounded, threshold=0.45)


def test_normalize_whitespace_nfc_collapse_trim():
    value = normalize_whitespace("  a\u00a0b\t\n c  ")
    assert value == "a b c"
    assert normalize_whitespace("") == ""
    assert normalize_whitespace(None) == ""


def test_exact_quote_is_verified():
    result = _verify([{"chunk_id": PASSED, "quote": "Memory stores observations."}])
    citation = result.citations[0]
    assert citation.source_exists is True
    assert citation.quote_verbatim is True
    assert citation.status == "verified"
    assert citation.source == "agents.md"
    assert citation.section == "2.1 Memory"
    assert citation.page_start == 5 and citation.page_end == 6
    assert result.status == "verified"
    assert result.meaning_check == "not_performed"
    assert citation.meaning_supported is None


def test_whitespace_equivalent_quote_passes_normalization():
    result = _verify([{"chunk_id": PASSED, "quote": "Memory  stores\n observations."}])
    assert result.citations[0].quote_verbatim is True


def test_paraphrase_and_empty_quote_are_rejected():
    paraphrase = _verify([{"chunk_id": PASSED, "quote": "Memory keeps observations."}])
    assert paraphrase.citations[0].quote_verbatim is False
    assert paraphrase.citations[0].status == "quote_mismatch"
    empty = _verify([{"chunk_id": PASSED, "quote": ""}])
    assert empty.citations[0].status == "quote_mismatch"


def test_unknown_chunk_id_is_not_confirmed():
    result = _verify([{"chunk_id": "b" * 64, "quote": "Memory stores observations."}])
    citation = result.citations[0]
    assert citation.source_exists is False
    assert citation.status == "unknown_chunk_id"
    assert citation.reason == "chunk_id_not_passed"
    assert result.status == "failed"


def test_fabricated_quote_with_real_id_is_not_verified():
    result = _verify([{"chunk_id": PASSED, "quote": "This sentence is not in the fragment."}])
    assert result.citations[0].status == "quote_mismatch"
    assert result.status != "verified"


def test_translation_keeps_original_and_marks_translation():
    result = _verify(
        [
            {
                "chunk_id": PASSED,
                "quote": "Memory stores observations.",
                "translation": "Память хранит наблюдения.",
            }
        ]
    )
    citation = result.citations[0]
    assert citation.is_translation is True
    assert citation.translation == "Память хранит наблюдения."
    assert citation.quote == "Memory stores observations."
    assert citation.status == "verified"


def test_translation_without_original_is_not_verified():
    result = _verify(
        [{"chunk_id": PASSED, "quote": "", "translation": "Память хранит наблюдения."}]
    )
    assert result.citations[0].quote_verbatim is False
    assert result.citations[0].status != "verified"


def test_insufficient_model_marks_an_honest_refusal():
    result = _verify([], insufficient=True)
    assert result.status == "refused"
    assert result.reason == "model_insufficient"
    assert result.refusal is not None and result.refusal["reason"] == "model_insufficient"


def test_limitation_makes_a_partial_answer():
    result = _verify(
        [{"chunk_id": PASSED, "quote": "Memory stores observations."}],
        limitation="partial answer: the documents do not state everything asked.",
    )
    assert result.status == "partial"
    assert result.limitation
    assert result.status != "verified"


def test_no_citations_is_failed_not_verified():
    result = _verify([])
    assert result.status == "failed"
    assert result.reason == "no_citations"


# -- parse_grounded_response ---------------------------------------------

def _parse_error(text):
    with pytest.raises(ChatInvalidResponse) as excinfo:
        parse_grounded_response(text)
    assert excinfo.value.details.get("format") == "grounded_json"
    return excinfo.value


def test_parse_valid_json_object():
    parsed = parse_grounded_response(
        '{"answer": "hi", "citations": [{"chunk_id": "a", "quote": "q"}], '
        '"insufficient": false, "limitation": null}'
    )
    assert parsed.answer == "hi"
    assert parsed.citations == [{"chunk_id": "a", "quote": "q", "translation": None}]
    assert parsed.insufficient is False


def test_parse_single_fenced_json_block():
    parsed = parse_grounded_response(
        'Here you go:\n```json\n{"answer": "hi", "citations": []}\n```'
    )
    assert parsed.answer == "hi"


def test_parse_accepts_one_object_wrapped_in_prose():
    # Regression (real provider deviation): the model prefixed the JSON object
    # with prose instead of returning it raw. One well-formed object is accepted.
    parsed = parse_grounded_response(
        'Sure, here it is: {"answer": "hi", "citations": []} Let me know if needed.'
    )
    assert parsed.answer == "hi"


def test_parse_rejects_two_objects():
    _parse_error('{"answer": "a", "citations": []} and {"answer": "b", "citations": []}')


def test_parse_rejects_malformed_and_incomplete_json():
    _parse_error("{")
    _parse_error("not json at all")
    _parse_error('{"answer": "hi", "citations": [')


def test_parse_rejects_missing_answer_and_wrong_types():
    _parse_error('{"citations": []}')
    _parse_error('{"answer": "  ", "citations": []}')
    _parse_error('{"answer": "hi", "citations": "nope"}')
    _parse_error('{"answer": "hi", "citations": [{"quote": "q"}]}')
    _parse_error('{"answer": "hi", "citations": [{"chunk_id": "a", "quote": 3}]}')
    _parse_error('{"answer": "hi", "citations": [], "insufficient": "yes"}')
    _parse_error('{"answer": "hi", "citations": [], "limitation": 3}')
    _parse_error("")
