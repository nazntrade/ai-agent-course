"""Reusable test support: deterministic fake embedder and domain builders."""

from __future__ import annotations

import hashlib
from typing import Any, Sequence

from knowledge_agent.chunking.fixed import FixedChunker
from knowledge_agent.chunking.structure import StructureChunker
from knowledge_agent.domain.contracts import (
    CORPUS_SCHEMA_VERSION,
    EmbedBatchResult,
    Embedder,
    EmbedderIdentity,
)
from knowledge_agent.domain.models import Document, ExtractionInfo, Section, SourceRef
from knowledge_agent.service.knowledge_service import KnowledgeService
from knowledge_agent.sources import DefaultSourceResolver
from knowledge_agent.storage.sqlite_store import SqliteIndexStore
from knowledge_agent.text.tokenizer import LexicalTokenizer


def fake_vector(text: str, dimension: int = 8) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values: list[float] = []
    counter = 0
    while len(values) < dimension:
        block = hashlib.sha256(digest + counter.to_bytes(4, "little")).digest()
        for byte in block:
            values.append((byte / 255.0) - 0.5)
            if len(values) == dimension:
                break
        counter += 1
    return values


class FakeEmbedder(Embedder):
    """Deterministic in-process embedder used by unit tests (never inference)."""

    def __init__(
        self,
        dimension: int = 8,
        model: str = "fake-model",
        digest: str = "fake-digest",
        document_prefix: str = "",
        query_prefix: str = "",
        available: bool = True,
    ) -> None:
        self.dimension = dimension
        self._model = model
        self._digest = digest
        self.document_prefix = document_prefix
        self.query_prefix = query_prefix
        self._available = available
        self.calls = 0

    def is_available(self) -> bool:
        return self._available

    def identity(self) -> EmbedderIdentity:
        return EmbedderIdentity(
            provider="fake",
            base_url="http://fake",
            endpoint_version="0",
            api="/api/embed",
            model=self._model,
            digest=self._digest,
            dimension=self.dimension,
            document_prefix=self.document_prefix,
            query_prefix=self.query_prefix,
        )

    def embed_documents(self, texts: Sequence[str]) -> EmbedBatchResult:
        self.calls += 1
        return EmbedBatchResult(
            vectors=[fake_vector(self.document_prefix + t, self.dimension) for t in texts],
            input_tokens=sum(len(t.split()) for t in texts),
        )

    def embed_query(self, text: str) -> EmbedBatchResult:
        self.calls += 1
        return EmbedBatchResult(
            vectors=[fake_vector(self.query_prefix + text, self.dimension)],
            input_tokens=len(text.split()),
        )

    def preflight(self, infer: bool = False) -> dict[str, Any]:
        return {"reachable": self._available, "model_present": self._available, "dimension": self.dimension}


def make_source_ref(label: str = "sample.txt", kind: str = "text") -> SourceRef:
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()
    return SourceRef(
        source_id=digest,
        uri=f"C:/absolute/private/{label}",
        public_uri=label,
        label=label,
        kind=kind,
        content_sha256=digest,
        size_bytes=123,
    )


def make_document(text: str, section_path: str = "Document") -> Document:
    source = make_source_ref()
    return Document(
        document_id="doc-1",
        source=source,
        extraction=ExtractionInfo(
            extraction_version="text-v1", adapter="text", useful_chars=len(text)
        ),
        sections=[
            Section(
                section_id="sec-1",
                section_path=section_path,
                level=1,
                role="body",
                page_start=None,
                page_end=None,
                text=text,
            )
        ],
    )


def make_service(
    tmp_path,
    embedder: Embedder | None = None,
    **kwargs: Any,
) -> tuple[KnowledgeService, SqliteIndexStore]:
    store = SqliteIndexStore(tmp_path / "index.db")
    tokenizer = LexicalTokenizer()
    chunkers = {
        "fixed": FixedChunker(tokenizer, kwargs.get("chunk_size", 50), kwargs.get("overlap", 10)),
        "structure": StructureChunker(tokenizer, max_tokens=60, min_tokens=5),
    }
    service = KnowledgeService(
        store,
        embedder or FakeEmbedder(),
        tokenizer,
        chunkers,
        DefaultSourceResolver(),
        excluded_roles=kwargs.get("excluded_roles", ("references",)),
    )
    return service, store


__all__ = [
    "CORPUS_SCHEMA_VERSION",
    "FakeEmbedder",
    "fake_vector",
    "make_document",
    "make_service",
    "make_source_ref",
]
