"""Two-column PDF reading order regression (D21-08/D21-13, extraction pdf-v3)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from knowledge_agent.domain.models import SourceRef
from knowledge_agent.sources.pdf_source import PdfSourceAdapter

from tests.fixtures.mini_pdf import (
    build_hyphen_overlay_pdf_bytes,
    build_two_column_pdf_bytes,
    write_mini_pdf,
)


def _ref_for(path: Path, kind: str = "pdf") -> SourceRef:
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


def _combined_text(path: Path) -> str:
    document = PdfSourceAdapter().extract(_ref_for(path))
    return "\n\n".join(section.text for section in document.sections)


def test_two_column_left_reads_before_right_and_full_width_stays(tmp_path):
    rows = [
        ("columns", "left alpha token", "right bravo token"),
        ("columns", "left charlie token", "right delta token"),
        ("columns", "left echo token", "right foxtrot token"),
        ("columns", "left golf token", "right hotel token"),
        ("full", "Figure 1 A caption spanning the entire width of both columns"),
        ("columns", "left india token", "right juliet token"),
        ("columns", "left kilo token", "right lima token"),
        ("columns", "left mike token", "right november token"),
        ("columns", "left oscar token", "right papa token"),
    ]
    path = tmp_path / "two_column.pdf"
    path.write_bytes(build_two_column_pdf_bytes(rows))
    text = _combined_text(path)

    # (a)+(г): every column token is present, and each band reads the whole
    # left column before the right one (no cross-column interleaving or gluing).
    expected_order = [
        "left alpha token",
        "left charlie token",
        "left echo token",
        "left golf token",
        "right bravo token",
        "right delta token",
        "right foxtrot token",
        "right hotel token",
        "Figure 1",
        "left india token",
        "left kilo token",
        "left mike token",
        "left oscar token",
        "right juliet token",
        "right lima token",
        "right november token",
        "right papa token",
    ]
    positions = [text.index(item) for item in expected_order]
    assert positions == sorted(positions)
    assert "tokenright" not in text
    assert "tokenleft" not in text

    # (б): the full-width caption is its own paragraph, not inside a column.
    caption_paragraphs = [
        paragraph for paragraph in text.split("\n\n") if "Figure 1" in paragraph
    ]
    assert len(caption_paragraphs) == 1
    assert "left " not in caption_paragraphs[0]
    assert "right " not in caption_paragraphs[0]


def test_two_column_restores_hyphen_across_line_break(tmp_path):
    rows = [
        ("columns", "The architec-", "right one separate text"),
        ("columns", "ture design is restored", "right two separate text"),
        ("columns", "more left body words", "right three separate text"),
        ("columns", "even left body words", "right four separate text"),
        ("columns", "final left body words", "right five separate text"),
        ("columns", "closing left body words", "right six separate text"),
    ]
    path = tmp_path / "hyphen.pdf"
    path.write_bytes(build_two_column_pdf_bytes(rows))
    text = _combined_text(path)

    # (в): the word split by the line-break hyphen is sewn back together.
    assert "architecture design is restored" in text
    assert "architec- ture" not in text
    # The right column is still read after the left column.
    assert text.index("closing left body words") < text.index("right one separate text")


def test_hyphen_does_not_glue_raised_intermediate_line(tmp_path):
    # (е): a word overlapped by an intermediate text-layer element must be
    # joined with its real continuation, not with the intermediate word.
    path = tmp_path / "hyphen_overlay.pdf"
    path.write_bytes(build_hyphen_overlay_pdf_bytes())
    text = _combined_text(path)

    assert "capability to complete different tasks." in text
    assert "capabildifferent" not in text
    # The intermediate word is preserved as its own token, never dropped.
    assert "different" in text
    assert "capabil- different" not in text


def test_single_column_pdf_order_is_preserved(tmp_path):
    # (д): a single-column page keeps its original top-down order.
    path = write_mini_pdf(
        tmp_path / "single.pdf",
        [
            "First single column line.",
            "Second single column line.",
            "Third single column line.",
            "Fourth single column line.",
        ],
    )
    text = _combined_text(path)
    order = [
        text.index("First single column line."),
        text.index("Second single column line."),
        text.index("Third single column line."),
        text.index("Fourth single column line."),
    ]
    assert order == sorted(order)
