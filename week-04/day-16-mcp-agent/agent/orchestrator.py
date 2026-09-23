"""The tool-calling loop that turns one user message into a streamed answer.

The flow of one request is:

``status(accepted)`` → MCP status → MCP tools list → ``status(model_calling)``
→ model turn → (tool_calls?) → ``tool_call`` → MCP ``tools/call`` →
``tool_result`` → next model turn → ``delta`` … → ``done``.

Every step is mirrored into the JSONL trace with the same ``request_id``, so
the harness can prove the real path *model → MCP tool → model*. A failure
produces an ``error`` event and never a fabricated successful answer.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import AsyncIterator

from agent import tool_schema
from agent.mcp_adapter import McpClient, McpError, tool_result_as_text
from agent.provider import (
    CATEGORY_MODEL_NOT_CONFIGURED,
    MODEL_NOT_CONFIGURED_MESSAGE,
    Finished,
    ModelError,
    ModelProvider,
    TextDelta,
    ToolCallDelta,
)
from agent.sessions import Session
from agent.trace import NullTraceWriter

SYSTEM_PROMPT = (
    "You are a helpful assistant with access to MCP tools. "
    "When the user asks for arithmetic, call the 'calculate' tool instead of "
    "computing the result yourself. Answer in the language of the user. "
    "When the user asks to find something online or needs current facts, call the "
    "'search_web' tool instead of answering from memory. "
    "The 'search_web' result contains only titles, links and short snippets: never "
    "claim to have opened or read the pages, and never invent facts, quotes or links "
    "that the tool did not return. "
    "When you use search results, include the sources as Markdown links [title](url). "
    "If a tool returns an error, report it briefly; do not fabricate a result."
)

DEFAULT_MAX_TOOL_ROUNDS = 3

CATEGORY_MCP_UNAVAILABLE = "mcp_unavailable"
CATEGORY_MCP_TIMEOUT = "mcp_timeout"
CATEGORY_INVALID_TOOL_ARGUMENTS = "invalid_tool_arguments"
CATEGORY_TOOL_ROUND_LIMIT = "tool_round_limit"
CATEGORY_INTERNAL = "internal"


class ChatEvent:
    """Base class of one server-sent event."""

    name = "event"

    def payload(self) -> dict:
        return {}

    def to_sse(self) -> str:
        """Render the event in the ``event:``/``data:`` wire format."""
        data = json.dumps(self.payload(), ensure_ascii=False)
        return f"event: {self.name}\ndata: {data}\n\n"


@dataclass
class StatusEvent(ChatEvent):
    """A progress marker of the current request stage."""

    request_id: str
    stage: str
    name = "status"

    def payload(self) -> dict:
        return {"request_id": self.request_id, "stage": self.stage}


@dataclass
class DeltaEvent(ChatEvent):
    """A piece of streamed assistant text."""

    text: str
    name = "delta"

    def payload(self) -> dict:
        return {"text": self.text}


@dataclass
class ToolCallEvent(ChatEvent):
    """The model asked for a tool call."""

    tool: str
    arguments: dict
    round: int
    name = "tool_call"

    def payload(self) -> dict:
        return {"tool": self.tool, "arguments": self.arguments, "round": self.round}


@dataclass
class ToolResultEvent(ChatEvent):
    """The result of the tool call, summarized for the UI."""

    tool: str
    ok: bool
    summary: str
    duration_ms: int
    name = "tool_result"

    def payload(self) -> dict:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "summary": self.summary,
            "duration_ms": self.duration_ms,
        }


@dataclass
class DoneEvent(ChatEvent):
    """The request finished successfully."""

    request_id: str
    finish_reason: str
    total_ms: int
    name = "done"

    def payload(self) -> dict:
        return {
            "request_id": self.request_id,
            "finish_reason": self.finish_reason,
            "total_ms": self.total_ms,
        }


@dataclass
class ErrorEvent(ChatEvent):
    """The request failed; no ``done`` event follows it."""

    request_id: str
    category: str
    message: str
    name = "error"

    def payload(self) -> dict:
        return {
            "request_id": self.request_id,
            "category": self.category,
            "message": self.message,
        }


def _millis(started: float) -> int:
    return max(int((time.monotonic() - started) * 1000), 0)


def _arguments_for_ui(arguments: dict) -> dict:
    """Keep only atomic values in the UI copy of tool arguments."""
    if not isinstance(arguments, dict):
        return {}
    return {
        str(key): value
        for key, value in arguments.items()
        if value is None or isinstance(value, (str, int, float, bool))
    }


def _summarize(result) -> str:
    """Build a short, safe one-line summary of a tool result."""
    if result.structured is not None:
        try:
            text = json.dumps(result.structured, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(result.structured)
    else:
        text = result.text or ""
    text = " ".join(text.split())
    return text[:300]


class Orchestrator:
    """Runs the tool-calling loop for one chat request."""

    def __init__(
        self,
        *,
        provider: ModelProvider,
        mcp_client: McpClient,
        trace=None,
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
    ):
        self._provider = provider
        self._mcp = mcp_client
        self._trace = trace if trace is not None else NullTraceWriter()
        self._max_tool_rounds = max(int(max_tool_rounds), 1)

    async def run(
        self, request_id: str, session: Session, user_message: str
    ) -> AsyncIterator[ChatEvent]:
        """Yield the events of one request for ``session``."""
        started = time.monotonic()
        self._trace.write(
            "request_start",
            request_id=request_id,
            session_id=session.session_id,
            message_chars=len(user_message or ""),
        )

        async with session.lock:
            yield StatusEvent(request_id, "accepted")

            # A missing model configuration is a controlled failure detected
            # before any model request and before the MCP server is polled.
            if not getattr(self._provider, "configured", True):
                self._trace.write(
                    "request_error",
                    request_id=request_id,
                    category=CATEGORY_MODEL_NOT_CONFIGURED,
                    message=MODEL_NOT_CONFIGURED_MESSAGE,
                )
                yield ErrorEvent(
                    request_id, CATEGORY_MODEL_NOT_CONFIGURED, MODEL_NOT_CONFIGURED_MESSAGE
                )
                return

            tools = []
            loaded: dict = {}
            async for event in self._load_tools(request_id, loaded):
                yield event
                if isinstance(event, ErrorEvent):
                    return
            tools = loaded.get("tools") or []
            openai_tools = tool_schema.to_openai_tools(tools)
            schema_by_name = {tool.name: tool.input_schema for tool in tools}

            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                *session.messages,
                {"role": "user", "content": str(user_message)},
            ]

            finish_reason = "stop"
            answered = False
            reported_model: str | None = None
            for round_index in range(1, self._max_tool_rounds + 1):
                phase = "tool_selection" if round_index == 1 else "final_answer"
                self._trace.write(
                    "model_request",
                    request_id=request_id,
                    phase=phase,
                    tools_offered=len(openai_tools),
                    provider=self._provider.name,
                    model=self._provider.model,
                )
                yield StatusEvent(request_id, "model_calling")

                text_parts: list = []
                calls: dict = {}
                try:
                    async for model_event in self._provider.stream(
                        messages, openai_tools
                    ):
                        if isinstance(model_event, TextDelta):
                            text_parts.append(model_event.text)
                            yield DeltaEvent(model_event.text)
                        elif isinstance(model_event, ToolCallDelta):
                            entry = calls.setdefault(
                                model_event.index,
                                {"id": None, "name": None, "arguments": ""},
                            )
                            if model_event.id:
                                entry["id"] = model_event.id
                            if model_event.name:
                                entry["name"] = (entry["name"] or "") + model_event.name
                            entry["arguments"] += model_event.arguments or ""
                        elif isinstance(model_event, Finished):
                            finish_reason = model_event.finish_reason or finish_reason
                            if model_event.reported_model:
                                reported_model = model_event.reported_model
                except ModelError as exc:
                    self._trace.write(
                        "request_error",
                        request_id=request_id,
                        category=exc.category,
                        message=exc.message,
                    )
                    yield ErrorEvent(request_id, exc.category, exc.message)
                    return

                if not calls:
                    session.append_user(user_message)
                    session.append_assistant("".join(text_parts))
                    answered = True
                    break

                ordered = [calls[index] for index in sorted(calls)]
                messages.append(
                    {
                        "role": "assistant",
                        "content": "".join(text_parts) or None,
                        "tool_calls": [
                            {
                                "id": call["id"] or f"call_{index}",
                                "type": "function",
                                "function": {
                                    "name": call["name"] or "",
                                    "arguments": call["arguments"] or "{}",
                                },
                            }
                            for index, call in zip(sorted(calls), ordered)
                        ],
                    }
                )

                async for event in self._run_tools(
                    request_id, round_index, ordered, schema_by_name, messages
                ):
                    if isinstance(event, ErrorEvent):
                        yield event
                        return
                    yield event

            if not answered:
                message = (
                    "The model kept requesting tools after "
                    f"{self._max_tool_rounds} rounds"
                )
                self._trace.write(
                    "request_error",
                    request_id=request_id,
                    category=CATEGORY_TOOL_ROUND_LIMIT,
                    message=message,
                )
                yield ErrorEvent(request_id, CATEGORY_TOOL_ROUND_LIMIT, message)
                return

            total_ms = _millis(started)
            done_fields: dict = {
                "request_id": request_id,
                "ok": True,
                "finish_reason": finish_reason,
                "total_ms": total_ms,
            }
            requested_model = str(getattr(self._provider, "model", "") or "")
            if reported_model and requested_model and reported_model != requested_model:
                # One event per request: the provider echoed a different model
                # id than the configured one, which is worth recording.
                self._trace.write(
                    "model_reported",
                    request_id=request_id,
                    requested_model=requested_model,
                    reported_model=reported_model,
                )
                done_fields["reported_model"] = reported_model
            self._trace.write("request_done", **done_fields)
            yield DoneEvent(request_id, finish_reason, total_ms)

    async def _load_tools(self, request_id: str, loaded: dict) -> AsyncIterator:
        """Yield progress/error events and store the MCP tools in ``loaded``.

        One :meth:`McpClient.probe` call does the handshake and the full
        ``tools/list`` in a single session, so a request opens exactly one MCP
        connection instead of two.
        """
        yield StatusEvent(request_id, "mcp_connecting")
        started = time.monotonic()
        status, tools = await self._mcp.probe()
        probe_ms = _millis(started)
        self._trace.write(
            "mcp_connect",
            request_id=request_id,
            ok=bool(status.connected),
            protocol_version=status.protocol_version,
            server_name=status.server_name,
            duration_ms=probe_ms,
        )
        if not status.connected:
            error = status.error
            category = (
                CATEGORY_MCP_TIMEOUT
                if error is not None and error.category == "timeout"
                else CATEGORY_MCP_UNAVAILABLE
            )
            message = (
                error.message
                if error is not None
                else "The MCP server is unavailable"
            )
            self._trace.write(
                "request_error",
                request_id=request_id,
                category=category,
                message=message,
            )
            yield ErrorEvent(request_id, category, message)
            return

        self._trace.write(
            "mcp_list_tools",
            request_id=request_id,
            ok=True,
            tools_count=len(tools),
            tool_names=[tool.name for tool in tools],
            duration_ms=probe_ms,
        )
        loaded["tools"] = tools

    async def _run_tools(
        self,
        request_id: str,
        round_index: int,
        calls: list,
        schema_by_name: dict,
        messages: list,
    ) -> AsyncIterator[ChatEvent]:
        """Run every tool call of one round, feeding results back to the model."""
        for call in calls:
            name = call["name"] or ""
            raw_arguments = call["arguments"] or "{}"
            call_id = call["id"] or f"call_{name or 'unknown'}"

            try:
                arguments = tool_schema.parse_arguments(raw_arguments)
                arguments = tool_schema.validate_arguments(
                    schema_by_name.get(name), arguments
                )
            except ValueError as exc:
                message = str(exc) or "Tool arguments are not valid"
                self._trace.write(
                    "tool_selected",
                    request_id=request_id,
                    tool=name,
                    arguments={},
                    round=round_index,
                    ok=False,
                )
                self._trace.write(
                    "tool_completed",
                    request_id=request_id,
                    tool=name,
                    ok=False,
                    result=message,
                    duration_ms=0,
                )
                yield ToolCallEvent(name, {}, round_index)
                yield ToolResultEvent(name, False, message, 0)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(
                            {"error": message, "category": CATEGORY_INVALID_TOOL_ARGUMENTS},
                            ensure_ascii=False,
                        ),
                    }
                )
                continue

            ui_arguments = _arguments_for_ui(arguments)
            self._trace.write(
                "tool_selected",
                request_id=request_id,
                tool=name,
                arguments=ui_arguments,
                round=round_index,
            )
            yield ToolCallEvent(name, ui_arguments, round_index)

            started = time.monotonic()
            try:
                result = await self._mcp.call_tool(name, arguments)
            except McpError as exc:
                category = (
                    CATEGORY_MCP_TIMEOUT
                    if exc.category == "timeout"
                    else CATEGORY_MCP_UNAVAILABLE
                )
                self._trace.write(
                    "tool_completed",
                    request_id=request_id,
                    tool=name,
                    ok=False,
                    result=exc.message,
                    duration_ms=_millis(started),
                )
                self._trace.write(
                    "request_error",
                    request_id=request_id,
                    category=category,
                    message=exc.message,
                )
                yield ToolResultEvent(name, False, exc.message, _millis(started))
                yield ErrorEvent(request_id, category, exc.message)
                return

            duration_ms = _millis(started)
            summary = _summarize(result)
            self._trace.write(
                "tool_completed",
                request_id=request_id,
                tool=name,
                ok=bool(result.ok),
                result=result.structured if result.structured is not None else summary,
                duration_ms=duration_ms,
            )
            yield ToolResultEvent(name, bool(result.ok), summary, duration_ms)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": tool_result_as_text(result),
                }
            )
