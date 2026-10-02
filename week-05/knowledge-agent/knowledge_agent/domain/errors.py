"""Typed error taxonomy with stable public codes and HTTP status mapping."""

from __future__ import annotations

from typing import Any


class KnowledgeError(Exception):
    """Base error carrying a stable ``code`` and the HTTP status to return."""

    code = "internal_error"
    http_status = 500

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class SourceError(KnowledgeError):
    """Any bad or unreadable source maps to HTTP 422 (SPEC 7.3)."""

    code = "source_error"
    http_status = 422


class SourceUnreadable(SourceError):
    code = "source_unreadable"


class SourceUnsupported(SourceError):
    code = "source_unsupported"


class SourceEmpty(SourceError):
    code = "source_empty"


class SourceNoTextLayer(SourceError):
    code = "source_no_text_layer"


class SourceInvalid(SourceError):
    code = "source_invalid"


class SourceEncrypted(SourceError):
    code = "source_encrypted"


class EmbeddingError(KnowledgeError):
    """Ollama unavailability and embedding contract violations map to HTTP 503."""

    code = "embedding_error"
    http_status = 503


class EmbeddingUnavailable(EmbeddingError):
    code = "embedding_unavailable"


class EmbeddingTimeout(EmbeddingError):
    code = "embedding_timeout"


class EmbeddingModelMissing(EmbeddingError):
    code = "embedding_model_missing"


class EmbeddingLengthError(EmbeddingError):
    code = "embedding_length_error"


class EmbeddingDimensionMismatch(EmbeddingError):
    code = "embedding_dimension_mismatch"


class EmbeddingCountMismatch(EmbeddingError):
    code = "embedding_count_mismatch"


class EmbeddingInvalidVector(EmbeddingError):
    code = "embedding_invalid_vector"


class EmbeddingUsageError(EmbeddingError):
    code = "embedding_usage_error"


class InvalidRequest(KnowledgeError):
    """Semantically invalid request that passed schema validation maps to 422."""

    code = "invalid_request"
    http_status = 422


class InvalidThreshold(InvalidRequest):
    """A relevance threshold outside [0, 1] (SPEC D23 8)."""

    code = "invalid_threshold"


class ChatError(KnowledgeError):
    """Chat provider and context errors (SPEC D22 7.2)."""

    code = "chat_error"
    http_status = 503


class ChatUnavailable(ChatError):
    code = "chat_unavailable"


class ChatTimeout(ChatError):
    code = "chat_timeout"


class ChatModelMissing(ChatError):
    code = "chat_model_missing"


class ChatInvalidResponse(ChatError):
    code = "chat_invalid_response"


class ChatLengthError(ChatError):
    code = "chat_length_error"


class ContextOverflow(KnowledgeError):
    """Instructions and question do not fit the prompt budget (D22-07)."""

    code = "context_overflow"
    http_status = 422


class IndexConflict(KnowledgeError):
    """Index state conflicts map to HTTP 409."""

    code = "index_conflict"
    http_status = 409


class IndexIncompatible(IndexConflict):
    code = "index_incompatible"


class IndexNotReady(IndexConflict):
    code = "index_not_ready"


class IndexBusy(IndexConflict):
    code = "index_busy"


class StoreSchemaUnsupported(KnowledgeError):
    code = "store_schema_unsupported"
    http_status = 500
