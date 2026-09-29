"""Shared chunking helpers: flat layout, stable chunk ids and metadata."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence

from ..domain.models import Chunk, ChunkMetadata, Document, Section
from ..text.tokenizer import count_tokens


def build_flat(document: Document) -> tuple[str, list[tuple[int, int, Section]]]:
    """Concatenate section texts and keep ``(start, end, section)`` ranges."""

    parts: list[str] = []
    ranges: list[tuple[int, int, Section]] = []
    offset = 0
    for section in document.sections:
        text = section.text or ""
        if parts:
            # Section separator so tokens from neighbouring sections never fuse.
            parts.append("\n\n")
            offset += 2
        start = offset
        parts.append(text)
        offset += len(text)
        ranges.append((start, offset, section))
    return "".join(parts), ranges


def sections_for(
    start: int, end: int, ranges: Sequence[tuple[int, int, Section]]
) -> list[Section]:
    return [section for (s, e, section) in ranges if s < end and e > start]


def make_chunk_id(
    *,
    corpus_schema_version: str,
    document_id: str,
    strategy: str,
    params: dict[str, Any],
    section_path: str,
    ordinal: int,
    text_hash: str,
) -> str:
    """Deterministic chunk id (SPEC 6.2, invariant I5)."""

    payload = "\x1f".join(
        [
            corpus_schema_version,
            document_id,
            strategy,
            json.dumps(params, sort_keys=True, ensure_ascii=False),
            section_path,
            str(ordinal),
            text_hash,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_chunk(
    *,
    text: str,
    document: Document,
    corpus_schema_version: str,
    strategy: str,
    params: dict[str, Any],
    section_path: str,
    ordinal: int,
    page_start: int | None,
    page_end: int | None,
    section_level: int = 1,
    section_role: str = "body",
) -> Chunk:
    token_count = count_tokens(text)
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    chunk_id = make_chunk_id(
        corpus_schema_version=corpus_schema_version,
        document_id=document.document_id,
        strategy=strategy,
        params=params,
        section_path=section_path,
        ordinal=ordinal,
        text_hash=text_hash,
    )
    metadata = ChunkMetadata(
        source_uri=document.source.public_uri,
        source_label=document.source.label,
        document_id=document.document_id,
        title=document.source.label,
        section_path=section_path,
        page_start=page_start,
        page_end=page_end,
        language=document.extraction.language,
        content_sha256=document.source.content_sha256,
        content_version=document.extraction.extraction_version,
    )
    return Chunk(
        chunk_id=chunk_id,
        text=text,
        token_count=token_count,
        char_count=len(text),
        metadata=metadata,
    )
