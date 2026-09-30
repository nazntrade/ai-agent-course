"""Text-layer PDF source adapter (pdfplumber with pypdfium2 fallback)."""

from __future__ import annotations

import hashlib
import io
import re
from collections import Counter
from pathlib import Path

from ..domain.contracts import SourceAdapter
from ..domain.errors import (
    SourceEmpty,
    SourceEncrypted,
    SourceInvalid,
    SourceNoTextLayer,
    SourceUnreadable,
)
from ..domain.models import Document, ExtractionInfo, Section, SourceRef
from ..text.normalize import detect_language, render_markdown
from .pdf_layout import (
    Paragraph,
    group_lines,
    join_paragraphs,
    order_lines,
    strip_headers_footers,
    words_from_pdfplumber,
    words_from_pypdfium,
)

EXTRACTION_VERSION = "pdf-v3"
_NUMBERED_HEADING_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,4})\s+([A-Z][^\n]{0,120})$")


class PdfSourceAdapter(SourceAdapter):
    kind = "pdf"
    extraction_version = EXTRACTION_VERSION

    def __init__(self, useful_page_min_chars: int = 500) -> None:
        self.useful_page_min_chars = useful_page_min_chars

    def extract(self, source_ref: SourceRef) -> Document:
        path = Path(source_ref.uri)
        if path.suffix.lower() != ".pdf":
            raise SourceInvalid(f"Unsupported PDF source: {source_ref.label}.")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise SourceUnreadable(f"Source could not be read: {source_ref.label}.") from exc
        if hashlib.sha256(data).hexdigest() != source_ref.content_sha256:
            raise SourceInvalid(f"Source changed while reading: {source_ref.label}.")
        if not data:
            raise SourceEmpty(f"Source is empty: {source_ref.label}.")

        warnings: list[str] = []
        pages_lines, page_heights, page_widths = self._extract_layout(data, source_ref)
        page_count = len(pages_lines)

        cleaned, removed = strip_headers_footers(pages_lines, page_heights)
        if removed:
            warnings.append(f"removed_repeated_headers_footers:{removed}")

        ordered_pages = [
            order_lines(lines, width) for lines, width in zip(cleaned, page_widths)
        ]
        all_sizes = [
            line.size for page in ordered_pages for line in page if line.size > 0
        ]
        body_size = _dominant_size(all_sizes)

        units: list[tuple[int | None, Paragraph]] = []
        for page_lines in ordered_pages:
            units.extend(_page_units(page_lines, body_size))

        paragraphs = [paragraph for _, paragraph in units]
        total_chars = sum(len(p.text) for p in paragraphs)
        if total_chars < 20:
            raise SourceNoTextLayer(
                f"The PDF has no extractable text layer: {source_ref.label}."
            )

        per_page_chars = Counter()
        for paragraph in paragraphs:
            per_page_chars[paragraph.page] += len(paragraph.text)
        useful_pages = sum(
            1
            for page in range(1, page_count + 1)
            if per_page_chars.get(page, 0) >= self.useful_page_min_chars
        )
        warnings.append(f"useful_pages:{useful_pages}/{page_count}")

        sections, structure_warning = _build_sections(units, source_ref, self.extraction_version)
        if structure_warning:
            warnings.append(structure_warning)

        all_text = "\n\n".join(p.text for p in paragraphs)
        document_id = _document_id(source_ref, self.extraction_version)
        extraction = ExtractionInfo(
            extraction_version=self.extraction_version,
            adapter="pdf",
            page_count=page_count,
            useful_pages=useful_pages,
            useful_chars=total_chars,
            language=detect_language(all_text),
            warnings=warnings,
        )
        document = Document(
            document_id=document_id,
            source=source_ref,
            extraction=extraction,
            sections=sections,
            normalized_markdown=None,
        )
        document.normalized_markdown = render_markdown(document)
        return document

    # -- extraction -------------------------------------------------------
    def _extract_layout(
        self, data: bytes, source_ref: SourceRef
    ) -> tuple[list[list], list[float], list[float]]:
        try:
            return _extract_with_pdfplumber(data)
        except Exception as exc:  # noqa: BLE001 - fall back to pypdfium2
            if _is_password_error(exc):
                raise SourceEncrypted(
                    f"The PDF is encrypted: {source_ref.label}."
                ) from exc
            first_error = exc
        try:
            return _extract_with_pypdfium(data)
        except Exception as exc:  # noqa: BLE001 - both backends failed
            if _is_password_error(exc):
                raise SourceEncrypted(
                    f"The PDF is encrypted: {source_ref.label}."
                ) from exc
            raise SourceInvalid(
                f"The PDF could not be parsed: {source_ref.label}."
            ) from first_error


def _extract_with_pdfplumber(data: bytes):
    import pdfplumber

    pages_lines: list[list] = []
    page_heights: list[float] = []
    page_widths: list[float] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            words = words_from_pdfplumber(page, number)
            pages_lines.append(group_lines(words))
            page_heights.append(float(page.height))
            page_widths.append(float(page.width))
    if not pages_lines:
        raise SourceInvalid("The PDF contains no pages.")
    return pages_lines, page_heights, page_widths


def _extract_with_pypdfium(data: bytes):
    import pypdfium2 as pdfium

    pages_lines: list[list] = []
    page_heights: list[float] = []
    page_widths: list[float] = []
    document = pdfium.PdfDocument(io.BytesIO(data))
    try:
        for index in range(len(document)):
            page = document[index]
            words = words_from_pypdfium(page, index + 1)
            pages_lines.append(group_lines(words))
            page_heights.append(float(page.get_height()))
            page_widths.append(float(page.get_width()))
    finally:
        document.close()
    if not pages_lines:
        raise SourceInvalid("The PDF contains no pages.")
    return pages_lines, page_heights, page_widths


def _page_units(
    lines: list, body_size: float
) -> list[tuple[int | None, Paragraph]]:
    """Split ordered lines into heading units and joined body paragraphs."""

    units: list[tuple[int | None, Paragraph]] = []
    buffer: list = []

    def flush_buffer() -> None:
        for paragraph in join_paragraphs(buffer):
            units.append((None, paragraph))
        buffer.clear()

    for line in lines:
        role_heading = _role_heading(line.text)
        if role_heading:
            flush_buffer()
            label, remainder = role_heading
            units.append(
                (
                    1,
                    Paragraph(
                        text=label,
                        page=line.page,
                        size=line.size,
                        bbox=[line.x0, line.top, line.x1, line.bottom],
                    ),
                )
            )
            # The heading often shares its line with the first reference item.
            if remainder:
                units.append(
                    (
                        None,
                        Paragraph(
                            text=remainder,
                            page=line.page,
                            size=line.size,
                            bbox=[line.x0, line.top, line.x1, line.bottom],
                        ),
                    )
                )
            continue
        level = _heading_level(line.text, line.size, body_size)
        if level:
            flush_buffer()
            units.append(
                (
                    level,
                    Paragraph(
                        text=line.text.strip(),
                        page=line.page,
                        size=line.size,
                        bbox=[line.x0, line.top, line.x1, line.bottom],
                    ),
                )
            )
        else:
            buffer.append(line)
    flush_buffer()
    return units


_ROLE_HEADINGS = {
    "references": "References",
    "bibliography": "Bibliography",
    "acknowledgments": "Acknowledgments",
    "acknowledgements": "Acknowledgements",
}

_REFERENCE_ENTRY_RE = re.compile(r"^(?:\[\d+\]|\d{1,3}[.)])\s+[A-Z\[]")


def _role_heading(text: str) -> tuple[str, str | None] | None:
    """Recognize a role heading even when body text follows on the same line.

    A shared line is accepted only when it is short or its remainder looks like
    a reference entry; a body sentence starting with "References …" is not
    silently turned into a section (and dropped).
    """

    stripped = text.strip()
    lowered = stripped.lower()
    for key, label in _ROLE_HEADINGS.items():
        if lowered == key:
            return label, None
        if (
            lowered.startswith(key)
            and len(stripped) > len(key)
            and stripped[len(key)].isspace()
        ):
            remainder = stripped[len(key) :].strip()
            if len(stripped) <= 40 or _REFERENCE_ENTRY_RE.match(remainder):
                return label, remainder
            return None
    return None


def _build_sections(
    units: list[tuple[int | None, Paragraph]], source_ref: SourceRef,
    extraction_version: str = EXTRACTION_VERSION,
) -> tuple[list[Section], str | None]:
    sections: list[dict] = []
    current: dict | None = None

    def start(path: str, level: int, paragraph: Paragraph) -> dict:
        return {
            "path": path,
            "level": level,
            "page_start": paragraph.page,
            "page_end": paragraph.page,
            "texts": [],
            "bbox": list(paragraph.bbox),
        }

    for level, paragraph in units:
        if level:
            current = start(paragraph.text.strip(), level, paragraph)
            sections.append(current)
            continue
        if current is None:
            current = start("Front matter", 0, paragraph)
            sections.append(current)
        current["texts"].append(paragraph.text)
        current["page_end"] = paragraph.page

    warning = None
    if not sections:
        return [], "no_structure_detected"
    has_heading = any(section["level"] > 0 for section in sections)
    if not has_heading:
        for section in sections:
            section["path"] = Path(source_ref.label).stem or "Document"
        warning = "structure_fallback_single_section"

    result: list[Section] = []
    for index, section in enumerate(sections):
        text = "\n\n".join(section["texts"]).strip()
        if not text:
            continue
        path = section["path"] or f"Section {index + 1}"
        result.append(
            Section(
                section_id=_section_id(source_ref, path, index, extraction_version),
                section_path=path,
                level=max(1, section["level"]),
                role=_role_for(path),
                start_bbox=section["bbox"],
                page_start=section["page_start"],
                page_end=section["page_end"],
                text=text,
            )
        )
    if not result:
        raise SourceNoTextLayer("The PDF produced no sections.")
    return result, warning


def _heading_level(text: str, size: float, body_size: float):
    stripped = text.strip()
    if not stripped or len(stripped) < 3 or len(stripped) > 120:
        return None
    if "(cid:" in stripped:
        return None
    words = stripped.split()
    letters = sum(1 for ch in stripped if ch.isalpha())
    alpha_ratio = letters / len(stripped)
    if stripped.lower() in {
        "references",
        "bibliography",
        "acknowledgments",
        "acknowledgements",
    }:
        return 1
    match = _NUMBERED_HEADING_RE.match(stripped)
    if match and not stripped.endswith((".", ",")):
        title = match.group(2).strip()
        if len(title.split()) <= 12 and not re.search(r",\s*(19|20)\d{2}\b", title):
            return match.group(1).count(".") + 1
    if re.match(r"^\d{3,}", stripped):
        # Author/affiliation or reference lines starting with a long number.
        return None
    if (
        body_size > 0
        and size >= body_size * 1.3
        and len(stripped) < 80
        and 2 <= len(words) <= 12
        and alpha_ratio >= 0.55
        and "[" not in stripped
        and not stripped.endswith((".", ",", ":", ";"))
    ):
        return 1
    if (
        stripped.isupper()
        and letters >= 3
        and 6 <= len(stripped) < 80
        and 2 <= len(words) <= 10
        and alpha_ratio >= 0.6
        and len(set(words)) > 1
    ):
        return 1
    return None


def _role_for(path: str) -> str:
    lowered = path.lower()
    if "reference" in lowered or "bibliograph" in lowered:
        return "references"
    if "appendix" in lowered:
        return "appendix"
    return "body"


def _dominant_size(values: list[float]) -> float:
    """Most common (rounded) font size; best represents body text."""

    if not values:
        return 0.0
    counts: dict[float, int] = {}
    for value in values:
        key = round(value * 2) / 2
        counts[key] = counts.get(key, 0) + 1
    return max(counts.items(), key=lambda item: item[1])[0]


def _document_id(source_ref: SourceRef, extraction_version: str = EXTRACTION_VERSION) -> str:
    return hashlib.sha256(
        f"{source_ref.source_id}:{extraction_version}".encode("utf-8")
    ).hexdigest()[:32]


def _section_id(source_ref: SourceRef, path: str, index: int, extraction_version: str) -> str:
    payload = f"{source_ref.source_id}:{extraction_version}:{path}:{index}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _is_password_error(exc: Exception) -> bool:
    return "password" in str(exc).lower() or "encrypted" in str(exc).lower()
