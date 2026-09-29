"""OllamaEmbedder contract on an injected transport (D21-03/08, SPEC 8)."""

from __future__ import annotations

import json
import math

import pytest

from knowledge_agent.domain.errors import (
    EmbeddingCountMismatch,
    EmbeddingDimensionMismatch,
    EmbeddingInvalidVector,
    EmbeddingLengthError,
    EmbeddingModelMissing,
    EmbeddingUnavailable,
    EmbeddingUsageError,
)
from knowledge_agent.embedding.ollama_embedder import (
    OllamaEmbedder,
    TransportError,
)


class FakeTransport:
    def __init__(
        self,
        dimension: int = 4,
        fail: str = "none",
        max_chars: int = 100,
        usage: bool = True,
        legacy: bool = False,
        tags_digest: str | None = "tags-digest",
        show_digest: str | None = None,
    ) -> None:
        self.dimension = dimension
        self.fail = fail
        self.max_chars = max_chars
        self.usage = usage
        self.legacy = legacy
        self.tags_digest = tags_digest
        self.show_digest = show_digest
        self.requests: list[tuple[str, str, object]] = []

    def __call__(self, method, url, payload, timeout):
        self.requests.append((method, url, payload))
        if url.endswith("/api/version"):
            if self.fail == "unavailable":
                raise TransportError("connection refused")
            return 200, json.dumps({"version": "1.2.3"}).encode()
        if url.endswith("/api/tags"):
            return 200, json.dumps(
                {
                    "models": [
                        {
                            "name": "embeddinggemma:300m",
                            "digest": self.tags_digest,
                        }
                    ]
                }
            ).encode()
        if url.endswith("/api/show"):
            if self.fail == "missing_model":
                return 404, b'{"error": "model not found, try pulling it"}'
            body = {"model_info": {"embeddinggemma.embedding_length": self.dimension}}
            if self.show_digest:
                body["digest"] = self.show_digest
            return 200, json.dumps(body).encode()
        if url.endswith("/api/embed"):
            if self.legacy:
                return 404, b'{"error": "unknown endpoint"}'
            if self.fail == "missing_model":
                return 404, b'{"error": "model not found, try pulling it"}'
            if self.fail == "length" and any(
                len(text) > self.max_chars for text in self._texts(payload)
            ):
                return 400, b'{"error": "input exceeds the context length"}'
            return 200, self._embed_response(payload)
        if url.endswith("/api/embeddings"):
            vectors = self._vectors(self._texts(payload))
            body: dict = {"embedding": vectors[0]}
            if self.usage:
                body["prompt_eval_count"] = 3
            return 200, json.dumps(body).encode()
        return 404, b"{}"

    def _texts(self, payload):
        if isinstance(payload, dict) and "input" in payload:
            return list(payload["input"])
        if isinstance(payload, dict) and "prompt" in payload:
            return [payload["prompt"]]
        return []

    def _vectors(self, texts):
        vectors = []
        for index, _ in enumerate(texts):
            vector = [float(index + 1)] * self.dimension
            vectors.append(vector)
        if self.fail == "bad_count" and len(vectors) > 1:
            vectors = vectors[:-1]
        if self.fail == "bad_dim":
            vectors = [vector[:-1] for vector in vectors]
        if self.fail == "nonfinite":
            vectors = [[math.nan] + vector[1:] for vector in vectors]
        return vectors

    def _embed_response(self, payload):
        vectors = self._vectors(self._texts(payload))
        body = {"embeddings": vectors, "truncate": False}
        if self.usage:
            body["prompt_eval_count"] = 7
        return json.dumps(body).encode()


def test_successful_batch_and_identity():
    transport = FakeTransport()
    embedder = OllamaEmbedder(transport=transport)
    result = embedder.embed_documents(["alpha", "beta"])
    assert len(result.vectors) == 2
    assert all(len(vector) == 4 for vector in result.vectors)
    assert result.input_tokens == 7
    identity = embedder.identity()
    assert identity.digest == "tags-digest"
    assert identity.dimension == 4
    assert identity.endpoint_version == "1.2.3"
    # l2 normalization applied once.
    assert all(abs(sum(v * v for v in vector) - 1.0) < 1e-6 for vector in result.vectors)


def test_digest_comes_from_tags_when_show_omits_it():
    # Regression: Ollama 0.34.4 reports the digest in /api/tags, not /api/show.
    embedder = OllamaEmbedder(
        transport=FakeTransport(tags_digest="tags-sha256", show_digest=None)
    )
    assert embedder.identity().digest == "tags-sha256"


def test_show_digest_overrides_tags_digest():
    embedder = OllamaEmbedder(
        transport=FakeTransport(tags_digest="from-tags", show_digest="from-show")
    )
    assert embedder.identity().digest == "from-show"


def test_request_uses_truncate_false_and_model():
    transport = FakeTransport()
    OllamaEmbedder(transport=transport).embed_documents(["alpha"])
    embed_requests = [r for r in transport.requests if r[1].endswith("/api/embed")]
    assert embed_requests
    payload = embed_requests[0][2]
    assert payload["truncate"] is False
    assert payload["model"] == "embeddinggemma:300m"


def test_batching_splits_requests():
    transport = FakeTransport()
    embedder = OllamaEmbedder(transport=transport, batch_size=1)
    result = embedder.embed_documents(["a", "b", "c"])
    embed_requests = [r for r in transport.requests if r[1].endswith("/api/embed")]
    assert len(embed_requests) == 3
    assert len(result.vectors) == 3


def test_count_mismatch_raises():
    with pytest.raises(EmbeddingCountMismatch):
        OllamaEmbedder(transport=FakeTransport(fail="bad_count")).embed_documents(["a", "b"])


def test_dimension_mismatch_raises():
    with pytest.raises(EmbeddingDimensionMismatch):
        OllamaEmbedder(transport=FakeTransport(fail="bad_dim")).embed_documents(["a"])


def test_non_finite_vector_raises():
    with pytest.raises(EmbeddingInvalidVector):
        OllamaEmbedder(transport=FakeTransport(fail="nonfinite")).embed_documents(["a"])


def test_length_error_splits_without_loss():
    transport = FakeTransport(fail="length", max_chars=20)
    embedder = OllamaEmbedder(transport=transport)
    text = "word " * 40  # 200 chars, over the fake limit
    result = embedder.embed_documents([text])
    assert len(result.vectors) == 1
    assert len(result.vectors[0]) == 4


def test_length_error_on_unsplittable_input_raises():
    transport = FakeTransport(fail="length", max_chars=-1)
    with pytest.raises(EmbeddingLengthError):
        OllamaEmbedder(transport=transport).embed_documents(["x"])


def test_double_prefix_raises_usage_error():
    embedder = OllamaEmbedder(
        transport=FakeTransport(), document_prefix="doc: ", query_prefix="q: "
    )
    with pytest.raises(EmbeddingUsageError):
        embedder.embed_documents(["doc: already prefixed"])


def test_unavailable_provider_raises():
    with pytest.raises(EmbeddingUnavailable):
        OllamaEmbedder(transport=FakeTransport(fail="unavailable")).embed_documents(["a"])


def test_missing_model_raises_with_hint():
    with pytest.raises(EmbeddingModelMissing) as error:
        OllamaEmbedder(transport=FakeTransport(fail="missing_model")).embed_documents(["a"])
    assert "ollama pull embeddinggemma:300m" in error.value.details.get("hint", "")


def test_input_tokens_none_when_usage_missing():
    result = OllamaEmbedder(transport=FakeTransport(usage=False)).embed_documents(["a"])
    assert result.input_tokens is None


def test_legacy_endpoint_fallback():
    transport = FakeTransport(legacy=True)
    result = OllamaEmbedder(transport=transport).embed_documents(["a", "b"])
    assert len(result.vectors) == 2
    assert any(r[1].endswith("/api/embeddings") for r in transport.requests)


def test_legacy_input_tokens_none_when_usage_missing():
    transport = FakeTransport(legacy=True, usage=False)
    result = OllamaEmbedder(transport=transport).embed_documents(["a", "b"])
    assert len(result.vectors) == 2
    assert result.input_tokens is None


def test_observed_identity_survives_transient_preflight_failure():
    transport = FakeTransport(dimension=4, tags_digest="tags-digest")
    embedder = OllamaEmbedder(transport=transport, preflight_ttl=0.0)
    embedder.embed_documents(["alpha"])

    transport.fail = "unavailable"  # provider goes down after a real success
    identity = embedder.identity()
    assert identity.dimension == 4
    assert identity.digest == "tags-digest"
