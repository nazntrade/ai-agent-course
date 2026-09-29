"""Normalization helpers: NFC, ligatures, hyphenation and Markdown rendering."""

from __future__ import annotations

import re
import unicodedata

from ..domain.models import Document

# Version of the text normalization pipeline; bump when the algorithm changes so
# fingerprints and index compatibility invalidate stale chunks (SPEC 6.2/9.5/10).
NORMALIZATION_VERSION = "norm-v1"
NORMALIZATION_DETAIL = "nfc+ligatures+dehyphenation"

_LIGATURES = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\ufb05": "st",
    "\ufb06": "st",
    "\u0153": "oe",
    "\u0152": "OE",
    "\u00e6": "ae",
    "\u00c6": "AE",
}

# Hyphen at end of a line followed by a lowercase word start (SPEC 17.1).
_HYPHEN_BREAK_RE = re.compile(r"(\w+)-\s*\n\s*([a-z\u0430-\u044f])")
_INLINE_WS_RE = re.compile(r"[ \t\u00a0]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    """NFC, ligature expansion, hyphenation repair and whitespace cleanup."""

    if not text:
        return ""
    for source, replacement in _LIGATURES.items():
        text = text.replace(source, replacement)
    text = unicodedata.normalize("NFC", text)
    text = dehyphenate(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_INLINE_WS_RE.sub(" ", line).rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    return text.strip()


def dehyphenate(text: str) -> str:
    """Join words split across line breaks by a trailing hyphen."""

    previous = None
    while previous != text:
        previous = text
        text = _HYPHEN_BREAK_RE.sub(r"\1\2", text)
    return text


def detect_language(text: str) -> str:
    """Very small deterministic heuristic used for metadata only."""

    sample = text[:4000]
    letters = [ch for ch in sample if ch.isalpha()]
    if not letters:
        return "unknown"
    cyrillic = sum(1 for ch in letters if "\u0400" <= ch <= "\u04ff")
    if cyrillic / len(letters) > 0.3:
        return "ru"
    return "en"


def render_markdown(document: Document) -> str:
    """Render a document to normalized Markdown while keeping provenance.

    Used when the PDF structure is unreliable; chunking still runs on the
    sections, and this method is a readable, checkable representation.
    """

    lines: list[str] = []
    for section in document.sections:
        level = max(1, min(6, section.level or 1))
        if section.section_path:
            lines.append("#" * level + " " + section.section_path)
        body = normalize_text(section.text)
        if body:
            lines.append(body)
        lines.append("")
    return "\n".join(lines).strip() + "\n"
