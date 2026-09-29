"""Source adapters and the default resolver."""

from __future__ import annotations

from pathlib import Path

from ..domain.contracts import SourceAdapter
from ..domain.errors import SourceUnsupported
from .pdf_source import PdfSourceAdapter
from .text_source import SUPPORTED_TEXT_SUFFIXES, TextSourceAdapter

__all__ = [
    "DefaultSourceResolver",
    "PdfSourceAdapter",
    "TextSourceAdapter",
    "SUPPORTED_TEXT_SUFFIXES",
]


class DefaultSourceResolver:
    """Selects a concrete adapter from the explicit user path extension."""

    def __init__(self, useful_page_min_chars: int = 500) -> None:
        self._pdf = PdfSourceAdapter(useful_page_min_chars=useful_page_min_chars)
        self._text = TextSourceAdapter()

    def resolve(self, path: str) -> SourceAdapter:
        suffix = Path(path).suffix.lower()
        if suffix == ".pdf":
            return self._pdf
        if suffix in SUPPORTED_TEXT_SUFFIXES:
            return self._text
        raise SourceUnsupported(
            f"Unsupported source format: {suffix or 'unknown'}.",
        )
