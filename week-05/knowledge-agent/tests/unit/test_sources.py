"""Source adapters: text and PDF error taxonomy (D21-09, SPEC 7.2)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from knowledge_agent.domain.errors import (
    SourceEmpty,
    SourceEncrypted,
    SourceInvalid,
    SourceNoTextLayer,
    SourceUnsupported,
)
from knowledge_agent.domain.models import SourceRef
from knowledge_agent.sources import DefaultSourceResolver
from knowledge_agent.sources import pdf_source
from knowledge_agent.sources.pdf_source import PdfSourceAdapter
from knowledge_agent.sources.text_source import TextSourceAdapter

from tests.fixtures.mini_pdf import build_pdf_bytes, write_mini_pdf

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def ref_for(path: Path, kind: str) -> SourceRef:
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    return SourceRef(
        source_id=digest,
        uri=str(path),
        public_uri=path.name,
        label=path.name,
        kind=kind,
        content_sha256=digest,
        size_bytes=len(data),
    )


def test_text_source_extracts_markdown_sections():
    path = FIXTURES / "sample.md"
    document = TextSourceAdapter().extract(ref_for(path, "text"))
    assert document.source.kind == "text"
    assert document.extraction.useful_chars > 0
    paths = {section.section_path for section in document.sections}
    assert "1 Introduction" in paths
    assert "2 Conclusion" in paths


def test_text_source_empty_file(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("", encoding="utf-8")
    with pytest.raises(SourceEmpty):
        TextSourceAdapter().extract(ref_for(path, "text"))


def test_text_source_unsupported_suffix(tmp_path):
    path = tmp_path / "data.csv"
    path.write_text("a,b", encoding="utf-8")
    ref = ref_for(path, "text")
    with pytest.raises(SourceUnsupported):
        TextSourceAdapter().extract(ref)


def test_text_source_detects_changed_bytes(tmp_path):
    path = tmp_path / "note.txt"
    path.write_text("original", encoding="utf-8")
    ref = ref_for(path, "text")
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(SourceInvalid):
        TextSourceAdapter().extract(ref)


def test_pdf_source_reads_text_layer(tmp_path):
    path = write_mini_pdf(tmp_path / "mini.pdf")
    document = PdfSourceAdapter(useful_page_min_chars=500).extract(ref_for(path, "pdf"))
    assert document.extraction.adapter == "pdf"
    assert document.extraction.page_count == 1
    assert document.extraction.useful_chars > 0
    assert document.extraction.useful_pages == 0  # a tiny page is not "useful"
    assert document.sections
    assert "Memory" in document.normalized_markdown


def test_pdf_source_without_text_layer(tmp_path):
    path = tmp_path / "blank.pdf"
    path.write_bytes(build_pdf_bytes([]))
    with pytest.raises(SourceNoTextLayer):
        PdfSourceAdapter().extract(ref_for(path, "pdf"))


def test_pdf_source_invalid_bytes(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"this is not a pdf at all")
    with pytest.raises(SourceInvalid):
        PdfSourceAdapter().extract(ref_for(path, "pdf"))


def test_pdf_source_encrypted_mapping(tmp_path, monkeypatch):
    path = write_mini_pdf(tmp_path / "enc.pdf")

    def raise_password(_data):
        raise Exception("PDF password required to decrypt")

    monkeypatch.setattr(pdf_source, "_extract_with_pdfplumber", raise_password)
    monkeypatch.setattr(pdf_source, "_extract_with_pypdfium", raise_password)
    with pytest.raises(SourceEncrypted):
        PdfSourceAdapter().extract(ref_for(path, "pdf"))


def test_default_resolver_selects_adapter_by_extension(tmp_path):
    resolver = DefaultSourceResolver()
    assert resolver.resolve("a.pdf").kind == "pdf"
    assert resolver.resolve("a.md").kind == "text"
    assert resolver.resolve("a.txt").kind == "text"
    with pytest.raises(SourceUnsupported):
        resolver.resolve("a.docx")


def test_heading_detection_accepts_real_headings_and_rejects_noise():
    from knowledge_agent.sources.pdf_source import _heading_level, _role_heading

    assert _heading_level("1 Introduction", 10.0, 9.0) == 1
    assert _heading_level("2.1 Agent Architecture", 10.0, 9.0) == 2
    assert _heading_level("3.2 Natural Science", 10.0, 9.0) == 2
    assert _heading_level("References", 10.0, 9.0) == 1
    # Running heads, reference/list items and table rows are not headings.
    assert _heading_level("5202 Lei Wang, Chen Ma", 20.0, 9.0) is None
    assert _heading_level("1. Mnih V, Kavukcuoglu K", 10.0, 9.0) is None
    assert _heading_level("MRKL[73] - - - 05/2022", 10.0, 9.0) is None
    assert _heading_level("M", 20.0, 9.0) is None
    # References can share the line with the first entry; keep the remainder.
    assert _role_heading("References 13. Shen Y, Song K") == (
        "References",
        "13. Shen Y, Song K",
    )
    assert _role_heading("References") == ("References", None)
    assert _role_heading("Referenced work") is None
    # A body sentence starting with "References ..." must not become a section.
    assert _role_heading("References are listed in the appendix section below") is None
    assert _role_heading("References 1. this is prose that continues as body text") is None


def test_strip_headers_footers_removes_alternating_running_heads():
    from knowledge_agent.sources.pdf_layout import Line, strip_headers_footers

    pages = []
    for page_number in range(1, 7):
        header = Line(
            text=f"{page_number * 2} Front. Comput. Sci.,2025",
            x0=60,
            x1=500,
            top=30,
            bottom=45,
            size=9,
            page=page_number,
        )
        body = Line(
            text=f"Body line on page {page_number}",
            x0=60,
            x1=500,
            top=120,
            bottom=132,
            size=9,
            page=page_number,
        )
        pages.append([header, body])
    cleaned, removed = strip_headers_footers(pages, [800.0] * 6)
    assert removed == 6
    assert all(page[0].text.startswith("Body line") for page in cleaned)


def test_pdf_splits_tightly_kerned_words_and_detects_heading(tmp_path):
    from tests.fixtures.mini_pdf import write_kerning_pdf

    path = write_kerning_pdf(tmp_path / "tight.pdf")
    document = PdfSourceAdapter().extract(ref_for(path, "pdf"))

    assert document.sections[0].section_path == "1 Introduction"
    combined = " ".join(section.text for section in document.sections)
    # Words separated only by a ~2.8pt kerning gap must be split.
    assert "Memory stores observations" in combined
    assert "Memorystores" not in combined
    # A long single word must not be cut apart.
    assert "Supercalifragilisticexpialidocious" in combined
    assert "Supercalifragilistice xpialidocious" not in combined


def test_pdf_does_not_split_tracked_words(tmp_path):
    from tests.fixtures.mini_pdf import write_tracking_pdf

    path = write_tracking_pdf(tmp_path / "tracked.pdf", ["1 Introduction", "Tracking"])
    document = PdfSourceAdapter().extract(ref_for(path, "pdf"))
    combined = " ".join(section.text for section in document.sections)
    # Letter gaps of 0.8pt at 10pt are below the scaled threshold: keep the word.
    assert "Tracking" in combined
    assert "Trac king" not in combined
    assert "T racking" not in combined



def test_pdf_effective_extraction_version_changes_metadata_and_ids(tmp_path, monkeypatch):
    path = write_mini_pdf(tmp_path / "versions.pdf")
    reference = ref_for(path, "pdf")
    adapter = PdfSourceAdapter()
    first = adapter.extract(reference)
    monkeypatch.setattr(adapter, "extraction_version", "pdf-next")
    second = adapter.extract(reference)
    assert second.extraction.extraction_version == "pdf-next"
    assert second.document_id != first.document_id
    assert {section.section_id for section in first.sections}.isdisjoint(
        section.section_id for section in second.sections
    )
    assert [section.text for section in first.sections] == [section.text for section in second.sections]
