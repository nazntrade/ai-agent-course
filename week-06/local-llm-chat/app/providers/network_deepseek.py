"""Network DeepSeek provider: own OpenAI-compatible server-side configuration.

The API key is read from the application's own ``.env`` and is only ever placed
in the outbound ``Authorization`` header. It is never echoed in errors, stored
in the database or sent to the browser (SPEC R3.5, I11).
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
    ProviderNotConfigured,
    ProviderTimeout,
    ProviderUnavailable,
)
from .base import AnswerProvider, ChatMessage, ChatResult, ChatUsage, ProviderStatus

_LENGTH_MARKERS = (
    "context length",
    "maximum context",
    "too many tokens",
    "input length",
    "input is too long",
)


class DeepSeekProvider(AnswerProvider):
    name = "network"

    def __init__(
        self,
        *,
        base_url: str,
        model_id: str,
        api_key: str,
        timeout: float = 120.0,
        max_output_tokens: int = 2048,
        temperature: float = 0.0,
        opener=None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self._api_key = api_key
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self._open = opener or urllib.request.urlopen

    # -- configuration ----------------------------------------------------
    def is_configured(self) -> bool:
        return not self.missing_config()

    def missing_config(self) -> list[str]:
        required = {
            "DEEPSEEK_BASE_URL": self.base_url,
            "DEEPSEEK_MODEL_ID": self.model_id,
            "DEEPSEEK_API_KEY": self._api_key,
        }
        return [name for name, value in required.items() if not str(value).strip()]

    def identity(self) -> dict[str, Any]:
        # The key is deliberately absent from the identity snapshot.
        return {
            "provider": self.name,
            "base_url": self.base_url,
            "model": self.model_id,
        }

    # -- transport --------------------------------------------------------
    def _request(self, path: str, payload: dict | None = None) -> dict:
        if not self.is_configured():
            raise ProviderNotConfigured(
                "The network provider is not configured.",
                details={"missing": self.missing_config()},
            )
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self._api_key,
        }
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
                raise ProviderTimeout("The network provider timed out.") from None
            if exc.code == 401:
                raise ProviderUnavailable("The network provider rejected the credentials.") from None
            text = exc.read(65536).decode("utf-8", "replace").lower()
            if exc.code == 413 or any(marker in text for marker in _LENGTH_MARKERS):
                raise ProviderLengthError("The network provider rejected the context as too long.") from None
            raise ProviderUnavailable("The network provider rejected the request.") from None
        except (TimeoutError, socket.timeout):
            raise ProviderTimeout("The network provider timed out.") from None
        except (OSError, urllib.error.URLError):
            raise ProviderUnavailable("The network provider is unreachable.") from None
        except ValueError:
            raise ProviderInvalidResponse("The network provider returned malformed JSON.") from None

    def preflight(self) -> dict[str, Any]:
        if not self.is_configured():
            return {"reachable": False, "model_present": False, "configured": False}
        try:
            data = self._request("/models")
        except (ProviderUnavailable, ProviderTimeout, ProviderInvalidResponse):
            return {"reachable": False, "model_present": False}
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            return {"reachable": False, "model_present": False}
        model_ids = [i.get("id") for i in data["data"] if isinstance(i, dict)]
        return {
            "reachable": True,
            "model_present": self.model_id in model_ids,
        }

    def status(self) -> ProviderStatus:
        if not self.is_configured():
            return ProviderStatus(
                provider=self.name,
                reachable=False,
                model=self.model_id or None,
                detail="missing configuration: " + ", ".join(self.missing_config()),
            )
        info = self.preflight()
        return ProviderStatus(
            provider=self.name,
            reachable=bool(info.get("reachable")),
            model=self.model_id,
            detail=None if info.get("reachable") else "network provider is not reachable",
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
        data = self._request("/chat/completions", payload)
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
            reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError):
            raise ProviderInvalidResponse("The network provider returned no answer.") from None
        if not isinstance(text, str) or not text.strip():
            raise ProviderInvalidResponse("The network provider returned an empty answer.")
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
