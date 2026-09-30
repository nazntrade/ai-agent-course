"""TXT/Markdown source adapter (the small second contract-check source)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..domain.contracts import SourceAdapter
from ..domain.errors import SourceEmpty, SourceInvalid, SourceUnreadable, SourceUnsupported
from ..domain.models import Document, ExtractionInfo, Section, SourceRef
from ..text.normalize import detect_language, normalize_text

EXTRACTION_VERSION = "text-v1"
SUPPORTED_TEXT_SUFFIXES = {".txt", ".text", ".md", ".markdown"}

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


class TextSourceAdapter(SourceAdapter):
    kind = "text"
    extraction_version = EXTRACTION_VERSION

    def effective_extraction_version(self, source_ref: SourceRef) -> str:
        mode = "markdown" if Path(source_ref.uri).suffix.lower() in {".md", ".markdown"} else "plain"
        return f"{self.extraction_version}:{mode}"

    def extract(self, source_ref: SourceRef) -> Document:
        path = Path(source_ref.uri)
        if path.suffix.lower() not in SUPPORTED_TEXT_SUFFIXES:
            raise SourceUnsupported(f"Unsupported text source: {source_ref.label}.")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise SourceUnreadable(f"Source could not be read: {source_ref.label}.") from exc
        if hashlib.sha256(data).hexdigest() != source_ref.content_sha256:
            raise SourceInvalid(
                f"Source changed while reading: {source_ref.label}.",
            )
        if not data:
            raise SourceEmpty(f"Source is empty: {source_ref.label}.")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            try:
                text = data.decode("cp1251")
            except UnicodeDecodeError:
                raise SourceInvalid(
                    f"Source is not valid UTF-8 text: {source_ref.label}."
                ) from exc
        if not text.strip():
            raise SourceEmpty(f"Source is empty: {source_ref.label}.")

        sections = self._sections(text, source_ref)
        if not any(section.text.strip() for section in sections):
            raise SourceEmpty(f"Source contains no usable text: {source_ref.label}.")

        extraction_version = self.effective_extraction_version(source_ref)
        document_id = _document_id(source_ref, extraction_version)
        extraction = ExtractionInfo(
            extraction_version=extraction_version,
            adapter="text",
            page_count=None,
            useful_pages=None,
            useful_chars=len(text),
            language=detect_language(text),
        )
        normalized = "\n\n".join(section.text for section in sections if section.text)
        return Document(
            document_id=document_id,
            source=source_ref,
            extraction=extraction,
            sections=sections,
            normalized_markdown=normalized,
        )

    def _sections(self, text: str, source_ref: SourceRef) -> list[Section]:
        version = self.effective_extraction_version(source_ref)
        if Path(source_ref.uri).suffix.lower() in {".md", ".markdown"}:
            return _markdown_sections(text, source_ref, version)
        body = normalize_text(text)
        return [
            Section(
                section_id=_section_id(source_ref, "Document", 0, version),
                section_path="Document",
                level=1,
                role="body",
                page_start=None,
                page_end=None,
                text=body,
            )
        ]


def _markdown_sections(text: str, source_ref: SourceRef, version: str) -> list[Section]:
    sections: list[Section] = []
    current_path = "Document"
    current_level = 1
    buffer: list[str] = []
    index = 0

    def flush() -> None:
        nonlocal index
        body = normalize_text("\n".join(buffer))
        if body:
            sections.append(
                Section(
                    section_id=_section_id(source_ref, current_path, index, version),
                    section_path=current_path,
                    level=current_level,
                    role=_role_for(current_path),
                    page_start=None,
                    page_end=None,
                    text=body,
                )
            )
            index += 1

    for line in text.splitlines():
        match = _HEADING_RE.match(line.strip())
        if match:
            flush()
            buffer = []
            current_level = len(match.group(1))
            current_path = match.group(2).strip() or "Document"
        else:
            buffer.append(line)
    flush()

    if not sections:
        sections.append(
            Section(
                section_id=_section_id(source_ref, "Document", 0, version),
                section_path="Document",
                level=1,
                role="body",
                page_start=None,
                page_end=None,
                text=normalize_text(text),
            )
        )
    return sections


def _role_for(path: str) -> str:
    lowered = path.lower()
    if "reference" in lowered or "bibliograph" in lowered:
        return "references"
    if "appendix" in lowered:
        return "appendix"
    return "body"


def _document_id(source_ref: SourceRef, extraction_version: str) -> str:
    return hashlib.sha256(
        f"{source_ref.source_id}:{extraction_version}".encode("utf-8")
    ).hexdigest()[:32]


def _section_id(source_ref: SourceRef, path: str, index: int, version: str) -> str:
    payload = f"{source_ref.source_id}:{version}:{path}:{index}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
