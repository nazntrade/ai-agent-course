"""D26-16/R5.2: citation verification against really passed fragments."""

from __future__ import annotations

from app.rag.citations import CitationVerifier, normalize_whitespace


FRAGMENTS = [
    {"chunk_id": "c1", "label": "geo.txt", "text": "The capital of France is Paris."},
    {"chunk_id": "c2", "label": "fruit.txt", "text": "Bananas are yellow."},
]


def test_source_exists_and_quote_verbatim_pass():
    report = CitationVerifier(FRAGMENTS).verify("Answer [c1]")
    assert report.status in ("verified", "source_only")
    assert report.inline_unsupported == []


def test_unknown_reference_is_unsupported():
    report = CitationVerifier(FRAGMENTS).verify("Answer [c9]")
    assert report.status == "failed"
    assert report.inline_unsupported == ["c9"]


def test_numeric_index_reference_is_recognized():
    report = CitationVerifier(FRAGMENTS).verify("Answer [1]")
    assert report.inline_unsupported == []
    assert report.citations[0].chunk_id == "c1"


def test_structured_quote_verbatim_is_verified():
    report = CitationVerifier(FRAGMENTS).verify(
        "See [c1]",
        quoted_citations=[{"chunk_id": "c1", "quote": "The capital of France is Paris."}],
    )
    check = [c for c in report.citations if c.reference == "c1"][0]
    assert check.status == "verified"
    assert check.quote_verbatim is True


def test_structured_quote_mismatch_is_not_verified():
    report = CitationVerifier(FRAGMENTS).verify(
        "See [c1]",
        quoted_citations=[{"chunk_id": "c1", "quote": "Paris is the capital of Spain."}],
    )
    check = [c for c in report.citations if c.reference == "c1"][0]
    assert check.status == "quote_mismatch"
    assert check.quote_verbatim is False
    assert report.status == "failed"


def test_whitespace_normalization_is_case_sensitive():
    assert normalize_whitespace("a  b\nc") == "a b c"
    report = CitationVerifier(FRAGMENTS).verify(
        "x",
        quoted_citations=[{"chunk_id": "c1", "quote": "the capital of france is paris."}],
    )
    check = report.citations[0]
    assert check.quote_verbatim is False  # case differs, and normalization is case-sensitive
