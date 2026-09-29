"""Ollama embedding adapter (SPEC 8).

Only this class applies the document/query prefixes, batches inputs, validates
count/dimension/finiteness and normalizes vectors once. HTTP access goes through
an injectable ``transport`` so unit tests never touch the network.
"""

from __future__ import annotations

import json
import math
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Sequence

from ..domain.contracts import EmbedBatchResult, Embedder, EmbedderIdentity
from ..domain.errors import (
    EmbeddingCountMismatch,
    EmbeddingDimensionMismatch,
    EmbeddingInvalidVector,
    EmbeddingLengthError,
    EmbeddingModelMissing,
    EmbeddingTimeout,
    EmbeddingUnavailable,
    EmbeddingUsageError,
)

Transport = Callable[[str, str, Any, float], tuple[int, bytes]]


class TransportError(Exception):
    """Network-level failure of the injected transport."""


class TransportTimeout(TransportError):
    """Timeout of the injected transport."""


class _LengthExceeded(Exception):
    """Internal signal that the provider rejected an over-long input."""


def urllib_transport(method: str, url: str, payload: Any, timeout: float) -> tuple[int, bytes]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except socket.timeout as exc:  # pragma: no cover - timing dependent
        raise TransportTimeout(str(exc)) from exc
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), socket.timeout):
            raise TransportTimeout(str(exc)) from exc
        raise TransportError(str(exc)) from exc


_LENGTH_MARKERS = (
    "too long",
    "context length",
    "exceeds",
    "maximum context",
    "input length",
)


class OllamaEmbedder(Embedder):
    provider = "ollama"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "embeddinggemma:300m",
        *,
        batch_size: int = 16,
        timeout: float = 60.0,
        document_prefix: str = "",
        query_prefix: str = "",
        transport: Transport | None = None,
        preflight_ttl: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.batch_size = max(1, batch_size)
        self.timeout = timeout
        self.document_prefix = document_prefix
        self.query_prefix = query_prefix
        self._transport = transport or urllib_transport
        self._preflight_ttl = preflight_ttl
        self._preflight_cache: dict[str, Any] | None = None
        self._preflight_at = 0.0
        # Identity observed from real successful calls; survives a later
        # transient preflight failure so compatibility checks stay stable.
        self._observed_dimension: int | None = None
        self._observed_digest: str | None = None

    # -- HTTP -------------------------------------------------------------
    def _request(
        self, method: str, path: str, payload: Any = None, timeout: float | None = None
    ) -> tuple[int, bytes]:
        url = self.base_url + path
        try:
            return self._transport(method, url, payload, timeout or self.timeout)
        except TransportTimeout as exc:
            raise EmbeddingTimeout("The embedding provider timed out.") from exc
        except TransportError as exc:
            raise EmbeddingUnavailable(
                "The embedding provider is not reachable."
            ) from exc

    # -- preflight --------------------------------------------------------
    def preflight(self, infer: bool = False, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if (
            not force
            and self._preflight_cache is not None
            and now - self._preflight_at < self._preflight_ttl
        ):
            return self._preflight_cache

        info: dict[str, Any] = {
            "reachable": False,
            "version": None,
            "model_present": False,
            "digest": None,
            "dimension": None,
            "api": "/api/embed",
            "warnings": [],
        }
        try:
            status, body = self._request("GET", "/api/version", None, 3.0)
        except (EmbeddingUnavailable, EmbeddingTimeout):
            self._cache(info)
            return info
        if 200 <= status < 300:
            info["reachable"] = True
            info["version"] = _safe_json(body).get("version")
        else:
            info["warnings"].append(f"version endpoint returned {status}")

        if info["reachable"]:
            try:
                status, body = self._request("GET", "/api/tags", None, 3.0)
                models = _safe_json(body).get("models", [])
                for model in models:
                    if _same_model(model.get("name", ""), self.model):
                        info["model_present"] = True
                        # Real Ollama returns the model digest in /api/tags.
                        if model.get("digest"):
                            info["digest"] = model.get("digest")
                            self._observed_digest = model.get("digest")
                        break
            except (EmbeddingUnavailable, EmbeddingTimeout):
                info["warnings"].append("tags endpoint unreachable")
            try:
                status, body = self._request(
                    "POST", "/api/show", {"model": self.model}, 5.0
                )
                if 200 <= status < 300:
                    show = _safe_json(body)
                    # Older Ollama versions return digest here; ones without it
                    # fall back to the digest collected from /api/tags.
                    if show.get("digest"):
                        info["digest"] = show.get("digest")
                        self._observed_digest = show.get("digest")
                    dimension = _extract_embedding_length(show)
                    if dimension is not None:
                        info["dimension"] = dimension
                        self._observed_dimension = dimension
            except (EmbeddingUnavailable, EmbeddingTimeout):
                info["warnings"].append("show endpoint unreachable")

        if infer and info["reachable"]:
            try:
                vectors, _ = self._post_embed(["preflight"])
                info["dimension"] = info["dimension"] or len(vectors[0])
            except Exception as exc:  # noqa: BLE001 - preflight must not raise
                info["warnings"].append(f"inference probe failed: {type(exc).__name__}")

        self._cache(info)
        return info

    def _cache(self, info: dict[str, Any]) -> None:
        self._preflight_cache = info
        self._preflight_at = time.monotonic()

    def is_available(self) -> bool:
        return bool(self.preflight().get("reachable"))

    def identity(self) -> EmbedderIdentity:
        info = self.preflight()
        return EmbedderIdentity(
            provider=self.provider,
            base_url=self.base_url,
            endpoint_version=info.get("version") or "unknown",
            api=info.get("api") or "/api/embed",
            model=self.model,
            digest=info.get("digest") or self._observed_digest,
            dimension=info.get("dimension") or self._observed_dimension,
            document_prefix=self.document_prefix,
            query_prefix=self.query_prefix,
        )

    # -- embedding --------------------------------------------------------
    def embed_documents(self, texts: Sequence[str]) -> EmbedBatchResult:
        prepared = [self._apply_prefix(text, self.document_prefix) for text in texts]
        return self._embed_many(prepared)

    def embed_query(self, text: str) -> EmbedBatchResult:
        prepared = self._apply_prefix(text, self.query_prefix)
        return self._embed_many([prepared])

    def _apply_prefix(self, text: str, prefix: str) -> str:
        for candidate in (self.document_prefix, self.query_prefix):
            if candidate and text.startswith(candidate):
                raise EmbeddingUsageError(
                    "The input already contains an embedding prefix; prefixes are"
                    " applied only by the embedder.",
                    details={"prefix": candidate},
                )
        return f"{prefix}{text}" if prefix else text

    def _embed_many(self, texts: Sequence[str]) -> EmbedBatchResult:
        if not self.is_available():
            raise EmbeddingUnavailable("The embedding provider is not reachable.")
        vectors: list[list[float]] = []
        latencies: list[float] = []
        input_tokens: int | None = 0
        for start in range(0, len(texts), self.batch_size):
            batch = list(texts[start : start + self.batch_size])
            began = time.perf_counter()
            raw, tokens = self._embed_batch_with_split(batch)
            latencies.append(round((time.perf_counter() - began) * 1000, 3))
            if tokens is None:
                input_tokens = None
            elif input_tokens is not None:
                input_tokens += tokens
            vectors.extend(raw)
        validated = self._validate(vectors, len(texts))
        if validated and self._observed_dimension is None:
            self._observed_dimension = len(validated[0])
        ordered = sorted(latencies)
        median = ordered[len(ordered) // 2] if ordered else None
        return EmbedBatchResult(
            vectors=validated,
            input_tokens=input_tokens,
            latency_ms=median,
            latencies_ms=latencies,
            batch_count=len(latencies),
        )

    def _embed_batch_with_split(
        self, texts: Sequence[str]
    ) -> tuple[list[list[float]], int | None]:
        try:
            return self._post_embed(texts)
        except _LengthExceeded as exc:
            if len(texts) > 1:
                middle = len(texts) // 2
                left, left_tokens = self._embed_batch_with_split(texts[:middle])
                right, right_tokens = self._embed_batch_with_split(texts[middle:])
                return left + right, _sum_tokens(left_tokens, right_tokens)
            text = texts[0]
            if len(text) < 2:
                raise EmbeddingLengthError(
                    "The input is too long for the embedding model and cannot be"
                    " split further."
                ) from exc
            cut = _split_point(text)
            left, _ = self._embed_batch_with_split([text[:cut]])
            right, _ = self._embed_batch_with_split([text[cut:]])
            fused = _l2([(a + b) / 2.0 for a, b in zip(left[0], right[0])])
            return [fused], None

    def _post_embed(self, texts: Sequence[str]) -> tuple[list[list[float]], int | None]:
        payload = {"model": self.model, "input": list(texts), "truncate": False}
        status, body = self._request("POST", "/api/embed", payload)
        if status == 404 and not _mentions_model(body):
            return self._post_legacy(texts)
        return self._parse_embed_response(status, body)

    def _post_legacy(self, texts: Sequence[str]) -> tuple[list[list[float]], int | None]:
        vectors: list[list[float]] = []
        total: int | None = 0
        for text in texts:
            status, body = self._request(
                "POST", "/api/embeddings", {"model": self.model, "prompt": text}
            )
            if status == 404 and _mentions_model(body):
                raise EmbeddingModelMissing(
                    f"The embedding model is not available: {self.model}.",
                    details={"hint": f"ollama pull {self.model}"},
                )
            if status < 200 or status >= 300:
                self._raise_for_status(status, body)
            document = _safe_json(body)
            vector = document.get("embedding")
            if not isinstance(vector, list):
                raise EmbeddingInvalidVector("The provider returned no embedding.")
            vectors.append([float(value) for value in vector])
            # Never invent usage: legacy providers may omit prompt_eval_count.
            tokens = document.get("prompt_eval_count")
            if not isinstance(tokens, int):
                total = None
            elif total is not None:
                total += tokens
        return vectors, total

    def _parse_embed_response(
        self, status: int, body: bytes
    ) -> tuple[list[list[float]], int | None]:
        if status < 200 or status >= 300:
            self._raise_for_status(status, body)
        document = _safe_json(body)
        embeddings = document.get("embeddings")
        if not isinstance(embeddings, list):
            # Some versions return a single "embedding".
            single = document.get("embedding")
            embeddings = [single] if isinstance(single, list) else None
        if not isinstance(embeddings, list):
            raise EmbeddingInvalidVector("The provider returned no embeddings.")
        vectors = [[float(value) for value in vector] for vector in embeddings]
        tokens = document.get("prompt_eval_count")
        return vectors, tokens if isinstance(tokens, int) else None

    def _raise_for_status(self, status: int, body: bytes) -> None:
        text = body.decode("utf-8", "replace").lower()
        if any(marker in text for marker in _LENGTH_MARKERS):
            raise _LengthExceeded(text[:200])
        if status == 404 and _mentions_model(body):
            raise EmbeddingModelMissing(
                f"The embedding model is not available: {self.model}.",
                details={"hint": f"ollama pull {self.model}"},
            )
        if status in (408, 504):
            raise EmbeddingTimeout(f"The embedding provider timed out ({status}).")
        raise EmbeddingUnavailable(
            f"The embedding provider returned HTTP {status}.",
        )

    def _validate(self, vectors: Sequence[Sequence[float]], expected: int) -> list[list[float]]:
        if len(vectors) != expected:
            raise EmbeddingCountMismatch(
                "The provider returned a different number of vectors than inputs.",
                details={"expected": expected, "actual": len(vectors)},
            )
        dimension = self.identity().dimension
        result: list[list[float]] = []
        for vector in vectors:
            values = list(vector)
            if not values:
                raise EmbeddingInvalidVector("The provider returned an empty vector.")
            for value in values:
                if not math.isfinite(value):
                    raise EmbeddingInvalidVector("The provider returned a non-finite value.")
            if dimension is not None and len(values) != dimension:
                raise EmbeddingDimensionMismatch(
                    "The provider returned an unexpected vector dimension.",
                    details={"expected": dimension, "actual": len(values)},
                )
            result.append(_l2(values))
        return result


def _l2(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return list(vector)
    return [value / norm for value in vector]


def _sum_tokens(left: int | None, right: int | None) -> int | None:
    if left is None or right is None:
        return None
    return left + right


def _split_point(text: str) -> int:
    middle = len(text) // 2
    cut = text.rfind(" ", 0, middle)
    if cut <= 0:
        cut = middle
    return cut


def _safe_json(body: bytes) -> dict[str, Any]:
    try:
        loaded = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _mentions_model(body: bytes) -> bool:
    text = body.decode("utf-8", "replace").lower()
    return "model" in text and ("not found" in text or "no such" in text or "pull" in text)


def _same_model(candidate: str, wanted: str) -> bool:
    if candidate == wanted:
        return True
    return candidate.split(":")[0] == wanted.split(":")[0]


def _extract_embedding_length(show: dict[str, Any]) -> int | None:
    model_info = show.get("model_info")
    if isinstance(model_info, dict):
        for key, value in model_info.items():
            if key.endswith("embedding_length") and isinstance(value, int):
                return value
    for key in ("embedding_length", "dimension"):
        value = show.get(key)
        if isinstance(value, int):
            return value
    return None
