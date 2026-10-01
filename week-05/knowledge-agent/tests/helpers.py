"""Reusable test support: deterministic fake embedder and domain builders."""

from __future__ import annotations

import hashlib
from typing import Any, Sequence

from knowledge_agent.chunking.fixed import FixedChunker
from knowledge_agent.chunking.structure import StructureChunker
from knowledge_agent.domain.contracts import (
    CORPUS_SCHEMA_VERSION,
    ChatMessage,
    ChatModel,
    ChatModelIdentity,
    ChatResult,
    ChatUsage,
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


class FakeChatModel(ChatModel):
    """Deterministic in-process chat model used by unit tests (never inference)."""

    def __init__(
        self,
        text: str = "fake answer",
        *,
        finish_reason: str = "stop",
        usage: ChatUsage | None = ChatUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        rate: float | None = 20.0,
        model: str = "fake-chat",
        error: Exception | None = None,
        stream_pieces: int = 3,
        context_length: int | None = 8192,
    ) -> None:
        self.text = text
        self.finish_reason = finish_reason
        self.usage = usage
        self.rate = rate
        self.model = model
        self.error = error
        self.stream_pieces = max(1, stream_pieces)
        self.context_length = context_length
        self.calls: list[list[ChatMessage]] = []
        self.options_seen: list[dict | None] = []

    def identity(self) -> ChatModelIdentity:
        return ChatModelIdentity(
            provider="fake",
            base_url="http://fake",
            model=self.model,
            digest="fake-digest",
            context_length=self.context_length,
            default_options={"temperature": 0, "seed": 0, "num_ctx": 8192, "num_predict": 128},
        )

    def preflight(self, infer: bool = False) -> dict:
        return {
            "reachable": True,
            "model_present": True,
            "digest": "fake-digest",
            "context_length": self.context_length,
            "version": "0.0.0-fake",
        }

    def _result(self) -> ChatResult:
        return ChatResult(
            text=self.text,
            finish_reason=self.finish_reason,
            usage=self.usage,
            model=self.model,
            created_at="2026-10-01T00:00:00+00:00",
            latency_ms=12.5,
            output_tokens_per_second=self.rate,
        )

    def chat(self, messages, options=None) -> ChatResult:
        self.calls.append(list(messages))
        self.options_seen.append(dict(options) if options else None)
        if self.error is not None:
            raise self.error
        return self._result()

    def stream_chat(self, messages, options=None):
        self.calls.append(list(messages))
        self.options_seen.append(dict(options) if options else None)
        if self.error is not None:
            raise self.error
        size = max(1, len(self.text) // self.stream_pieces or 1)
        for start in range(0, len(self.text), size):
            yield {"type": "token", "text": self.text[start : start + size]}
        yield {"type": "done", "result": self._result()}


class FakeKnowledge:
    """Minimal KnowledgeService stand-in for ChatService unit tests."""

    def __init__(
        self,
        fragments=None,
        *,
        search_error: Exception | None = None,
        resolve_error: Exception | None = None,
        index_version_id: str = "idx-1",
        fingerprint: str = "fp-1",
    ) -> None:
        self.fragments = fragments if fragments is not None else []
        self.search_error = search_error
        self.resolve_error = resolve_error
        self.index_version_id = index_version_id
        self.fingerprint = fingerprint
        self.search_calls = 0
        self.resolve_calls = 0

    def search(self, collection_id, query, top_k=5, index_version_id=None, strategy=None):
        self.search_calls += 1
        if self.search_error is not None:
            raise self.search_error
        return {
            "query": query,
            "collection_id": collection_id,
            "index_version_id": index_version_id or self.index_version_id,
            "strategy": strategy or "fixed",
            "top_k": top_k,
            "fragments": list(self.fragments),
            "counts": {"indexed_chunks": len(self.fragments), "returned": len(self.fragments)},
            "metrics": {},
        }

    def get_index_version(self, index_version_id):
        return {"index_version_id": index_version_id, "fingerprint": self.fingerprint}

    def resolve_ready_index(self, collection_id, index_version_id=None, strategy=None):
        self.resolve_calls += 1
        if self.resolve_error is not None:
            raise self.resolve_error
        return {"index_version_id": index_version_id or self.index_version_id, "status": "ready"}


def fragment(chunk_id: str, text: str = "chunk text", rank: int = 1, score: float = 0.9):
    return {
        "rank": rank,
        "score": score,
        "chunk_id": chunk_id,
        "text": text,
        "metadata": {
            "source_uri": "sample.txt",
            "source_label": "sample.txt",
            "document_id": "doc",
            "title": "sample",
            "section_path": "Section",
            "page_start": 1,
            "page_end": 1,
            "language": "en",
            "content_sha256": "0" * 64,
            "content_version": "text-v1",
        },
    }


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
    "FakeChatModel",
    "FakeEmbedder",
    "FakeKnowledge",
    "fake_vector",
    "fragment",
    "make_document",
    "make_service",
    "make_source_ref",
]
