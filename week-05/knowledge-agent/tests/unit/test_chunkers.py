"""Chunkers: fixed windows and structure packing (D21-02, I5, I6)."""

from __future__ import annotations

from knowledge_agent.chunking.fixed import FixedChunker
from knowledge_agent.chunking.structure import StructureChunker
from knowledge_agent.domain.models import Document, ExtractionInfo, Section
from knowledge_agent.text.tokenizer import LexicalTokenizer

from tests.helpers import make_document, make_source_ref


def _numbered_document(count: int = 100) -> Document:
    text = " ".join(f"w{i}" for i in range(count))
    return make_document(text, section_path="1 Body")


def test_fixed_chunker_overlap_and_tail_preserved():
    tokenizer = LexicalTokenizer()
    chunker = FixedChunker(tokenizer, chunk_size=30, overlap=10)
    document = _numbered_document(100)
    chunks = chunker.chunk(document)

    assert len(chunks) >= 4
    tokens = [token for chunk in chunks for token in tokenizer.tokenize(chunk.text)]
    # No token of the source is lost (invariant I6) and the tail is present.
    assert tokens[-1] == "w99"
    for index in range(100):
        assert f"w{index}" in tokens
    # Consecutive chunks overlap.
    first = set(tokenizer.tokenize(chunks[0].text))
    second = set(tokenizer.tokenize(chunks[1].text))
    assert first & second


def test_fixed_chunk_ids_are_deterministic_and_param_sensitive():
    tokenizer = LexicalTokenizer()
    document = _numbered_document(60)
    first = FixedChunker(tokenizer, chunk_size=20, overlap=5).chunk(document)
    again = FixedChunker(tokenizer, chunk_size=20, overlap=5).chunk(document)
    different = FixedChunker(tokenizer, chunk_size=25, overlap=5).chunk(document)
    assert [chunk.chunk_id for chunk in first] == [chunk.chunk_id for chunk in again]
    assert [chunk.chunk_id for chunk in first] != [chunk.chunk_id for chunk in different]


def test_fixed_chunk_metadata_has_provenance():
    tokenizer = LexicalTokenizer()
    chunk = FixedChunker(tokenizer, chunk_size=50, overlap=10).chunk(_numbered_document(60))[0]
    metadata = chunk.metadata
    assert metadata.source_uri == "sample.txt"
    assert "/" not in metadata.source_uri and "\\" not in metadata.source_uri
    assert metadata.section_path == "1 Body"
    assert metadata.content_sha256
    assert metadata.document_id
    assert chunk.token_count > 0


def test_structure_chunker_respects_section_boundaries():
    tokenizer = LexicalTokenizer()
    source = make_source_ref()
    section_a = Section(
        section_id="a",
        section_path="1 Intro",
        level=1,
        text="alpha " * 40,
    )
    section_b = Section(
        section_id="b",
        section_path="2 Memory",
        level=1,
        text="beta " * 40,
    )
    document = Document(
        document_id="doc",
        source=source,
        extraction=ExtractionInfo(extraction_version="text-v1", adapter="text", useful_chars=1),
        sections=[section_a, section_b],
    )
    chunks = StructureChunker(tokenizer, max_tokens=60, min_tokens=5).chunk(document)
    paths = {chunk.metadata.section_path for chunk in chunks}
    assert paths == {"1 Intro", "2 Memory"}
    for chunk in chunks:
        assert chunk.crosses_sections is False
        assert chunk.token_count <= 60


def test_structure_chunker_splits_long_paragraph_without_loss():
    tokenizer = LexicalTokenizer()
    document = make_document(" ".join(f"t{i}" for i in range(300)), section_path="Long")
    chunks = StructureChunker(tokenizer, max_tokens=50, min_tokens=5).chunk(document)
    assert len(chunks) > 1
    collected = [token for chunk in chunks for token in tokenizer.tokenize(chunk.text)]
    assert collected == [f"t{i}" for i in range(300)]


def test_structure_chunker_enforces_max_chars():
    tokenizer = LexicalTokenizer()
    document = make_document("x" * 5000, section_path="Chars")
    chunks = StructureChunker(tokenizer, max_tokens=100, min_tokens=5, max_chars=200).chunk(document)
    assert chunks
    assert all(chunk.char_count <= 200 for chunk in chunks)
