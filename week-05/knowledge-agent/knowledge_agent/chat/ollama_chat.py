"""Ollama chat adapter (``POST /api/chat``), SPEC D22 16.

HTTP access goes through an injectable ``transport`` (stdlib ``urllib`` by
default) so unit tests never touch the network. ``done_reason``/usage are read
from the provider verbatim; missing values are never invented.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Mapping, Sequence

from ..domain.contracts import (
    ChatMessage,
    ChatModel,
    ChatModelIdentity,
    ChatResult,
    ChatUsage,
)
from ..domain.errors import (
    ChatInvalidResponse,
    ChatLengthError,
    ChatModelMissing,
    ChatTimeout,
    ChatUnavailable,
)

Transport = Callable[[str, str, Any, float], tuple[int, bytes]]


class TransportError(Exception):
    """Network-level failure of the injected transport."""


class TransportTimeout(TransportError):
    """Timeout of the injected transport."""


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
    "too many tokens",
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class OllamaChatModel(ChatModel):
    provider = "ollama"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "",
        *,
        timeout: float = 120.0,
        max_output_tokens: int = 1024,
        context_tokens: int = 8192,
        temperature: float = 0.0,
        seed: int = 0,
        transport: Transport | None = None,
        preflight_ttl: float = 5.0,
        stream_opener: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self.context_tokens = context_tokens
        self.temperature = temperature
        self.seed = seed
        self._transport = transport or urllib_transport
        self._stream_opener = stream_opener or (urllib.request.urlopen if transport is None else None)
        self._preflight_ttl = preflight_ttl
        self._preflight_cache: dict[str, Any] | None = None
        self._preflight_at = 0.0
        self._observed_digest: str | None = None
        self._observed_context_length: int | None = None

    # -- defaults ---------------------------------------------------------
    @property
    def default_options(self) -> dict[str, Any]:
        return {
            "temperature": self.temperature,
            "seed": self.seed,
            "num_ctx": self.context_tokens,
            "num_predict": self.max_output_tokens,
        }

    def _options(self, options: Mapping[str, Any] | None) -> dict[str, Any]:
        merged = dict(self.default_options)
        if options:
            merged.update(dict(options))
        return merged

    # -- HTTP -------------------------------------------------------------
    def _request(
        self, method: str, path: str, payload: Any = None, timeout: float | None = None
    ) -> tuple[int, bytes]:
        url = self.base_url + path
        try:
            return self._transport(method, url, payload, timeout or self.timeout)
        except TransportTimeout as exc:
            raise ChatTimeout("The chat provider timed out.") from exc
        except TransportError as exc:
            raise ChatUnavailable("The chat provider is not reachable.") from exc

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
            "context_length": None,
            "api": "/api/chat",
            "warnings": [],
        }
        try:
            status, body = self._request("GET", "/api/version", None, 3.0)
        except (ChatUnavailable, ChatTimeout):
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
                for model in _safe_json(body).get("models", []):
                    if _same_model(str(model.get("name", "")), self.model):
                        info["model_present"] = True
                        if model.get("digest"):
                            info["digest"] = model.get("digest")
                            self._observed_digest = model.get("digest")
                        break
            except (ChatUnavailable, ChatTimeout):
                info["warnings"].append("tags endpoint unreachable")
            try:
                status, body = self._request("POST", "/api/show", {"model": self.model}, 5.0)
                if 200 <= status < 300:
                    show = _safe_json(body)
                    if show.get("digest"):
                        info["digest"] = show.get("digest")
                        self._observed_digest = show.get("digest")
                    length = _extract_context_length(show)
                    if length is not None:
                        info["context_length"] = length
                        self._observed_context_length = length
            except (ChatUnavailable, ChatTimeout):
                info["warnings"].append("show endpoint unreachable")

        self._cache(info)
        return info

    def _cache(self, info: dict[str, Any]) -> None:
        self._preflight_cache = info
        self._preflight_at = time.monotonic()

    def is_available(self) -> bool:
        return bool(self.preflight().get("reachable"))

    def identity(self) -> ChatModelIdentity:
        info = self.preflight()
        return ChatModelIdentity(
            provider=self.provider,
            base_url=self.base_url,
            model=self.model,
            digest=info.get("digest") or self._observed_digest,
            context_length=info.get("context_length") or self._observed_context_length,
            default_options=self.default_options,
        )

    # -- chat -------------------------------------------------------------
    def chat(
        self, messages: Sequence[ChatMessage], options: Mapping[str, Any] | None = None
    ) -> ChatResult:
        payload = {
            "model": self.model,
            "messages": [message.to_dict() for message in messages],
            "stream": False,
            "options": self._options(options),
        }
        started = time.perf_counter()
        status, body = self._request("POST", "/api/chat", payload)
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        self._raise_for_status(status, body)

        document = _safe_json(body)
        text = _message_content(document)
        if not isinstance(text, str) or text == "":
            raise ChatInvalidResponse("The chat provider returned an empty answer.")
        return self._result(text, document, latency_ms)

    def stream_chat(
        self, messages: Sequence[ChatMessage], options: Mapping[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        payload = {
            "model": self.model,
            "messages": [message.to_dict() for message in messages],
            "stream": True,
            "options": self._options(options),
        }
        started = time.perf_counter()
        response = None
        if self._stream_opener is None:
            status, body = self._request("POST", "/api/chat", payload)
            self._raise_for_status(status, body)
            lines = body.splitlines()
        else:
            request = urllib.request.Request(self.base_url + "/api/chat",
                data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
            try:
                response = self._stream_opener(request, timeout=self.timeout)
                lines = response
            except urllib.error.HTTPError as exc:
                self._raise_for_status(exc.code, exc.read())
            except (TimeoutError, socket.timeout):
                raise ChatTimeout("The chat provider timed out.") from None
            except (OSError, urllib.error.URLError):
                raise ChatUnavailable("The chat provider is unreachable.") from None

        pieces: list[str] = []
        done_document: dict[str, Any] | None = None
        try:
            yield from self._stream_lines(lines, pieces, started)
        except (TimeoutError, socket.timeout):
            raise ChatTimeout("The chat stream timed out.") from None
        except (OSError, urllib.error.URLError):
            raise ChatUnavailable("The chat stream was interrupted.") from None
        finally:
            if response is not None:
                response.close()

    def _stream_lines(self, lines, pieces, started):
        done_document = None
        for raw in lines:
            line = raw.decode("utf-8", "replace")
            line = line.strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except ValueError as exc:
                raise ChatInvalidResponse(
                    "The chat provider returned a malformed stream chunk."
                ) from exc
            if not isinstance(chunk, dict):
                continue
            if chunk.get("error"):
                self._raise_for_error_text(str(chunk.get("error")))
            content = _message_content(chunk)
            if isinstance(content, str) and content:
                pieces.append(content)
                yield {"type": "token", "text": content}
            if chunk.get("done"):
                done_document = chunk

        if done_document is None:
            raise ChatInvalidResponse("The chat stream ended without a done chunk.")
        # Symmetry with ``chat()``: an empty aggregate is never a success, while a
        # non-empty ``length`` answer keeps its finish reason.
        text = "".join(pieces)
        if not text.strip():
            raise ChatInvalidResponse("The chat provider returned an empty answer.")
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        result = self._result(text, done_document, latency_ms)
        yield {"type": "done", "result": result}

    def _result(self, text: str, document: Mapping[str, Any], latency_ms: float) -> ChatResult:
        finish_reason = document.get("done_reason")
        if finish_reason is None:
            finish_reason = None
        usage = _usage_from(document)
        rate: float | None = None
        eval_count = document.get("eval_count")
        eval_duration = document.get("eval_duration")
        if isinstance(eval_count, int) and isinstance(eval_duration, int) and eval_duration > 0:
            rate = round(eval_count / (eval_duration / 1e9), 3)
        return ChatResult(
            text=text,
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
            usage=usage,
            model=str(document.get("model") or self.model),
            created_at=_utcnow(),
            latency_ms=latency_ms,
            output_tokens_per_second=rate,
        )

    def _raise_for_status(self, status: int, body: bytes) -> None:
        if 200 <= status < 300:
            return
        self._raise_for_error_text(body.decode("utf-8", "replace"), status)

    def _raise_for_error_text(self, text: str, status: int | None = None) -> None:
        lowered = text.lower()
        if any(marker in lowered for marker in _LENGTH_MARKERS):
            raise ChatLengthError("The chat provider rejected the input as too long.")
        if _mentions_model(text):
            raise ChatModelMissing(
                f"The chat model is not available: {self.model}.",
                details={"hint": f"ollama pull {self.model}"},
            )
        if status in (408, 504):
            raise ChatTimeout(f"The chat provider timed out ({status}).")
        raise ChatUnavailable("The chat provider returned an unexpected response.")


def _message_content(document: Mapping[str, Any]) -> Any:
    message = document.get("message")
    if isinstance(message, dict):
        return message.get("content")
    if isinstance(document.get("response"), str):
        return document.get("response")
    return None


def _usage_from(document: Mapping[str, Any]) -> ChatUsage | None:
    input_tokens = document.get("prompt_eval_count")
    output_tokens = document.get("eval_count")
    input_tokens = input_tokens if isinstance(input_tokens, int) else None
    output_tokens = output_tokens if isinstance(output_tokens, int) else None
    if input_tokens is None and output_tokens is None:
        return None
    total: int | None = None
    if input_tokens is not None or output_tokens is not None:
        total = (input_tokens or 0) + (output_tokens or 0)
    return ChatUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total,
    )


def _safe_json(body: bytes) -> dict[str, Any]:
    try:
        loaded = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _mentions_model(text: str) -> bool:
    lowered = text.lower()
    return "model" in lowered and ("not found" in lowered or "no such" in lowered or "pull" in lowered)


def _same_model(candidate: str, wanted: str) -> bool:
    if not wanted:
        return False
    if candidate == wanted:
        return True
    return candidate.split(":")[0] == wanted.split(":")[0]


def _extract_context_length(show: Mapping[str, Any]) -> int | None:
    model_info = show.get("model_info")
    if isinstance(model_info, dict):
        for key, value in model_info.items():
            if key.endswith("context_length") and isinstance(value, int):
                return value
    for key in ("context_length", "num_ctx"):
        value = show.get(key)
        if isinstance(value, int):
            return value
    return None

