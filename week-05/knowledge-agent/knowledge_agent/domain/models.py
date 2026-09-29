"""Domain data models shared by every adapter and the service layer."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class SourceRef(BaseModel):
    """A concrete file chosen by the user through configuration.

    ``uri`` holds the explicit local path and only ever lives in the local
    database (``sources.uri``). ``public_uri`` is the sanitized value exposed
    through metadata and the API (invariant I7).
    """

    source_id: str
    uri: str
    public_uri: str
    label: str
    kind: Literal["pdf", "text"]
    content_sha256: str
    size_bytes: int


class ExtractionInfo(BaseModel):
    extraction_version: str
    adapter: str
    page_count: Optional[int] = None
    useful_pages: Optional[int] = None
    useful_chars: int = 0
    language: str = "unknown"
    warnings: list[str] = Field(default_factory=list)


class Section(BaseModel):
    """A real document section (a heading and its body), never a page."""

    section_id: str
    section_path: str
    level: int = 1
    role: str = "body"
    start_bbox: Optional[list[float]] = None
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    text: str


class Document(BaseModel):
    document_id: str
    source: SourceRef
    extraction: ExtractionInfo
    sections: list[Section] = Field(default_factory=list)
    normalized_markdown: Optional[str] = None


class ChunkMetadata(BaseModel):
    """Full chunk provenance (SPEC 6.3)."""

    source_uri: str
    source_label: str
    document_id: str
    title: str
    section_path: str
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    language: str = "unknown"
    content_sha256: str
    content_version: str


class Chunk(BaseModel):
    chunk_id: str
    text: str
    token_count: int
    char_count: int
    metadata: ChunkMetadata
    vector: Optional[list[float]] = None
    # Internal metric flag: chunk spans more than one real section (SPEC 14.4).
    crosses_sections: bool = False
