"""Strict local provider boundary; never log provider bodies or credentials."""
from __future__ import annotations

import asyncio
import codecs
import json
import os
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping
from urllib.parse import urlsplit

import httpx


class UpstreamError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class ProviderConfig:
    base_url: str
    api_key: str
    model_name: str
    lease_url: str
    parent_lease_id: str = ""

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> "ProviderConfig":
        env = os.environ if env is None else env
        if env.get("AI_TEST_LIVE_POLICY") != "allowed":
            raise UpstreamError("live_policy_blocked")
        keys = ("BASE_URL", "API_KEY", "NAME", "LEASE_URL", "ID", "PATH")
        values = {key: env.get("AI_TEST_MODEL_" + key, "") for key in keys}
        if env.get("AI_TEST_MODEL_KIND") != "local" or not all(values.values()):
            raise UpstreamError("local_profile_missing_or_partial")
        base, lease = urlsplit(values["BASE_URL"]), urlsplit(values["LEASE_URL"])
        if (base.scheme != "http" or base.hostname not in {"localhost", "127.0.0.1", "::1"}
                or base.username or base.password or base.query or base.fragment
                or base.path != "/api/local-models/" + values["ID"] + "/v1" or base.port is None):
            raise UpstreamError("invalid_provider_route")
        if (lease.scheme, lease.hostname, lease.port) != (base.scheme, base.hostname, base.port):
            raise UpstreamError("invalid_lease_route")
        if (lease.username or lease.password or lease.query or lease.fragment
                or not re.fullmatch(r"/api/local-models/[a-f0-9]{16}/test-leases", lease.path)
                or values["ID"] != lease.path.split("/")[3] or values["NAME"] != "local"):
            raise UpstreamError("invalid_lease_route")
        ready = env.get("AI_TEST_MODEL_PARENT_READY", "")
        lease_id = env.get("AI_TEST_MODEL_LEASE_ID", "")
        if (ready not in {"", "0", "1"} or (ready == "1") != bool(lease_id)
                or (lease_id and not re.fullmatch(r"[a-f0-9]{48}", lease_id))):
            raise UpstreamError("invalid_parent_lease")
        return cls(values["BASE_URL"].rstrip("/"), values["API_KEY"], values["NAME"],
                   values["LEASE_URL"], lease_id)

    def headers(self, lease_id: str = "") -> dict[str, str]:
        headers = {"Authorization": "Bearer " + self.api_key}
        if lease_id:
            headers["x-ai-test-model-lease-id"] = lease_id
        return headers


async def bounded_json(response: httpx.Response, cap: int = 65536) -> dict:
    if not 200 <= response.status_code < 300:
        raise UpstreamError("provider_http_error")
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > cap:
            raise UpstreamError("provider_response_too_large")
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        raise UpstreamError("provider_invalid_json") from None
    if not isinstance(value, dict):
        raise UpstreamError("provider_invalid_json")
    return value


class LeaseManager:
    """Keep a shared cold-start task alive through request cancellation."""
    def __init__(self, config: ProviderConfig, client: httpx.AsyncClient, *, renew_seconds: float = 60):
        self.config, self.client = config, client
        self.renew_seconds = renew_seconds
        self.lease_id = config.parent_lease_id
        self.acquire_task: asyncio.Task | None = None
        self.renew_task: asyncio.Task | None = None
        self.close_task: asyncio.Task | None = None
        self.renew_failed = False
        self.closing = False
        self.release_observation: dict = {"status": "NOT_ASSESSED"}

    async def _request(self, method: str, url: str, timeout: float) -> dict:
        try:
            async with asyncio.timeout(timeout + 10):
                async with self.client.stream(method, url, headers=self.config.headers(),
                                              timeout=httpx.Timeout(timeout, connect=10)) as response:
                    return await bounded_json(response)
        except (httpx.HTTPError, TimeoutError):
            raise UpstreamError("lease_transport_error") from None

    async def _acquire(self) -> str:
        value = await self._request("POST", self.config.lease_url, 270)
        lease_id = value.get("lease_id")
        ttl = value.get("expires_in_seconds")
        if not isinstance(lease_id, str) or not re.fullmatch(r"[a-f0-9]{48}", lease_id):
            raise UpstreamError("invalid_lease_response")
        # Preserve a valid returned lease before validating the remaining metadata.
        self.lease_id = lease_id
        if type(ttl) not in {int, float} or ttl <= self.renew_seconds * 2:
            raise UpstreamError("invalid_lease_ttl")
        if not self.closing:
            self.renew_task = asyncio.create_task(self._renew_loop())
        return lease_id

    async def ensure(self) -> str:
        if self.closing or self.renew_failed:
            raise UpstreamError("lease_unavailable")
        if self.lease_id:
            return self.lease_id
        if self.acquire_task is None:
            self.acquire_task = asyncio.create_task(self._acquire())
        return await asyncio.shield(self.acquire_task)

    async def renew(self) -> dict:
        if self.config.parent_lease_id:
            return {"renewed": False, "parent_owned": True}
        if not self.lease_id:
            raise UpstreamError("lease_unavailable")
        value = await self._request("PATCH", self.config.lease_url + "/" + self.lease_id, 15)
        if value.get("renewed") is not True:
            raise UpstreamError("invalid_renew_response")
        return {"renewed": True}

    async def _renew_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.renew_seconds)
                await self.renew()
        except UpstreamError:
            self.renew_failed = True

    async def close(self) -> dict:
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close())
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(self.close_task)
                break
            except asyncio.CancelledError:
                if self.close_task.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _close(self) -> dict:
        self.closing = True
        # _acquire owns its bounded transport timeout. Do not abandon a late lease.
        if self.acquire_task:
            try:
                await asyncio.shield(self.acquire_task)
            except UpstreamError:
                pass
        if self.renew_task:
            self.renew_task.cancel()
            await asyncio.gather(self.renew_task, return_exceptions=True)
        if self.config.parent_lease_id:
            self.release_observation = {"status": "PARENT_OWNED", "released": False}
        elif self.lease_id:
            value = await self._request("DELETE", self.config.lease_url + "/" + self.lease_id, 30)
            if value.get("released") is not True or value.get("disposition") not in {
                "shared", "retained", "deferred", "unloaded"
            }:
                raise UpstreamError("invalid_release_response")
            self.release_observation = {"status": "OBSERVED", "released": True,
                                        "disposition": value["disposition"]}
            self.lease_id = ""
        return self.release_observation


@dataclass
class StreamResult:
    content: str = ""
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    content_chunks: int = 0
    reasoning_characters: int = 0
    done: bool = False


class ChatStreamParser:
    """Incremental SSE parser with strict completion and bounded memory."""
    def __init__(self, *, event_cap: int = 131072, response_cap: int = 2097152):
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.buffer = ""
        self.data_lines: list[str] = []
        self.event_bytes = 0
        self.total_bytes = 0
        self.event_cap, self.response_cap = event_cap, response_cap
        self.result = StreamResult()

    def feed(self, chunk: bytes) -> list[str]:
        self.total_bytes += len(chunk)
        if self.total_bytes > self.response_cap:
            raise UpstreamError("stream_response_too_large")
        try:
            self.buffer += self.decoder.decode(chunk)
        except UnicodeError:
            raise UpstreamError("stream_invalid_utf8") from None
        deltas = []
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line.endswith("\r"):
                line = line[:-1]
            self.event_bytes += len(line.encode("utf-8")) + 1
            if self.event_bytes > self.event_cap:
                raise UpstreamError("stream_event_too_large")
            if line == "":
                if self.data_lines:
                    delta = self._event("\n".join(self.data_lines))
                    if delta:
                        deltas.append(delta)
                self.data_lines, self.event_bytes = [], 0
            elif line.startswith("data:"):
                value = line[5:]
                self.data_lines.append(value[1:] if value.startswith(" ") else value)
            elif line.startswith(":"):
                continue
        if len(self.buffer.encode("utf-8")) + self.event_bytes > self.event_cap:
            raise UpstreamError("stream_event_too_large")
        return deltas

    def _event(self, data: str) -> str:
        result = self.result
        if result.done:
            raise UpstreamError("stream_data_after_done")
        if data == "[DONE]":
            if result.finish_reason not in {"stop", "length"} or not result.content.strip():
                raise UpstreamError("stream_early_done")
            result.done = True
            return ""
        try:
            event = json.loads(data)
        except ValueError:
            raise UpstreamError("stream_invalid_json") from None
        if not isinstance(event, dict) or "error" in event:
            raise UpstreamError("stream_invalid_event")
        choices = event.get("choices")
        usage = event.get("usage")
        if usage is not None:
            if not isinstance(usage, dict) or type(usage.get("prompt_tokens")) is not int or usage["prompt_tokens"] < 1:
                raise UpstreamError("stream_invalid_usage")
            if result.prompt_tokens is not None and result.prompt_tokens != usage["prompt_tokens"]:
                raise UpstreamError("stream_conflicting_usage")
            result.prompt_tokens = usage["prompt_tokens"]
        if choices == [] and usage is not None:
            return ""
        if not isinstance(choices, list) or len(choices) != 1:
            raise UpstreamError("stream_invalid_choices")
        choice = choices[0]
        if (not isinstance(choice, dict) or type(choice.get("index")) is not int
                or choice["index"] != 0 or not isinstance(choice.get("delta"), dict)):
            raise UpstreamError("stream_invalid_choice")
        delta = choice["delta"]
        if any(key not in {"role", "content", "reasoning_content", "reasoning"} for key in delta):
            raise UpstreamError("stream_unsupported_delta")
        if delta.get("role") not in {None, "assistant"}:
            raise UpstreamError("stream_invalid_role")
        content = delta.get("content")
        if content is not None and not isinstance(content, str):
            raise UpstreamError("stream_invalid_content")
        for key in ("reasoning_content", "reasoning"):
            if delta.get(key) is not None:
                if not isinstance(delta[key], str):
                    raise UpstreamError("stream_invalid_reasoning")
                result.reasoning_characters += len(delta[key])
        if content:
            if result.finish_reason is not None:
                raise UpstreamError("stream_content_after_finish")
            result.content += content
            result.content_chunks += 1
        reason = choice.get("finish_reason")
        if reason is not None:
            if reason not in {"stop", "length"} or result.finish_reason is not None:
                raise UpstreamError("stream_invalid_finish")
            result.finish_reason = reason
        return content or ""

    def finish(self) -> StreamResult:
        try:
            tail = self.decoder.decode(b"", final=True)
        except UnicodeError:
            raise UpstreamError("stream_invalid_utf8") from None
        if tail or self.buffer.strip() or self.data_lines or not self.result.done:
            raise UpstreamError("stream_incomplete")
        return self.result


def chat_body(messages: list[dict[str, str]], *, output_tokens: int = 1024) -> dict:
    return {"model": "local", "messages": messages, "max_tokens": output_tokens,
            "temperature": 0.2, "stream": True, "stream_options": {"include_usage": True},
            "reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False}}


class LocalProvider:
    def __init__(self, config: ProviderConfig, client: httpx.AsyncClient, lease: LeaseManager):
        self.config, self.client, self.lease = config, client, lease

    async def count(self, body: dict) -> dict:
        lease_id = await self.lease.ensure()
        try:
            async with self.client.stream("POST", self.config.base_url + "/token-count", json=body,
                                          headers=self.config.headers(lease_id), timeout=45) as response:
                value = await bounded_json(response)
        except httpx.HTTPError:
            raise UpstreamError("count_transport_error") from None
        if (type(value.get("input_tokens")) is not int or value["input_tokens"] < 1
                or type(value.get("context_capacity")) is not int or value["context_capacity"] < 1
                or value.get("method") != "native-chat-input-tokens"
                or value.get("context_source") != "runtime-slot-props"):
            raise UpstreamError("invalid_token_count")
        return value

    async def stream(self, body: dict, on_content: Callable[[str], Awaitable[None]] | None = None) -> StreamResult:
        lease_id = await self.lease.ensure()
        parser = ChatStreamParser()
        try:
            async with asyncio.timeout(300):
                async with self.client.stream("POST", self.config.base_url + "/chat/completions", json=body,
                                              headers=self.config.headers(lease_id),
                                              timeout=httpx.Timeout(60, connect=10)) as response:
                    if response.status_code != 200:
                        raise UpstreamError("inference_http_error")
                    if response.headers.get("content-type", "").split(";")[0].strip() != "text/event-stream":
                        raise UpstreamError("inference_not_stream")
                    async for chunk in response.aiter_bytes():
                        if self.lease.renew_failed:
                            raise UpstreamError("lease_renew_failed")
                        for delta in parser.feed(chunk):
                            if on_content:
                                await on_content(delta)
                    return parser.finish()
        except (httpx.HTTPError, TimeoutError):
            raise UpstreamError("inference_transport_error") from None
