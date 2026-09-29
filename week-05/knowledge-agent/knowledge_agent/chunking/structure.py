"""Structure-aware chunker: paragraph packing inside real section boundaries."""

from __future__ import annotations

import re
from typing import Any

from ..domain.contracts import CORPUS_SCHEMA_VERSION, Chunker, Tokenizer
from ..domain.models import Chunk, Document
from .common import build_chunk

_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")


class StructureChunker(Chunker):
    strategy = "structure"

    def __init__(
        self,
        tokenizer: Tokenizer,
        max_tokens: int = 800,
        min_tokens: int = 64,
        overlap: int = 0,
        max_chars: int | None = None,
        corpus_schema_version: str = CORPUS_SCHEMA_VERSION,
    ) -> None:
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if min_tokens < 0:
            raise ValueError("min_tokens must be non-negative")
        if overlap != 0:
            raise ValueError("structure strategy does not use overlap")
        self._tokenizer = tokenizer
        self.max_tokens = max_tokens
        self.min_tokens = min_tokens
        self.overlap = overlap
        self.max_chars = max_chars if max_chars is not None else 4 * max_tokens
        self.corpus_schema_version = corpus_schema_version

    @property
    def params(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "unit": self._tokenizer.unit,
            "tokenizer": getattr(self._tokenizer, "name", "lexical-v1"),
            "max_tokens": self.max_tokens,
            "min_tokens": self.min_tokens,
            "overlap": self.overlap,
            "max_chars": self.max_chars,
            "corpus_schema_version": self.corpus_schema_version,
        }

    def chunk(self, document: Document) -> list[Chunk]:
        chunks: list[Chunk] = []
        for section in document.sections:
            units = self._section_units(section.text or "")
            texts = self._pack(units)
            for text in texts:
                chunks.append(
                    build_chunk(
                        text=text,
                        document=document,
                        corpus_schema_version=self.corpus_schema_version,
                        strategy=self.strategy,
                        params=self.params,
                        section_path=section.section_path,
                        ordinal=len(chunks),
                        page_start=section.page_start,
                        page_end=section.page_end,
                        section_level=section.level,
                        section_role=section.role,
                    )
                )
        return chunks

    def _section_units(self, text: str) -> list[str]:
        paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT_RE.split(text or "")]
        units: list[str] = []
        for paragraph in paragraphs:
            if not paragraph:
                continue
            units.extend(self._split_long(paragraph))
        return units

    def _split_long(self, text: str) -> list[str]:
        """Split a single paragraph so no unit exceeds the token/char budget."""

        if self._token_count(text) <= self.max_tokens and len(text) <= self.max_chars:
            return [text]

        spans = self._tokenizer.tokenize_spans(text)
        if not spans:
            return [text]
        parts: list[str] = []
        start_index = 0
        while start_index < len(spans):
            end_index = min(start_index + self.max_tokens, len(spans))
            piece = text[spans[start_index][1] : spans[end_index - 1][2]]
            parts.extend(self._split_by_chars(piece))
            start_index = end_index
        return [p for p in parts if p.strip()]

    def _split_by_chars(self, text: str) -> list[str]:
        if len(text) <= self.max_chars:
            return [text.strip()]
        parts: list[str] = []
        remaining = text
        while len(remaining) > self.max_chars:
            cut = remaining.rfind(" ", 0, self.max_chars)
            if cut <= 0:
                cut = self.max_chars
            parts.append(remaining[:cut].strip())
            remaining = remaining[cut:].lstrip()
        if remaining.strip():
            parts.append(remaining.strip())
        return parts

    def _pack(self, units: list[str]) -> list[str]:
        if not units:
            return []
        chunks: list[str] = []
        buffer: list[str] = []
        buffer_tokens = 0
        buffer_chars = 0
        for unit in units:
            unit_tokens = self._token_count(unit)
            projected_chars = buffer_chars + len(unit) + (2 if buffer else 0)
            if buffer and (
                buffer_tokens + unit_tokens > self.max_tokens
                or projected_chars > self.max_chars
            ):
                chunks.append("\n\n".join(buffer))
                buffer, buffer_tokens, buffer_chars = [], 0, 0
            buffer.append(unit)
            buffer_tokens += unit_tokens
            buffer_chars += len(unit) + (2 if len(buffer) > 1 else 0)
        if buffer:
            chunks.append("\n\n".join(buffer))

        # Merge a too-small trailing chunk back into its predecessor when it fits.
        if len(chunks) >= 2:
            last_tokens = self._token_count(chunks[-1])
            if last_tokens < self.min_tokens:
                merged = chunks[-2] + "\n\n" + chunks[-1]
                if (
                    self._token_count(merged) <= self.max_tokens
                    and len(merged) <= self.max_chars
                ):
                    chunks = chunks[:-2] + [merged]
        return chunks

    def _token_count(self, text: str) -> int:
        return len(self._tokenizer.tokenize(text))
