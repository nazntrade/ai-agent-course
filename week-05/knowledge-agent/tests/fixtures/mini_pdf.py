"""Generate tiny valid single-page PDFs with a text layer.

Avoids committing a binary fixture and needs no third-party PDF writer.
Two builders are provided:

* :func:`build_pdf_bytes` - normal text with explicit spaces between words;
* :func:`build_kerning_pdf_bytes` - words separated only by small positive
  kerning gaps (< 3pt, no space glyphs), which mimics tightly-set PDFs.
"""

from __future__ import annotations

from pathlib import Path


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _assemble_pdf(content: bytes) -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
            b" /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n"
        + content
        + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


def build_pdf_bytes(lines: list[str], font_size: int = 12) -> bytes:
    content_lines = ["BT", f"/F1 {font_size} Tf", f"{font_size} TL", "72 720 Td"]
    for line in lines:
        content_lines.append(f"({_escape(line)}) Tj")
        content_lines.append("T*")
    content_lines.append("ET")
    content = ("\n".join(content_lines) + "\n").encode("latin-1")
    return _assemble_pdf(content)


def build_kerning_pdf_bytes(
    lines: list[str], font_size: int = 10, gap_pt: float = 2.8
) -> bytes:
    """Words are placed with no space glyph and a kerning gap of ``gap_pt``."""

    kern = int(round(gap_pt / font_size * 1000))
    content_lines = ["BT", f"/F1 {font_size} Tf", f"{font_size} TL", "72 720 Td"]
    for line in lines:
        parts: list[str] = []
        for word in line.split():
            if parts:
                parts.append(f"-{kern}")
            parts.append(f"({_escape(word)})")
        content_lines.append("[" + " ".join(parts) + "] TJ")
        content_lines.append("T*")
    content_lines.append("ET")
    content = ("\n".join(content_lines) + "\n").encode("latin-1")
    return _assemble_pdf(content)


def write_mini_pdf(path: str | Path, lines: list[str] | None = None) -> Path:
    target = Path(path)
    target.write_bytes(
        build_pdf_bytes(
            lines
            or [
                "Memory stores observations for autonomous agents.",
                "Planning decomposes goals into executable steps.",
                "Tool use connects the agent to external services.",
                "Retrieval augments the agent with external knowledge.",
            ]
        )
    )
    return target


def write_kerning_pdf(
    path: str | Path, lines: list[str] | None = None
) -> Path:
    target = Path(path)
    target.write_bytes(
        build_kerning_pdf_bytes(
            lines
            or [
                "1 Introduction",
                "Memory stores observations",
                "Supercalifragilisticexpialidocious retrieval",
            ]
        )
    )
    return target


def build_tracking_pdf_bytes(
    lines: list[str], font_size: int = 10, letter_gap_pt: float = 0.8
) -> bytes:
    """Words are letter-spaced (tracked); letters must not be split apart."""

    kern = int(round(letter_gap_pt / font_size * 1000))
    content_lines = ["BT", f"/F1 {font_size} Tf", f"{font_size} TL", "72 720 Td"]
    for line in lines:
        parts: list[str] = []
        for word_index, word in enumerate(line.split()):
            if word_index:
                parts.append("( )")
            for char_index, char in enumerate(word):
                if char_index:
                    parts.append(f"-{kern}")
                parts.append(f"({_escape(char)})")
        content_lines.append("[" + " ".join(parts) + "] TJ")
        content_lines.append("T*")
    content_lines.append("ET")
    content = ("\n".join(content_lines) + "\n").encode("latin-1")
    return _assemble_pdf(content)


def write_tracking_pdf(
    path: str | Path, lines: list[str] | None = None
) -> Path:
    target = Path(path)
    target.write_bytes(
        build_tracking_pdf_bytes(lines or ["1 Introduction", "Tracking"])
    )
    return target


def build_placed_text_pdf_bytes(
    runs: list[tuple], font_size: int = 12
) -> bytes:
    """Place every ``(x, y, text[, size])`` run at an explicit position.

    Unlike :func:`build_pdf_bytes` this keeps both axes under caller control,
    which is required to reproduce a word whose box is taller than the rest of
    its line (an overlaid text-layer element).
    """

    content_lines = ["BT", f"/F1 {font_size} Tf"]
    for run in runs:
        x, y, text = run[0], run[1], run[2]
        run_size = run[3] if len(run) > 3 else None
        if run_size is not None:
            content_lines.append(f"/F1 {run_size} Tf")
        content_lines.append(f"1 0 0 1 {x:.1f} {y:.1f} Tm")
        content_lines.append(f"({_escape(text)}) Tj")
    content_lines.append("ET")
    content = ("\n".join(content_lines) + "\n").encode("latin-1")
    return _assemble_pdf(content)


def build_hyphen_overlay_pdf_bytes(
    font_size: int = 10, intermediate_size: int = 16
) -> bytes:
    """Hyphenated word split by an intermediate text-layer element.

    ``capabil-`` ends one line and ``ity ...`` continues on the next, but the
    word ``different`` sits between them on the continuation baseline while
    being rendered a little larger. Its box is therefore taller than the line's
    (top edge well above, bottom edge near the baseline), exactly as the real
    survey PDF overlays that element. A top-only line grouping detaches
    ``different`` into its own line and glues it into ``capabil-``; a
    baseline-aware grouping keeps it on its continuation line and the word is
    joined correctly.
    """

    runs = [
        (72.0, 720.0, "Left body begins here and ends with capabil-"),
        (72.0, 700.0, "ity to complete"),
        (220.0, 700.0, "different", intermediate_size),
        (380.0, 700.0, "tasks."),
    ]
    return build_placed_text_pdf_bytes(runs, font_size=font_size)


def build_two_column_pdf_bytes(
    rows: list[tuple],
    font_size: int = 10,
    line_height: float = 16.0,
    top: float = 720.0,
    left_x: float = 60.0,
    right_x: float = 330.0,
) -> bytes:
    """Build a single page with a two-column body and full-width rows.

    ``rows`` entries are either ``("full", text)`` for a row spanning the page
    width or ``("columns", left_text, right_text)`` for a row whose left and
    right cells share one baseline. Sharing the baseline reproduces the layout
    that a vertical-position-only grouping merges across the gutter.
    """

    content_lines = ["BT", f"/F1 {font_size} Tf"]
    y = top
    for row in rows:
        if row[0] == "full":
            placements = [(left_x, row[1])]
        else:
            placements = [(left_x, row[1]), (right_x, row[2])]
        for x, text in placements:
            content_lines.append(f"1 0 0 1 {x:.1f} {y:.1f} Tm")
            content_lines.append(f"({_escape(text)}) Tj")
        y -= line_height
    content_lines.append("ET")
    content = ("\n".join(content_lines) + "\n").encode("latin-1")
    return _assemble_pdf(content)

