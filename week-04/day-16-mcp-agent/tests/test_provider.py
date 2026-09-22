"""Unit tests of the provider event normalization (no network, no real client)."""

from __future__ import annotations

import asyncio
import json
import types
import unittest

import httpx
import openai

from agent.provider import (
    CATEGORY_MODEL_CREDENTIALS_REJECTED,
    CATEGORY_MODEL_HTTP_ERROR,
    CATEGORY_MODEL_NOT_CONFIGURED,
    CATEGORY_MODEL_NOT_FOUND,
    CATEGORY_MODEL_PROTOCOL_ERROR,
    CATEGORY_MODEL_TIMEOUT,
    CATEGORY_MODEL_UNREACHABLE,
    Finished,
    ModelError,
    OpenAICompatibleProvider,
    TextDelta,
    ToolCallDelta,
    _usage_dict,
)


class FakeStream:
    """An async iterator of pre-built chunks."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def close(self):
        self.closed = True


def _chunk(delta, finish_reason=None, usage=None, model=None):
    choice = types.SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return types.SimpleNamespace(choices=[choice], usage=usage, model=model)


def _tool_delta(index, call_id=None, name=None, arguments=None):
    function = types.SimpleNamespace(name=name, arguments=arguments)
    return types.SimpleNamespace(index=index, id=call_id, function=function)


def _status_error(status: int, message: str = "provider error", body=None):
    """Build a real ``openai.APIStatusError`` for the given HTTP status."""
    request = httpx.Request("POST", "http://127.0.0.1:1/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return openai.APIStatusError(message, response=response, body=body)


class FakeClient:
    """A minimal stand-in for ``openai.AsyncOpenAI``."""

    def __init__(self, chunks=None, error=None):
        self.chunks = chunks or []
        self.error = error
        self.calls = []
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create)
        )

    async def _create(self, **payload):
        self.calls.append(payload)
        if self.error is not None:
            raise self.error
        return FakeStream(self.chunks)


class StreamTest(unittest.IsolatedAsyncioTestCase):
    """Chunks become TextDelta, ToolCallDelta and Finished events."""

    async def _collect(self, provider):
        return [event async for event in provider.stream([{"role": "user"}], [])]

    async def test_text_deltas_and_finish(self):
        client = FakeClient(
            chunks=[
                _chunk(types.SimpleNamespace(content="Hel")),
                _chunk(types.SimpleNamespace(content="lo")),
                _chunk(types.SimpleNamespace(content=None), finish_reason="stop"),
                _chunk(types.SimpleNamespace(content=None), usage=None),
            ]
        )
        provider = OpenAICompatibleProvider(
            base_url="http://127.0.0.1:1/v1", model="m", api_key="k", client=client
        )
        events = await self._collect(provider)
        self.assertEqual(
            [event.text for event in events if isinstance(event, TextDelta)], ["Hel", "lo"]
        )
        self.assertIsInstance(events[-1], Finished)
        self.assertEqual(events[-1].finish_reason, "stop")

    async def test_tool_call_is_accumulated_from_pieces(self):
        client = FakeClient(
            chunks=[
                _chunk(
                    types.SimpleNamespace(
                        content=None,
                        tool_calls=[_tool_delta(0, "call_1", "calculate", "")],
                    )
                ),
                _chunk(
                    types.SimpleNamespace(
                        content=None,
                        tool_calls=[_tool_delta(0, None, None, '{"a": ')],
                    )
                ),
                _chunk(
                    types.SimpleNamespace(
                        content=None,
                        tool_calls=[_tool_delta(0, None, None, "1}")],
                    )
                ),
                _chunk(types.SimpleNamespace(content=None), finish_reason="tool_calls"),
            ]
        )
        provider = OpenAICompatibleProvider(
            base_url="http://127.0.0.1:1/v1", model="m", api_key="k", client=client
        )
        events = await self._collect(provider)
        deltas = [event for event in events if isinstance(event, ToolCallDelta)]
        self.assertEqual(deltas[0].id, "call_1")
        self.assertEqual(deltas[0].name, "calculate")
        self.assertEqual("".join(event.arguments for event in deltas), '{"a": 1}')
        self.assertEqual(events[-1].finish_reason, "tool_calls")

    async def test_reported_model_is_extracted_from_the_stream(self):
        client = FakeClient(
            chunks=[
                _chunk(types.SimpleNamespace(content="x"), model="reported-model"),
                _chunk(
                    types.SimpleNamespace(content=None),
                    finish_reason="stop",
                    model="reported-model",
                ),
            ]
        )
        provider = OpenAICompatibleProvider(
            base_url="http://127.0.0.1:1/v1", model="m", api_key="k", client=client
        )
        events = await self._collect(provider)
        self.assertEqual(events[-1].reported_model, "reported-model")

    async def test_missing_reported_model_is_none(self):
        client = FakeClient(
            chunks=[_chunk(types.SimpleNamespace(content="x"), finish_reason="stop")]
        )
        provider = OpenAICompatibleProvider(
            base_url="http://127.0.0.1:1/v1", model="m", api_key="k", client=client
        )
        events = await self._collect(provider)
        self.assertIsNone(events[-1].reported_model)

    async def test_tools_are_sent_only_when_present(self):
        client = FakeClient(
            chunks=[_chunk(types.SimpleNamespace(content=None), finish_reason="stop")]
        )
        provider = OpenAICompatibleProvider(
            base_url="http://127.0.0.1:1/v1", model="m", api_key="k", client=client
        )
        await self._collect(provider)
        self.assertNotIn("tools", client.calls[0])
        await self._collect_with_tools(provider, [{"type": "function"}])
        self.assertIn("tools", client.calls[1])
        self.assertEqual(client.calls[1]["tool_choice"], "auto")

    async def _collect_with_tools(self, provider, tools):
        return [event async for event in provider.stream([{"role": "user"}], tools)]

    async def test_usage_is_kept_and_partial_usage_does_not_invent_numbers(self):
        client = FakeClient(
            chunks=[
                _chunk(types.SimpleNamespace(content="x")),
                _chunk(
                    types.SimpleNamespace(content=None),
                    usage=types.SimpleNamespace(
                        prompt_tokens=1, completion_tokens=None, total_tokens=1
                    ),
                ),
            ]
        )
        provider = OpenAICompatibleProvider(
            base_url="http://127.0.0.1:1/v1", model="m", api_key="k", client=client
        )
        events = await self._collect(provider)
        self.assertEqual(events[-1].usage, {"prompt_tokens": 1, "total_tokens": 1})

    async def test_missing_usage_is_none(self):
        client = FakeClient(
            chunks=[_chunk(types.SimpleNamespace(content="x"), finish_reason="stop")]
        )
        provider = OpenAICompatibleProvider(
            base_url="http://127.0.0.1:1/v1", model="m", api_key="k", client=client
        )
        events = await self._collect(provider)
        self.assertIsNone(events[-1].usage)


class ErrorMappingTest(unittest.IsolatedAsyncioTestCase):
    """Provider failures become sanitized, explicitly categorized errors."""

    async def _collect(self, provider):
        return [event async for event in provider.stream([{"role": "user"}], [])]

    def _provider(self, *, client=None, error=None, api_key="k", configured=None):
        if client is None:
            client = FakeClient(error=error)
        return OpenAICompatibleProvider(
            base_url="http://127.0.0.1:1/v1",
            model="m",
            api_key=api_key,
            client=client,
            configured=configured,
        )

    async def test_connection_error_is_model_unreachable(self):
        provider = self._provider(error=ConnectionError("connection refused to somewhere"))
        with self.assertRaises(ModelError) as caught:
            await self._collect(provider)
        self.assertEqual(caught.exception.category, CATEGORY_MODEL_UNREACHABLE)
        self.assertNotIn("http://", str(caught.exception))

    async def test_connection_error_class_is_unreachable(self):
        provider = self._provider(error=ConnectionRefusedError("refused"))
        with self.assertRaises(ModelError) as caught:
            await self._collect(provider)
        self.assertEqual(caught.exception.category, CATEGORY_MODEL_UNREACHABLE)

    async def test_timeout_is_model_timeout(self):
        provider = self._provider(error=asyncio.TimeoutError())
        with self.assertRaises(ModelError) as caught:
            await self._collect(provider)
        self.assertEqual(caught.exception.category, CATEGORY_MODEL_TIMEOUT)

    async def test_401_is_credentials_rejected_and_not_unreachable(self):
        provider = self._provider(error=_status_error(401, "Unauthorized"))
        with self.assertRaises(ModelError) as caught:
            await self._collect(provider)
        self.assertEqual(caught.exception.category, CATEGORY_MODEL_CREDENTIALS_REJECTED)
        self.assertNotEqual(caught.exception.category, CATEGORY_MODEL_UNREACHABLE)
        self.assertNotIn("not reachable", str(caught.exception).lower())
        self.assertNotIn("http://", str(caught.exception))

    async def test_403_is_credentials_rejected(self):
        provider = self._provider(error=_status_error(403, "Forbidden"))
        with self.assertRaises(ModelError) as caught:
            await self._collect(provider)
        self.assertEqual(caught.exception.category, CATEGORY_MODEL_CREDENTIALS_REJECTED)

    async def test_404_is_model_not_found(self):
        provider = self._provider(error=_status_error(404, "Not Found"))
        with self.assertRaises(ModelError) as caught:
            await self._collect(provider)
        self.assertEqual(caught.exception.category, CATEGORY_MODEL_NOT_FOUND)
        self.assertIn("m", str(caught.exception))

    async def test_400_with_model_not_found_body_is_model_not_found(self):
        provider = self._provider(
            error=_status_error(400, "The model `m` does not exist")
        )
        with self.assertRaises(ModelError) as caught:
            await self._collect(provider)
        self.assertEqual(caught.exception.category, CATEGORY_MODEL_NOT_FOUND)

    async def test_other_status_is_model_http_error(self):
        provider = self._provider(error=_status_error(500, "Internal Server Error"))
        with self.assertRaises(ModelError) as caught:
            await self._collect(provider)
        self.assertEqual(caught.exception.category, CATEGORY_MODEL_HTTP_ERROR)

    async def test_unparsable_stream_without_choices_is_protocol_error(self):
        client = FakeClient(chunks=[types.SimpleNamespace(choices=None, usage=None)])
        provider = self._provider(client=client)
        with self.assertRaises(ModelError) as caught:
            await self._collect(provider)
        self.assertEqual(caught.exception.category, CATEGORY_MODEL_PROTOCOL_ERROR)

    async def test_invalid_json_response_is_protocol_error(self):
        provider = self._provider(error=json.JSONDecodeError("bad", "x", 0))
        with self.assertRaises(ModelError) as caught:
            await self._collect(provider)
        self.assertEqual(caught.exception.category, CATEGORY_MODEL_PROTOCOL_ERROR)

    async def test_error_during_iteration_is_mapped(self):
        class ExplodingStream(FakeStream):
            async def __anext__(self):
                raise asyncio.TimeoutError

        class Client(FakeClient):
            async def _create(self, **payload):
                return ExplodingStream([])

        provider = self._provider(client=Client())
        with self.assertRaises(ModelError) as caught:
            await self._collect(provider)
        self.assertEqual(caught.exception.category, CATEGORY_MODEL_TIMEOUT)

    async def test_not_configured_makes_no_http_request(self):
        client = FakeClient(
            chunks=[_chunk(types.SimpleNamespace(content="x"), finish_reason="stop")]
        )
        provider = self._provider(client=client, configured=False)
        with self.assertRaises(ModelError) as caught:
            await self._collect(provider)
        self.assertEqual(caught.exception.category, CATEGORY_MODEL_NOT_CONFIGURED)
        self.assertEqual(client.calls, [])

    async def test_error_message_never_contains_the_key(self):
        provider = self._provider(
            error=ConnectionError("secret sk-1234 refused"), api_key="sk-1234"
        )
        with self.assertRaises(ModelError) as caught:
            await self._collect(provider)
        self.assertNotIn("sk-1234", str(caught.exception))

    async def test_status_error_message_never_contains_the_key(self):
        provider = self._provider(
            error=_status_error(401, "sk-1234 rejected"),
            api_key="sk-1234",
        )
        with self.assertRaises(ModelError) as caught:
            await self._collect(provider)
        self.assertNotIn("sk-1234", str(caught.exception))


class UsageTest(unittest.TestCase):
    """``_usage_dict`` handles missing and partial usage."""

    def test_none_usage(self):
        self.assertIsNone(_usage_dict(None))

    def test_dict_usage_with_reasoning_details(self):
        value = _usage_dict(
            {
                "prompt_tokens": 1,
                "completion_tokens": 2,
                "total_tokens": 3,
                "completion_tokens_details": {"reasoning_tokens": 4},
            }
        )
        self.assertEqual(value["reasoning_tokens"], 4)
        self.assertEqual(value["total_tokens"], 3)

    def test_booleans_are_not_treated_as_numbers(self):
        self.assertIsNone(_usage_dict({"prompt_tokens": True}))


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
