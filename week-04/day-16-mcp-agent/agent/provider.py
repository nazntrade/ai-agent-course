"""Model provider abstraction.

The agent talks to an OpenAI-compatible ``/v1`` endpoint through the
:class:`ModelProvider` protocol. The concrete implementation wraps
``openai.AsyncOpenAI`` in streaming mode and normalizes the chunks into the
small event vocabulary of :mod:`agent.orchestrator`: text deltas, tool-call
deltas and a terminal ``Finished`` event.

Every failure becomes a :class:`ModelError` with one explicit category (no API
key, unreachable, timeout, credentials rejected, model not found, protocol
error, other HTTP error). The messages are sanitized: no URL, no API key, no
raw provider payload. A missing configuration is reported as
``model_not_configured`` before any HTTP request is made.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol, runtime_checkable

CATEGORY_MODEL_NOT_CONFIGURED = "model_not_configured"
CATEGORY_MODEL_UNREACHABLE = "model_unreachable"
CATEGORY_MODEL_TIMEOUT = "model_timeout"
CATEGORY_MODEL_CREDENTIALS_REJECTED = "model_credentials_rejected"
CATEGORY_MODEL_NOT_FOUND = "model_not_found"
CATEGORY_MODEL_PROTOCOL_ERROR = "model_protocol_error"
CATEGORY_MODEL_HTTP_ERROR = "model_http_error"

MODEL_NOT_CONFIGURED_MESSAGE = (
    "The model is not configured. Copy .env.example to .env and set the API key."
)


class ModelError(Exception):
    """A sanitized model provider failure with one explicit category."""

    category = CATEGORY_MODEL_HTTP_ERROR

    def __init__(self, message: str, category: str | None = None):
        super().__init__(message)
        self.message = str(message)
        if category:
            self.category = category

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


@dataclass(frozen=True)
class TextDelta:
    """A piece of streamed assistant text."""

    text: str


@dataclass(frozen=True)
class ToolCallDelta:
    """A piece of a streamed tool call.

    ``index`` identifies the call inside the response, ``id`` and ``name`` are
    only present on the first chunk, ``arguments`` accumulates the JSON string.
    """

    index: int
    id: str | None = None
    name: str | None = None
    arguments: str = ""


@dataclass(frozen=True)
class Finished:
    """The end of one model response.

    ``reported_model`` is the model id the provider echoed on the stream; it is
    ``None`` when the provider sent no ``model`` field.
    """

    finish_reason: str | None = None
    usage: dict | None = None
    reported_model: str | None = None


ModelEvent = TextDelta | ToolCallDelta | Finished


@runtime_checkable
class ModelProvider(Protocol):
    """The provider surface the orchestrator depends on."""

    name: str
    model: str
    configured: bool

    def stream(
        self, messages: list, tools: list, *, timeout_s: float | None = None
    ) -> AsyncIterator[ModelEvent]: ...


def _usage_dict(usage: Any) -> dict | None:
    """Normalize a provider usage object into a plain dictionary.

    A provider may omit ``usage`` entirely or send a partial object; both are
    represented without inventing numbers.
    """
    if usage is None:
        return None
    fields = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "reasoning_tokens",
    )
    result: dict = {}
    for field in fields:
        if isinstance(usage, dict):
            value = usage.get(field)
        else:
            value = getattr(usage, field, None)
        if isinstance(value, int) and not isinstance(value, bool):
            result[field] = value
    details = getattr(usage, "completion_tokens_details", None)
    if details is not None:
        reasoning = getattr(details, "reasoning_tokens", None)
        if isinstance(reasoning, int) and not isinstance(reasoning, bool):
            result["reasoning_tokens"] = reasoning
    if isinstance(usage, dict) and isinstance(usage.get("completion_tokens_details"), dict):
        reasoning = usage["completion_tokens_details"].get("reasoning_tokens")
        if isinstance(reasoning, int) and not isinstance(reasoning, bool):
            result["reasoning_tokens"] = reasoning
    return result or None


def _iter_tool_calls(delta: Any) -> list:
    calls = getattr(delta, "tool_calls", None)
    if calls is None and isinstance(delta, dict):
        calls = delta.get("tool_calls")
    return list(calls or [])


def _call_pieces(call: Any) -> tuple[int, str | None, str | None, str]:
    """Extract ``(index, id, name, arguments)`` from one streamed tool call."""
    index = 0
    if isinstance(call, dict):
        raw_index = call.get("index")
        call_id = call.get("id")
        function = call.get("function") or {}
        name = function.get("name") if isinstance(function, dict) else None
        arguments = function.get("arguments") if isinstance(function, dict) else ""
    else:
        raw_index = getattr(call, "index", None)
        call_id = getattr(call, "id", None)
        function = getattr(call, "function", None)
        name = getattr(function, "name", None)
        arguments = getattr(function, "arguments", None)
    if isinstance(raw_index, int) and not isinstance(raw_index, bool):
        index = raw_index
    return index, call_id, name, arguments or ""


def _delta_content(delta: Any) -> str:
    if isinstance(delta, dict):
        content = delta.get("content")
    else:
        content = getattr(delta, "content", None)
    return content if isinstance(content, str) else ""


def _model_not_found_hint(item: Any) -> bool:
    """Whether an HTTP 400 body names a missing/unknown model.

    Only used to choose a category; the text itself never reaches a message.
    """
    parts = []
    for name in ("message", "body"):
        value = getattr(item, name, None)
        if value is not None:
            parts.append(str(value))
    text = " ".join(parts).lower()
    return any(
        marker in text
        for marker in (
            "model not found",
            "model_not_found",
            "unknown model",
            "no such model",
            "does not exist",
        )
    )


class OpenAICompatibleProvider:
    """Streaming provider for any OpenAI-compatible ``/v1`` endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        name: str = "openai-compatible",
        client: Any = None,
        default_timeout_s: float = 120.0,
        configured: bool | None = None,
    ):
        self.name = name
        self.model = str(model)
        self._base_url = str(base_url)
        self._api_key = str(api_key)
        self._client = client
        self._default_timeout_s = float(default_timeout_s)
        if configured is None:
            configured = bool(self._api_key.strip())
        self.configured = bool(configured)

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise ModelError(
                "The OpenAI client library is not installed",
                CATEGORY_MODEL_HTTP_ERROR,
            ) from exc
        self._client = AsyncOpenAI(
            base_url=self._base_url,
            api_key=self._api_key,
            max_retries=0,
        )
        return self._client

    def _status_error(self, item: Any) -> ModelError:
        status = getattr(item, "status_code", None)
        if status in (401, 403):
            return ModelError(
                "The model endpoint rejected the configured credentials",
                CATEGORY_MODEL_CREDENTIALS_REJECTED,
            )
        if status == 404 or (status == 400 and _model_not_found_hint(item)):
            return ModelError(
                f"The model '{self.model}' was not found",
                CATEGORY_MODEL_NOT_FOUND,
            )
        if status is not None:
            return ModelError(
                f"The model endpoint returned status {status}",
                CATEGORY_MODEL_HTTP_ERROR,
            )
        return ModelError("The model request failed", CATEGORY_MODEL_HTTP_ERROR)

    def _map_error(self, exc: BaseException) -> ModelError:
        if isinstance(exc, ModelError):
            return exc
        leaves = _leaf_exceptions(exc) or [exc]
        names = {type(item).__name__ for item in leaves}
        if "TimeoutError" in names or "TimeoutException" in names:
            return ModelError(
                "The model did not answer within the timeout", CATEGORY_MODEL_TIMEOUT
            )
        if names & {"JSONDecodeError", "ValidationError", "UnicodeDecodeError"}:
            return ModelError(
                "The model endpoint returned an unexpected response",
                CATEGORY_MODEL_PROTOCOL_ERROR,
            )
        try:
            import openai
        except ImportError:  # pragma: no cover - dependency is pinned
            return ModelError("The model request failed", CATEGORY_MODEL_HTTP_ERROR)
        for item in leaves:
            if isinstance(item, openai.APITimeoutError):
                return ModelError(
                    "The model did not answer within the timeout",
                    CATEGORY_MODEL_TIMEOUT,
                )
            if isinstance(item, openai.APIConnectionError):
                return ModelError(
                    "The model endpoint is not reachable", CATEGORY_MODEL_UNREACHABLE
                )
            if isinstance(item, openai.APIStatusError):
                return self._status_error(item)
        if names & {
            "ConnectError",
            "ConnectionRefusedError",
            "ConnectionError",
            "ConnectionResetError",
            "OSError",
            "SSLError",
        }:
            return ModelError(
                "The model endpoint is not reachable", CATEGORY_MODEL_UNREACHABLE
            )
        return ModelError("The model request failed", CATEGORY_MODEL_HTTP_ERROR)

    async def stream(
        self, messages: list, tools: list, *, timeout_s: float | None = None
    ) -> AsyncIterator[ModelEvent]:
        """Stream one model response as normalized events."""
        if not self.configured:
            raise ModelError(MODEL_NOT_CONFIGURED_MESSAGE, CATEGORY_MODEL_NOT_CONFIGURED)

        client = self._get_client()
        payload: dict = {
            "model": self.model,
            "messages": list(messages or []),
            "stream": True,
        }
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"
        payload["stream_options"] = {"include_usage": True}
        payload["timeout"] = (
            float(timeout_s) if timeout_s is not None else self._default_timeout_s
        )

        try:
            stream = await client.chat.completions.create(**payload)
        except Exception as exc:  # noqa: BLE001 - mapped to a sanitized error
            raise self._map_error(exc) from exc

        finish_reason: str | None = None
        usage: dict | None = None
        reported_model: str | None = None
        saw_choices = False
        try:
            async for chunk in stream:
                chunk_model = getattr(chunk, "model", None)
                if isinstance(chunk_model, str) and chunk_model:
                    reported_model = chunk_model
                choices = getattr(chunk, "choices", None)
                if choices is None and isinstance(chunk, dict):
                    choices = chunk.get("choices")
                if choices:
                    saw_choices = True
                for choice in choices or []:
                    if isinstance(choice, dict):
                        delta = choice.get("delta")
                        reason = choice.get("finish_reason")
                    else:
                        delta = getattr(choice, "delta", None)
                        reason = getattr(choice, "finish_reason", None)
                    if isinstance(reason, str) and reason:
                        finish_reason = reason
                    if delta is None:
                        continue
                    content = _delta_content(delta)
                    if content:
                        yield TextDelta(content)
                    for call in _iter_tool_calls(delta):
                        index, call_id, name, arguments = _call_pieces(call)
                        yield ToolCallDelta(
                            index=index, id=call_id, name=name, arguments=arguments
                        )
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is None and isinstance(chunk, dict):
                    chunk_usage = chunk.get("usage")
                if chunk_usage is not None:
                    usage = _usage_dict(chunk_usage)
        except Exception as exc:  # noqa: BLE001 - mapped to a sanitized error
            raise self._map_error(exc) from exc
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:  # pragma: no cover - best-effort cleanup
                    pass

        if not saw_choices and finish_reason is None:
            raise ModelError(
                "The model endpoint returned an unexpected response",
                CATEGORY_MODEL_PROTOCOL_ERROR,
            )

        yield Finished(
            finish_reason=finish_reason,
            usage=usage,
            reported_model=reported_model,
        )


def _leaf_exceptions(exc: BaseException) -> list:
    """Flatten an exception group into its leaf exceptions."""
    leaves: list = []
    pending = [exc]
    seen = 0
    while pending and seen < 100:
        current = pending.pop()
        seen += 1
        children = getattr(current, "exceptions", None)
        if children:
            pending.extend(children)
        else:
            leaves.append(current)
    return leaves
