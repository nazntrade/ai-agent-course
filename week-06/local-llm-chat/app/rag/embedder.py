"""Embedding adapter, independent from the answer model (SPEC R5.4).

The embedder speaks the Ollama HTTP API (``/api/embed``) exactly like week-05's
retrieval, so the transferred index behavior is preserved. Vectors are L2
normalized; the document/query prefixes are applied only here.
"""

from __future__ import annotations

import json
import math
import socket
import urllib.error
import urllib.request
from typing import Any, Sequence

from ..errors import ProviderUnavailable


class OllamaEmbedder:
    provider = "ollama"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "embeddinggemma:300m",
        *,
        timeout: float = 60.0,
        document_prefix: str = "",
        query_prefix: str = "",
        transport=None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.document_prefix = document_prefix
        self.query_prefix = query_prefix
        self._transport = transport or self._urllib_transport

    @staticmethod
    def _urllib_transport(method: str, url: str, payload: Any, timeout: float) -> tuple[int, bytes]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (socket.timeout, urllib.error.URLError, OSError) as exc:
            raise ProviderUnavailable("The embedding provider is unreachable.") from exc

    def identity(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "document_prefix": self.document_prefix,
            "query_prefix": self.query_prefix,
        }

    def is_available(self) -> bool:
        return bool(self.preflight().get("reachable"))

    def preflight(self) -> dict[str, Any]:
        try:
            status, _ = self._transport("GET", self.base_url + "/api/tags", None, 3.0)
        except ProviderUnavailable:
            return {"reachable": False, "model_present": False}
        return {"reachable": 200 <= status < 300, "model_present": 200 <= status < 300}

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed([self.document_prefix + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._embed([self.query_prefix + text])[0]

    def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        payload = {"model": self.model, "input": list(texts)}
        status, body = self._transport("POST", self.base_url + "/api/embed", payload, self.timeout)
        if not (200 <= status < 300):
            raise ProviderUnavailable("The embedding provider rejected the request.")
        try:
            data = json.loads(body.decode("utf-8"))
            vectors = data.get("embeddings") or [data.get("embedding")]
            return [_l2([float(v) for v in vector]) for vector in vectors]
        except (ValueError, UnicodeDecodeError, TypeError, AttributeError):
            raise ProviderUnavailable("The embedding provider returned an invalid response.") from None


def _l2(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return list(vector)
    return [value / norm for value in vector]


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return float(sum(a * b for a, b in zip(left, right)))
