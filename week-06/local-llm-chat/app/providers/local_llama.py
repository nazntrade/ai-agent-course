"""Local Gemma provider: OpenAI-compatible llama-server endpoint on loopback.

This adapter never starts or stops a process; process ownership lives in
``app.local_process``. It only speaks HTTP to an already-available server.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Mapping, Sequence

from ..errors import (
    ProviderInvalidResponse,
    ProviderLengthError,
    ProviderTimeout,
    ProviderUnavailable,
)
from .base import AnswerProvider, ChatMessage, ChatResult, ChatUsage, ProviderStatus

_LENGTH_MARKERS = (
    "context length",
    "context_length_exceeded",
    "maximum context",
    "context window",
    "too many tokens",
    "input length",
    "input is too long",
)


class LocalLlamaProvider(AnswerProvider):
    name = "local"

    def __init__(
        self,
        *,
        base_url: str,
        model_id: str,
        timeout: float = 180.0,
        max_output_tokens: int = 1024,
        temperature: float = 0.0,
        opener=None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self._open = opener or urllib.request.urlopen

    # -- configuration ----------------------------------------------------
    def is_configured(self) -> bool:
        return bool(self.base_url and self.model_id)

    def missing_config(self) -> list[str]:
        required = {"GEMMA_MODEL_ID": self.model_id}
        return [name for name, value in required.items() if not str(value).strip()]

    def identity(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "base_url": self.base_url,
            "model": self.model_id,
        }

    # -- transport --------------------------------------------------------
    def _request(self, path: str, payload: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers=headers,
        )
        try:
            with self._open(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (408, 504):
                raise ProviderTimeout("The local model timed out.") from None
            text = exc.read(65536).decode("utf-8", "replace").lower()
            if exc.code == 413 or any(marker in text for marker in _LENGTH_MARKERS):
                raise ProviderLengthError("The local model rejected the context as too long.") from None
            raise ProviderUnavailable("The local model rejected the request.") from None
        except (TimeoutError, socket.timeout):
            raise ProviderTimeout("The local model timed out.") from None
        except (OSError, urllib.error.URLError):
            raise ProviderUnavailable("The local model is unreachable.") from None
        except ValueError:
            raise ProviderInvalidResponse("The local model returned malformed JSON.") from None

    def preflight(self) -> dict[str, Any]:
        info = {"reachable": False, "model_present": False, "model": None}
        try:
            data = self._request("/v1/models")
        except (ProviderUnavailable, ProviderTimeout, ProviderInvalidResponse):
            return info
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            return info
        info["reachable"] = True
        for item in data["data"]:
            if isinstance(item, dict) and item.get("id"):
                info["model_present"] = True
                info["model"] = item.get("id")
                break
        return info

    def status(self) -> ProviderStatus:
        info = self.preflight()
        return ProviderStatus(
            provider=self.name,
            reachable=bool(info.get("reachable")),
            model=info.get("model") or self.model_id,
            detail=None if info.get("reachable") else "local server is not reachable",
        )

    # -- generation -------------------------------------------------------
    def chat(
        self, messages: Sequence[ChatMessage], options: Mapping[str, Any] | None = None
    ) -> ChatResult:
        opts = dict(options or {})
        max_tokens = int(opts.get("max_output_tokens", self.max_output_tokens))
        temperature = float(opts.get("temperature", self.temperature))
        payload = {
            "model": self.model_id,
            "messages": [m.to_dict() for m in messages],
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        started = time.perf_counter()
        data = self._request("/v1/chat/completions", payload)
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
            reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError):
            raise ProviderInvalidResponse("The local model returned no answer.") from None
        if not isinstance(text, str) or not text.strip():
            # An empty answer is never a success (SPEC R7.3).
            raise ProviderInvalidResponse("The local model returned an empty answer.")
        usage = data.get("usage") if isinstance(data, dict) else None
        return ChatResult(
            text=text,
            model=data.get("model") or self.model_id,
            finish_reason=reason if isinstance(reason, str) else None,
            usage=_parse_usage(usage),
            latency_ms=latency_ms,
            parameters={
                "temperature": temperature,
                "max_output_tokens": max_tokens,
            },
        )


def _parse_usage(usage: Any) -> ChatUsage | None:
    if not isinstance(usage, dict):
        return None

    def integer(name: str) -> int | None:
        value = usage.get(name)
        return value if type(value) is int and value >= 0 else None

    values = [integer(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")]
    if any(v is not None for v in values):
        return ChatUsage(*values)
    return None
