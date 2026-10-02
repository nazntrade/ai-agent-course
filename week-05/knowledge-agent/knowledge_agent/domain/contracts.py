"""Abstract contracts connecting the core to external adapters (SPEC 5, 7.1)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Literal, Mapping, Protocol, Sequence

from .errors import IndexIncompatible
from .models import Chunk, Document, Section, SourceRef

CORPUS_SCHEMA_VERSION = "corpus-v3"

# Fields compared by :func:`check_index_compatibility` (SPEC 10).
# ``normalization_version`` is an addition beyond the literal SPEC 10 list; it is
# also recorded in ``manifest.pipeline`` (see README).
COMPATIBILITY_FIELDS = (
    "model",
    "digest",
    "dimension",
    "normalization",
    "normalization_version",
    "dtype",
    "document_prefix",
    "query_prefix",
    "corpus_schema_version",
)


class SourceAdapter(ABC):
    """Reads one source file into a :class:`Document`."""

    kind: str
    extraction_version: str

    @abstractmethod
    def extract(self, source_ref: SourceRef) -> Document:
        """Parse ``source_ref`` and raise typed ``Source*`` errors on failure."""


class SourceResolver(Protocol):
    """Chooses the concrete :class:`SourceAdapter` for an explicit user path."""

    def resolve(self, path: str) -> SourceAdapter: ...


class Tokenizer(ABC):
    @abstractmethod
    def tokenize(self, text: str) -> list[str]:
        """Return a deterministic lexical token list (offline, no network)."""

    def tokenize_spans(self, text: str) -> list[tuple[str, int, int]]:
        """Return ``(token, start, end)`` triples so chunkers keep char offsets."""
        raise NotImplementedError


class Chunker(ABC):
    strategy: str

    @property
    @abstractmethod
    def params(self) -> dict[str, Any]:
        """Deterministic parameter map stored in the manifest and chunk ids."""

    @abstractmethod
    def chunk(self, document: Document) -> list[Chunk]:
        """Split ``document`` into indexed chunks."""


@dataclass
class EmbedderIdentity:
    provider: str
    base_url: str
    endpoint_version: str
    api: str
    model: str
    digest: str | None
    dimension: int | None
    dtype: str = "float32"
    normalization: str = "l2"
    document_prefix: str = ""
    query_prefix: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "endpoint_version": self.endpoint_version,
            "api": self.api,
            "model": self.model,
            "digest": self.digest,
            "dimension": self.dimension,
            "dtype": self.dtype,
            "normalization": self.normalization,
            "document_prefix": self.document_prefix,
            "query_prefix": self.query_prefix,
        }


@dataclass
class EmbedBatchResult:
    vectors: list[list[float]]
    input_tokens: int | None = None
    latency_ms: float | None = None
    latencies_ms: list[float] = field(default_factory=list)
    batch_count: int = 0


class Embedder(ABC):
    """Computes vectors. Only the embedder applies document/query prefixes."""

    @abstractmethod
    def identity(self) -> EmbedderIdentity: ...

    def is_available(self) -> bool:
        """Whether the provider is reachable; adapters may override."""
        return True

    @abstractmethod
    def embed_documents(self, texts: Sequence[str]) -> EmbedBatchResult: ...

    @abstractmethod
    def embed_query(self, text: str) -> EmbedBatchResult: ...

    @abstractmethod
    def preflight(self, infer: bool = False) -> dict[str, Any]:
        """Probe provider availability/format without running inference."""


@dataclass
class ChatMessage:
    """One provider message (``system``/``user``/``assistant``)."""

    role: Literal["system", "user", "assistant"]
    content: str

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "content": self.content}


@dataclass
class ChatUsage:
    """Provider-reported token usage; any field may be ``None`` (never invented)."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class ChatModelIdentity:
    """Resolved chat-model identity snapshot (SPEC D22 6.1)."""

    provider: str
    base_url: str
    model: str
    digest: str | None = None
    context_length: int | None = None
    default_options: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "digest": self.digest,
            "context_length": self.context_length,
            "default_options": dict(self.default_options),
        }


@dataclass
class ChatResult:
    """One non-streamed provider response."""

    text: str
    finish_reason: str | None
    usage: ChatUsage | None
    model: str
    created_at: str
    latency_ms: float
    output_tokens_per_second: float | None = None
    raw: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "text": self.text,
            "finish_reason": self.finish_reason,
            "usage": self.usage.to_dict() if self.usage else None,
            "model": self.model,
            "created_at": self.created_at,
            "latency_ms": self.latency_ms,
            "output_tokens_per_second": self.output_tokens_per_second,
        }
        if self.raw is not None:
            payload["raw"] = self.raw
        return payload


@dataclass
class RewriteResult:
    """Query-rewrite outcome (SPEC D23 6.2). Never an HTTP error by itself."""

    original_query: str
    search_query: str
    attempted: bool
    used: bool
    fallback: bool
    reason: str | None = None
    template_id: str = ""
    template_hash: str = ""
    finish_reason: str | None = None
    usage: ChatUsage | None = None
    latency_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "used": self.used,
            "fallback": self.fallback,
            "reason": self.reason,
            "original_query": self.original_query,
            "search_query": self.search_query,
            "template_id": self.template_id,
            "template_hash": self.template_hash,
            "finish_reason": self.finish_reason,
            "usage": self.usage.to_dict() if self.usage else None,
            "latency_ms": self.latency_ms,
        }


class QueryRewriter(ABC):
    """Core contract: reformulates only the retrieval query (SPEC D23 7.2)."""

    @abstractmethod
    def rewrite(self, question: str) -> RewriteResult: ...


# Grounded-RAG layer (SPEC D24 6). These structures are pure data: the verifier
# fills them from ``passed`` chunks and the parsed provider answer. The model
# text never supplies ``source``/``section``; those come from index metadata.

MEANING_CHECK_NOT_PERFORMED = "not_performed"
WHITESPACE_NORMALIZATION = "nfc-collapse-trim-v1"


@dataclass
class GroundedAnswer:
    """A parsed grounded-JSON provider answer (SPEC D24 6.1)."""

    answer: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    insufficient: bool = False
    limitation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "citations": [dict(citation) for citation in self.citations],
            "insufficient": self.insufficient,
            "limitation": self.limitation,
        }


@dataclass
class Citation:
    """One verified/unsupported citation projection (SPEC D24 6.2)."""

    chunk_id: str
    source: str | None = None
    section: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    quote: str = ""
    translation: str | None = None
    is_translation: bool = False
    source_exists: bool = False
    quote_verbatim: bool = False
    meaning_supported: bool | None = None
    status: str = "unknown_chunk_id"
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "source": self.source,
            "section": self.section,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "quote": self.quote,
            "translation": self.translation,
            "is_translation": self.is_translation,
            "source_exists": self.source_exists,
            "quote_verbatim": self.quote_verbatim,
            "meaning_supported": self.meaning_supported,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass
class GroundingResult:
    """The ``answer.grounding`` block (SPEC D24 6.3)."""

    status: str
    reason: str | None = None
    threshold: float | None = None
    meaning_check: str = MEANING_CHECK_NOT_PERFORMED
    limitation: str | None = None
    citations: list[Citation] = field(default_factory=list)
    refusal: dict[str, Any] | None = None
    # D24 defect fix: inline ``[chunk_id]`` references in the answer text that
    # contradict the structured citations. ``inline_unsupported`` are ids not
    # passed to the model; ``inline_missing_quote`` are passed ids without a
    # verified structured citation. Both are empty when every inline reference
    # is backed by a verified citation.
    inline_unsupported: list[str] = field(default_factory=list)
    inline_missing_quote: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "threshold": self.threshold,
            "meaning_check": self.meaning_check,
            "limitation": self.limitation,
            "citations": [citation.to_dict() for citation in self.citations],
            "refusal": self.refusal,
            "inline_unsupported": list(self.inline_unsupported),
            "inline_missing_quote": list(self.inline_missing_quote),
        }


# Provider-level streaming events: a tagged union of ``token`` and ``done``
# (errors are raised as typed exceptions, not encoded as events; SPEC D22 6.1).
ChatModelEvent = dict[str, Any]
# Service-level streaming events: ``start``/``sources``/``token``/``done``/``error``.
ChatStreamEvent = dict[str, Any]


class ChatModel(ABC):
    """Core chat-generation contract; the core never sees Ollama details."""

    @abstractmethod
    def identity(self) -> ChatModelIdentity: ...

    @abstractmethod
    def chat(
        self, messages: Sequence[ChatMessage], options: Mapping[str, Any] | None = None
    ) -> ChatResult: ...

    @abstractmethod
    def stream_chat(
        self, messages: Sequence[ChatMessage], options: Mapping[str, Any] | None = None
    ) -> Iterator[ChatModelEvent]: ...

    def is_available(self) -> bool:
        """Whether the provider endpoint is reachable; adapters may override."""

        return True

    def preflight(self, infer: bool = False) -> dict[str, Any]:
        """Probe provider availability/format without running inference."""

        return {}


class ChatRunStore(ABC):
    """Immutable run records and mutable manual evaluations (SPEC D22 15)."""

    @abstractmethod
    def create_run(self, record: Mapping[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def get_run(self, run_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_runs(
        self,
        *,
        limit: int = 20,
        kind: str | None = None,
        mode: str | None = None,
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    def save_evaluation(self, evaluation: Mapping[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def get_evaluation(self, run_id: str) -> dict[str, Any] | None: ...


class IndexStore(ABC):
    """Adapter between SQLite details and :class:`KnowledgeService` (SPEC 5.3)."""

    @abstractmethod
    def ensure_schema(self) -> None: ...

    @abstractmethod
    def create_collection(self, name: str) -> dict[str, Any]: ...

    @abstractmethod
    def list_collections(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_collection(self, collection_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def find_ready_index(self, collection_id: str, fingerprint: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def find_ready_index_by_strategy(
        self, collection_id: str, strategy: str
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def create_index_version(
        self,
        collection_id: str,
        strategy: str,
        fingerprint: str,
        identity: Mapping[str, Any],
    ) -> str: ...

    @abstractmethod
    def update_progress(self, index_version_id: str, progress: Mapping[str, Any]) -> None: ...

    @abstractmethod
    def finish_index(
        self,
        index_version_id: str,
        counts: Mapping[str, Any],
        metrics: Mapping[str, Any],
        manifest: Mapping[str, Any],
        finished_at: str | None = None,
        identity: Mapping[str, Any] | None = None,
    ) -> None: ...

    @abstractmethod
    def fail_index(self, index_version_id: str, error: str) -> None: ...

    @abstractmethod
    def mark_stale_builds_failed(self, reason: str) -> int: ...

    @abstractmethod
    def set_active_index(self, collection_id: str, index_version_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def get_active_index(self, collection_id: str, strategy: str | None = None) -> dict[str, Any] | None: ...

    @abstractmethod
    def get_index_version(self, index_version_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_index_versions(self, collection_id: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def save_document(self, index_version_id: str, document: Document, ordinal: int) -> None: ...

    @abstractmethod
    def insert_chunks(
        self, index_version_id: str, chunks: Iterable[Chunk], ordinal_start: int = 0
    ) -> int: ...

    @abstractmethod
    def list_chunks(
        self,
        index_version_id: str,
        offset: int = 0,
        limit: int = 50,
        document_id: str | None = None,
        section_path: str | None = None,
        chunk_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]: ...

    @abstractmethod
    def iter_chunk_vectors(self, index_version_id: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def count_rows(self, index_version_id: str) -> dict[str, int]: ...

    @abstractmethod
    def db_size_bytes(self) -> int: ...


def check_index_compatibility(
    expected: Mapping[str, Any], actual: Mapping[str, Any]
) -> None:
    """Compare stored vs current embedding identity before any query embedding.

    Raises :class:`IndexIncompatible` with ``{expected, actual}`` on the first
    set of differing fields (SPEC 10, D21-08).
    """

    diffs_expected: dict[str, Any] = {}
    diffs_actual: dict[str, Any] = {}
    for field_name in COMPATIBILITY_FIELDS:
        lhs = expected.get(field_name)
        rhs = actual.get(field_name)
        if lhs != rhs:
            diffs_expected[field_name] = lhs
            diffs_actual[field_name] = rhs
    if diffs_actual:
        raise IndexIncompatible(
            "Index embedding identity is incompatible with the current one.",
            details={"expected": diffs_expected, "actual": diffs_actual},
        )


def identity_for_compatibility(version: Mapping[str, Any]) -> dict[str, Any]:
    """Project a stored index version onto the compatibility identity fields.

    Lives in the core so the service does not depend on storage details.
    """

    return {field_name: version.get(field_name) for field_name in COMPATIBILITY_FIELDS}
