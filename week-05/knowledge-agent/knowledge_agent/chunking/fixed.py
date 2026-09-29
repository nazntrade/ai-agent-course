"""Fixed-size sliding-window chunker (500 tokens, 75 overlap by default)."""

from __future__ import annotations

from typing import Any

from ..domain.contracts import CORPUS_SCHEMA_VERSION, Chunker, Tokenizer
from ..domain.models import Chunk, Document, Section
from .common import build_chunk, build_flat, sections_for


class FixedChunker(Chunker):
    strategy = "fixed"

    def __init__(
        self,
        tokenizer: Tokenizer,
        chunk_size: int = 500,
        overlap: int = 75,
        corpus_schema_version: str = CORPUS_SCHEMA_VERSION,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if overlap < 0 or overlap >= chunk_size:
            raise ValueError("overlap must be in [0, chunk_size)")
        self._tokenizer = tokenizer
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.corpus_schema_version = corpus_schema_version

    @property
    def params(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "unit": self._tokenizer.unit,
            "tokenizer": getattr(self._tokenizer, "name", "lexical-v1"),
            "chunk_size": self.chunk_size,
            "overlap": self.overlap,
            "corpus_schema_version": self.corpus_schema_version,
        }

    def chunk(self, document: Document) -> list[Chunk]:
        flat, ranges = build_flat(document)
        spans = self._tokenizer.tokenize_spans(flat)
        if not spans:
            return []

        size = self.chunk_size
        overlap = min(self.overlap, size - 1)
        step = size - overlap
        total = len(spans)

        windows: list[tuple[int, int]] = []
        i = 0
        while i < total:
            window = spans[i : i + size]
            windows.append((window[0][1], window[-1][2]))
            nxt = i + step
            if nxt >= total or total - nxt <= overlap:
                break
            i = nxt

        chunks: list[Chunk] = []
        for ordinal, (start, end) in enumerate(windows):
            text = flat[start:end].strip()
            if not text:
                continue
            overlapping = sections_for(start, end, ranges)
            if overlapping:
                primary = overlapping[0]
                section_path = primary.section_path
                level = primary.level
                role = primary.role
                page_start, page_end = _page_range(overlapping)
            else:
                section_path, level, role = "Document", 1, "body"
                page_start = page_end = None
            chunk = build_chunk(
                    text=text,
                    document=document,
                    corpus_schema_version=self.corpus_schema_version,
                    strategy=self.strategy,
                    params=self.params,
                    section_path=section_path,
                    ordinal=len(chunks),
                    page_start=page_start,
                    page_end=page_end,
                    section_level=level,
                    section_role=role,
                )
            chunk.crosses_sections = len({s.section_path for s in overlapping}) > 1
            chunks.append(chunk)
        return chunks


def _page_range(sections: list[Section]) -> tuple[int | None, int | None]:
    starts = [s.page_start for s in sections if s.page_start is not None]
    ends = [s.page_end for s in sections if s.page_end is not None]
    if not starts and not ends:
        return None, None
    return (min(starts) if starts else None, max(ends) if ends else None)
