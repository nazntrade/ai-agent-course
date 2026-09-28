"""Unit tests of the tool-calling loop (fake provider + fake MCP client)."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from agent.mcp_adapter import McpCallResult, McpError, McpTool
from agent.mcp_hub import McpHub
from agent.orchestrator import (
    DEFAULT_MAX_TOOL_ROUNDS,
    SYSTEM_PROMPT,
    DeltaEvent,
    DoneEvent,
    ErrorEvent,
    Orchestrator,
    ToolCallEvent,
    ToolResultEvent,
)
from agent.provider import Finished, ModelError, TextDelta, ToolCallDelta
from agent.sessions import ChatSession
from agent.trace import NullTraceWriter, TraceWriter
from tests.support.fakes import BlockingProvider, FakeMcpClient, ScriptedProvider


def _tool_turn(index=0, name="calculate", arguments='{"operation": "multiply", "a": 23, "b": 17}'):
    return [
        ToolCallDelta(index=index, id="call_1", name=name, arguments=arguments),
        Finished(finish_reason="tool_calls"),
    ]


def _answer_turn(text="The result is 391."):
    return [TextDelta(text), Finished(finish_reason="stop")]


async def _collect(orchestrator, request_id, session, message, **kwargs):
    return [
        event
        async for event in orchestrator.run(request_id, session, message, **kwargs)
    ]


def _names(events):
    return [event.name for event in events]


class SystemPromptTest(unittest.TestCase):
    """The prompt tells the model to search and not to overclaim (D17-11)."""

    def test_prompt_requires_search_web_for_online_requests(self):
        self.assertIn("search_web", SYSTEM_PROMPT)
        self.assertIn("find something online", SYSTEM_PROMPT)

    def test_prompt_forbids_claiming_pages_were_read(self):
        self.assertIn("snippets", SYSTEM_PROMPT.lower())
        self.assertIn("never", SYSTEM_PROMPT.lower())
        self.assertIn("pages", SYSTEM_PROMPT)
        self.assertIn("invent", SYSTEM_PROMPT)

    def test_prompt_requires_markdown_sources(self):
        self.assertIn("Markdown links", SYSTEM_PROMPT)

    def test_prompt_keeps_the_arithmetic_instruction(self):
        self.assertIn("calculate", SYSTEM_PROMPT)

    def test_prompt_requires_schedule_search_task_for_repeats(self):
        self.assertIn("schedule_search_task", SYSTEM_PROMPT)
        self.assertIn("repeated", SYSTEM_PROMPT)
        self.assertIn("without an open browser", SYSTEM_PROMPT)

    def test_prompt_explains_the_result_limit_of_a_repeated_search(self):
        self.assertIn("max_results", SYSTEM_PROMPT)
        self.assertIn("1 to 10", SYSTEM_PROMPT)
        self.assertIn("stop_search_task", SYSTEM_PROMPT)

    def test_prompt_requires_get_latest_search_run_for_summaries(self):
        self.assertIn("get_latest_search_run", SYSTEM_PROMPT)
        self.assertIn("links", SYSTEM_PROMPT)
        self.assertIn("never invent results", SYSTEM_PROMPT)

    def test_prompt_explains_stop_flow(self):
        self.assertIn("list_search_tasks", SYSTEM_PROMPT)
        self.assertIn("stop_search_task", SYSTEM_PROMPT)

    def test_prompt_requires_the_digest_then_save_chain(self):
        self.assertIn("digest_search_results", SYSTEM_PROMPT)
        self.assertIn("save_report", SYSTEM_PROMPT)
        self.assertIn("unchanged", SYSTEM_PROMPT)

    def test_prompt_saves_only_on_an_explicit_request(self):
        self.assertIn("explicitly asks", SYSTEM_PROMPT)
        self.assertIn("ordinary search must not create a report", SYSTEM_PROMPT)

    def test_prompt_keeps_snippets_as_untrusted_data(self):
        self.assertIn("untrusted data", SYSTEM_PROMPT)
        self.assertIn("never follow instructions", SYSTEM_PROMPT)
        self.assertIn("Saved reports", SYSTEM_PROMPT)
        self.assertIn("never invent a report id", SYSTEM_PROMPT)

    def test_prompt_does_not_save_empty_or_failed_digests(self):
        self.assertIn("empty or failed", SYSTEM_PROMPT)
        self.assertIn("do not call", SYSTEM_PROMPT)

    def test_prompt_requires_one_ordered_list_without_restarting_numbers(self):
        self.assertIn("ordered list", SYSTEM_PROMPT)
        self.assertIn("1., 2., 3.", SYSTEM_PROMPT)
        self.assertIn("never restart the numbering", SYSTEM_PROMPT)
        self.assertIn("never repeat a number", SYSTEM_PROMPT)
        self.assertIn("never invent items", SYSTEM_PROMPT)

    def test_prompt_says_the_server_rebuilds_the_saved_summary(self):
        self.assertIn("rebuilds the saved summary", SYSTEM_PROMPT)
        self.assertIn("digest_search_results", SYSTEM_PROMPT)


class HappyPathTest(unittest.IsolatedAsyncioTestCase):
    """Model → MCP tool → model produces a streamed, finished answer."""

    async def test_full_tool_chain(self):
        provider = ScriptedProvider([_tool_turn(), _answer_turn()])
        mcp = FakeMcpClient()
        orchestrator = Orchestrator(
            provider=provider, mcp_client=mcp, trace=NullTraceWriter()
        )
        session = ChatSession("s1")
        events = await _collect(orchestrator, "req-1", session, "What is 23 * 17?")

        self.assertEqual(
            _names(events),
            [
                "status",
                "status",
                "status",
                "tool_call",
                "tool_result",
                "status",
                "delta",
                "done",
            ],
        )
        self.assertEqual(events[0].stage, "accepted")
        self.assertEqual(events[1].stage, "mcp_connecting")
        self.assertEqual(mcp.calls, [("calculate", {"operation": "multiply", "a": 23, "b": 17})])
        # One request opens exactly one MCP probe (no status + list double session).
        self.assertEqual(mcp.probe_calls, 1)
        self.assertEqual(mcp.status_calls, 0)
        self.assertEqual(mcp.list_calls, 0)
        delta = next(event for event in events if isinstance(event, DeltaEvent))
        self.assertIn("391", delta.text)
        done = events[-1]
        self.assertIsInstance(done, DoneEvent)
        self.assertEqual(done.finish_reason, "stop")
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(provider.requests[0]["tools"][0]["function"]["name"], "calculate")

    async def test_answer_without_tool_call(self):
        provider = ScriptedProvider([_answer_turn("Hello!")])
        orchestrator = Orchestrator(
            provider=provider, mcp_client=FakeMcpClient(), trace=NullTraceWriter()
        )
        events = await _collect(orchestrator, "req-2", ChatSession("s2"), "hi")
        self.assertEqual(_names(events), ["status", "status", "status", "delta", "done"])

    async def test_successful_turn_is_persisted_through_the_callback(self):
        provider = ScriptedProvider([_answer_turn("Hello!")])
        saved = []
        session = ChatSession(
            "s3", on_success=lambda user, assistant: saved.append((user, assistant))
        )
        orchestrator = Orchestrator(
            provider=provider, mcp_client=FakeMcpClient(), trace=NullTraceWriter()
        )
        await _collect(orchestrator, "req-3", session, "hi")
        self.assertEqual(saved, [("hi", "Hello!")])

    async def test_trace_contains_the_ordered_chain_and_the_chat_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "trace.jsonl"
            provider = ScriptedProvider([_tool_turn(), _answer_turn()])
            orchestrator = Orchestrator(
                provider=provider,
                mcp_client=FakeMcpClient(),
                trace=TraceWriter(trace_path),
            )
            session = ChatSession(
                "s4", history=[{"role": "user", "content": "earlier"}]
            )
            await _collect(orchestrator, "req-4", session, "What is 23 * 17?")
            records = [
                json.loads(line)
                for line in trace_path.read_text(encoding="utf-8").splitlines()
            ]
            events = [record["event"] for record in records]
            for expected in (
                "request_start",
                "mcp_connect",
                "mcp_list_tools",
                "model_request",
                "tool_selected",
                "tool_completed",
                "request_done",
            ):
                self.assertIn(expected, events)
            self.assertLess(events.index("tool_selected"), events.index("tool_completed"))
            self.assertNotIn("What is 23 * 17?", trace_path.read_text(encoding="utf-8"))
            self.assertEqual(records[0]["message_chars"], len("What is 23 * 17?"))
            self.assertEqual(records[0]["chat_id"], "s4")
            self.assertEqual(records[0]["context_messages"], 1)
            self.assertNotIn("session_id", records[0])

    async def test_context_window_is_limited_to_the_loaded_history(self):
        provider = ScriptedProvider([_answer_turn("ok")])
        history = [
            {"role": "user" if index % 2 == 0 else "assistant", "content": f"m{index}"}
            for index in range(20)
        ]
        session = ChatSession("s-window", history=history)
        orchestrator = Orchestrator(
            provider=provider, mcp_client=FakeMcpClient(), trace=NullTraceWriter()
        )
        await _collect(orchestrator, "req-window", session, "current")
        sent = provider.requests[0]["messages"]
        # system + 20 window messages + the current user message.
        self.assertEqual(len(sent), 22)
        self.assertEqual(sent[-1]["content"], "current")
        self.assertEqual(sum(1 for m in sent if m["role"] == "user"), 11)

    async def test_chat_id_is_hidden_from_the_model_schema(self):
        provider = ScriptedProvider([_answer_turn("ok")])
        orchestrator = Orchestrator(
            provider=provider, mcp_client=FakeMcpClient(), trace=NullTraceWriter()
        )
        await _collect(orchestrator, "req-hide", ChatSession("s-hide"), "hi")
        schedule = next(
            tool
            for tool in provider.requests[0]["tools"]
            if tool["function"]["name"] == "schedule_search_task"
        )
        self.assertNotIn("chat_id", schedule["function"]["parameters"]["properties"])

    async def test_chat_id_is_injected_and_overwrites_the_model_value(self):
        provider = ScriptedProvider(
            [
                _tool_turn(
                    name="schedule_search_task",
                    arguments=(
                        '{"query": "news", "interval_seconds": 86400, '
                        '"chat_id": "attacker"}'
                    ),
                ),
                _answer_turn("Scheduled."),
            ]
        )
        mcp = FakeMcpClient()
        session = ChatSession("trusted-chat")
        orchestrator = Orchestrator(
            provider=provider, mcp_client=mcp, trace=NullTraceWriter()
        )
        events = await _collect(orchestrator, "req-inject", session, "daily news")
        self.assertEqual(mcp.calls[0][0], "schedule_search_task")
        self.assertEqual(mcp.calls[0][1]["chat_id"], "trusted-chat")
        tool_call = next(event for event in events if event.name == "tool_call")
        self.assertNotIn("chat_id", tool_call.arguments)


class ToolCompositionTest(unittest.IsolatedAsyncioTestCase):
    """One user message can drive three dependent tool calls plus the answer."""

    SEARCH_RESULT = {
        "query": "Kotlin news",
        "count": 1,
        "results": [
            {
                "title": "Kotlin 1.9",
                "url": "https://kotlin.example.test/1",
                "description": "A release.",
            }
        ],
        "more_results_available": False,
        "note": "Snippets only; the pages were not opened.",
    }

    DIGEST = {
        "status": "ok",
        "topic": "Kotlin news",
        "count": 1,
        "summary": (
            "1. Kotlin 1.9 - A release.\n   Source: https://kotlin.example.test/1"
        ),
        "sources": [
            {
                "title": "Kotlin 1.9",
                "url": "https://kotlin.example.test/1",
                "description": "A release.",
            }
        ],
        "duplicates_removed": 0,
        "invalid_removed": 0,
        "truncated": False,
        "digest_id": "d19-abc",
        "note": "Snippets only.",
    }

    def test_default_round_limit_supports_a_three_step_chain(self):
        self.assertEqual(DEFAULT_MAX_TOOL_ROUNDS, 5)

    async def test_search_digest_save_chain_and_final_answer(self):
        mcp = FakeMcpClient(
            call_results={
                "search_web": McpCallResult(
                    ok=True, text="3 results", structured=self.SEARCH_RESULT
                ),
                "digest_search_results": McpCallResult(
                    ok=True, text="digest", structured=self.DIGEST
                ),
                "save_report": McpCallResult(
                    ok=True, text="saved", structured={"report_id": "r1"}
                ),
            }
        )
        provider = ScriptedProvider(
            [
                _tool_turn(name="search_web", arguments='{"query": "Kotlin news"}'),
                _tool_turn(
                    name="digest_search_results",
                    arguments=json.dumps({"search_result": self.SEARCH_RESULT}),
                ),
                _tool_turn(
                    name="save_report",
                    arguments=json.dumps({"digest": self.DIGEST}),
                ),
                _answer_turn("Saved. The report is in the Saved reports panel."),
            ]
        )
        orchestrator = Orchestrator(
            provider=provider, mcp_client=mcp, trace=NullTraceWriter()
        )
        events = await _collect(
            orchestrator,
            "req-compose",
            ChatSession("compose-chat"),
            "Find news about Kotlin, make a short summary with sources and save it",
        )
        self.assertEqual(
            [name for name, _ in mcp.calls],
            ["search_web", "digest_search_results", "save_report"],
        )
        # The whole object is forwarded unchanged between the steps.
        self.assertEqual(mcp.calls[1][1]["search_result"], self.SEARCH_RESULT)
        self.assertEqual(mcp.calls[2][1]["digest"], self.DIGEST)
        self.assertEqual(mcp.calls[2][1]["chat_id"], "compose-chat")
        results = [event for event in events if isinstance(event, ToolResultEvent)]
        self.assertEqual(len(results), 3)
        self.assertTrue(all(result.ok for result in results))
        self.assertEqual(len(provider.requests), 4)
        self.assertIsInstance(events[-1], DoneEvent)


class FailurePathTest(unittest.IsolatedAsyncioTestCase):
    """Failures are controlled and never produce a fake success."""

    async def test_mcp_unavailable_before_the_model(self):
        mcp = FakeMcpClient(connected=False)
        provider = ScriptedProvider([_answer_turn()])
        orchestrator = Orchestrator(
            provider=provider, mcp_client=mcp, trace=NullTraceWriter()
        )
        events = await _collect(orchestrator, "req-5", ChatSession("s5"), "hi")
        self.assertIsInstance(events[-1], ErrorEvent)
        self.assertEqual(events[-1].category, "mcp_unavailable")
        self.assertNotIn("done", _names(events))
        self.assertEqual(provider.requests, [])

    async def test_mcp_list_tools_failure(self):
        mcp = FakeMcpClient(list_error=McpError("protocol", "The MCP handshake failed"))
        orchestrator = Orchestrator(
            provider=ScriptedProvider([_answer_turn()]),
            mcp_client=mcp,
            trace=NullTraceWriter(),
        )
        events = await _collect(orchestrator, "req-6", ChatSession("s6"), "hi")
        self.assertEqual(events[-1].category, "mcp_unavailable")

    async def test_mcp_timeout_is_categorized(self):
        mcp = FakeMcpClient(status_error=McpError("timeout", "The MCP server did not answer"))
        mcp.connected = False
        orchestrator = Orchestrator(
            provider=ScriptedProvider([_answer_turn()]),
            mcp_client=mcp,
            trace=NullTraceWriter(),
        )
        events = await _collect(orchestrator, "req-7", ChatSession("s7"), "hi")
        self.assertEqual(events[-1].category, "mcp_timeout")

    async def test_mcp_unavailable_during_the_tool_call(self):
        mcp = FakeMcpClient(call_error=McpError("unreachable", "The MCP server is not reachable"))
        orchestrator = Orchestrator(
            provider=ScriptedProvider([_tool_turn(), _answer_turn()]),
            mcp_client=mcp,
            trace=NullTraceWriter(),
        )
        events = await _collect(orchestrator, "req-8", ChatSession("s8"), "What is 23 * 17?")
        self.assertIsInstance(events[-1], ErrorEvent)
        self.assertEqual(events[-1].category, "mcp_unavailable")
        self.assertNotIn("done", _names(events))

    async def test_model_unreachable(self):
        class FailingProvider(ScriptedProvider):
            async def stream(self, messages, tools, *, timeout_s=None):
                self.requests.append({"messages": list(messages), "tools": list(tools)})
                raise ModelError(
                    "The model endpoint is not reachable", "model_unreachable"
                )
                yield  # pragma: no cover - makes this an async generator

        saved = []
        session = ChatSession("s9", on_success=lambda u, a: saved.append((u, a)))
        orchestrator = Orchestrator(
            provider=FailingProvider([]),
            mcp_client=FakeMcpClient(),
            trace=NullTraceWriter(),
        )
        events = await _collect(orchestrator, "req-9", session, "hi")
        self.assertEqual(events[-1].category, "model_unreachable")
        self.assertEqual(saved, [])

    async def test_model_not_configured_is_detected_before_mcp(self):
        class UnconfiguredProvider(ScriptedProvider):
            configured = False

        mcp = FakeMcpClient()
        saved = []
        session = ChatSession("s9b", on_success=lambda u, a: saved.append((u, a)))
        orchestrator = Orchestrator(
            provider=UnconfiguredProvider([_answer_turn()]),
            mcp_client=mcp,
            trace=NullTraceWriter(),
        )
        events = await _collect(orchestrator, "req-9b", session, "hi")
        self.assertEqual(_names(events), ["status", "error"])
        self.assertEqual(events[-1].category, "model_not_configured")
        self.assertIn("not configured", events[-1].message)
        self.assertEqual(mcp.probe_calls, 0)
        self.assertEqual(saved, [])

    async def test_model_reported_event_only_on_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "trace.jsonl"
            provider = ScriptedProvider(
                [[TextDelta("hi"), Finished(finish_reason="stop", reported_model="other-model")]]
            )
            orchestrator = Orchestrator(
                provider=provider,
                mcp_client=FakeMcpClient(),
                trace=TraceWriter(trace_path),
            )
            await _collect(orchestrator, "req-9c", ChatSession("s9c"), "hi")
            records = [
                json.loads(line)
                for line in trace_path.read_text(encoding="utf-8").splitlines()
            ]
            reported = [r for r in records if r["event"] == "model_reported"]
            self.assertEqual(len(reported), 1)
            self.assertEqual(reported[0]["requested_model"], "scripted-model")
            self.assertEqual(reported[0]["reported_model"], "other-model")
            done = next(r for r in records if r["event"] == "request_done")
            self.assertEqual(done["reported_model"], "other-model")

    async def test_model_reported_event_absent_without_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "trace.jsonl"
            provider = ScriptedProvider(
                [[TextDelta("hi"), Finished(finish_reason="stop", reported_model="scripted-model")]]
            )
            orchestrator = Orchestrator(
                provider=provider,
                mcp_client=FakeMcpClient(),
                trace=TraceWriter(trace_path),
            )
            await _collect(orchestrator, "req-9d", ChatSession("s9d"), "hi")
            records = [
                json.loads(line)
                for line in trace_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertNotIn("model_reported", [r["event"] for r in records])
            done = next(r for r in records if r["event"] == "request_done")
            self.assertNotIn("reported_model", done)

    async def test_invalid_tool_arguments_are_reported_and_recoverable(self):
        provider = ScriptedProvider(
            [_tool_turn(arguments="{not json"), _answer_turn("I could not compute it.")]
        )
        mcp = FakeMcpClient()
        orchestrator = Orchestrator(
            provider=provider, mcp_client=mcp, trace=NullTraceWriter()
        )
        events = await _collect(orchestrator, "req-10", ChatSession("s10"), "What is 23 * 17?")
        results = [event for event in events if isinstance(event, ToolResultEvent)]
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertEqual(mcp.calls, [])
        self.assertIsInstance(events[-1], DoneEvent)

    async def test_tool_level_error_is_fed_back_to_the_model(self):
        mcp = FakeMcpClient(
            call_result=McpCallResult(ok=False, text="Division by zero is not allowed")
        )
        provider = ScriptedProvider(
            [
                _tool_turn(arguments='{"operation": "divide", "a": 1, "b": 0}'),
                _answer_turn("Division by zero is not allowed."),
            ]
        )
        orchestrator = Orchestrator(
            provider=provider, mcp_client=mcp, trace=NullTraceWriter()
        )
        events = await _collect(orchestrator, "req-11", ChatSession("s11"), "1 divided by 0?")
        results = [event for event in events if isinstance(event, ToolResultEvent)]
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertNotIn("error", _names(events))
        self.assertIsInstance(events[-1], DoneEvent)

    async def test_round_limit_is_enforced(self):
        mcp = FakeMcpClient()
        provider = ScriptedProvider([_tool_turn()] * 5)
        orchestrator = Orchestrator(
            provider=provider, mcp_client=mcp, trace=NullTraceWriter(), max_tool_rounds=3
        )
        events = await _collect(orchestrator, "req-12", ChatSession("s12"), "loop")
        self.assertIsInstance(events[-1], ErrorEvent)
        self.assertEqual(events[-1].category, "tool_round_limit")
        self.assertEqual(len(provider.requests), 3)

    async def test_default_round_limit_is_five(self):
        mcp = FakeMcpClient()
        provider = ScriptedProvider([_tool_turn()] * 8)
        orchestrator = Orchestrator(
            provider=provider, mcp_client=mcp, trace=NullTraceWriter()
        )
        events = await _collect(orchestrator, "req-12b", ChatSession("s12b"), "loop")
        self.assertEqual(events[-1].category, "tool_round_limit")
        self.assertNotIn("done", _names(events))
        self.assertEqual(len(provider.requests), DEFAULT_MAX_TOOL_ROUNDS)


class ConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    """One session is serialized; a cancelled client leaves nothing behind."""

    async def test_same_session_requests_are_serialized(self):
        gate = asyncio.Event()
        provider = BlockingProvider(gate)
        orchestrator = Orchestrator(
            provider=provider, mcp_client=FakeMcpClient(), trace=NullTraceWriter()
        )
        session = ChatSession("shared")

        first = []
        second = []

        async def consume(target, request_id):
            async for event in orchestrator.run(request_id, session, "hi"):
                target.append(event)

        task_one = asyncio.create_task(consume(first, "a"))
        await asyncio.sleep(0.05)
        task_two = asyncio.create_task(consume(second, "b"))
        await asyncio.sleep(0.05)

        self.assertTrue(session.lock.locked())
        self.assertEqual(second, [])
        self.assertEqual(provider.calls, 1)

        gate.set()
        await asyncio.gather(task_one, task_two)
        self.assertEqual(provider.calls, 2)
        self.assertFalse(session.lock.locked())

    async def test_cancelled_client_releases_the_session_lock(self):
        gate = asyncio.Event()
        provider = BlockingProvider(gate)
        orchestrator = Orchestrator(
            provider=provider, mcp_client=FakeMcpClient(), trace=NullTraceWriter()
        )
        session = ChatSession("cancelled")

        async def consume():
            async for _event in orchestrator.run("req-cancel", session, "hi"):
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)
        self.assertFalse(session.lock.locked())


class MultiServerAndMonitorTest(unittest.IsolatedAsyncioTestCase):
    """Day 20: server labels in the trace/SSE and the monitor run contract."""

    def _b_tools(self):
        return [
            McpTool(
                name="evaluate_run",
                title=None,
                description="Compare a run with a watch.",
                input_schema={"type": "object", "properties": {}},
            ),
            McpTool(
                name="send_notification",
                title=None,
                description="Send a notification.",
                input_schema={"type": "object", "properties": {}},
            ),
        ]

    def _hub(self, *, a=None, b=None):
        a_client = a or FakeMcpClient()
        b_client = b or FakeMcpClient(tools=self._b_tools())
        hub = McpHub(
            [("A", a_client, "127.0.0.1:8765"), ("B", b_client, "127.0.0.1:8766")],
            primary="A",
        )
        return hub, a_client, b_client

    def _records(self, trace_path):
        return [
            json.loads(line)
            for line in trace_path.read_text(encoding="utf-8").splitlines()
        ]

    async def test_hub_records_per_server_connect_and_aggregated_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "trace.jsonl"
            b = FakeMcpClient(
                tools=self._b_tools(),
                call_results={
                    "evaluate_run": McpCallResult(
                        ok=True, text="", structured={"should_notify": False}
                    )
                },
            )
            hub, _a, _b = self._hub(b=b)
            provider = ScriptedProvider(
                [
                    _tool_turn(name="evaluate_run", arguments="{}"),
                    _answer_turn("Done."),
                ]
            )
            orchestrator = Orchestrator(
                provider=provider, mcp_client=hub, trace=TraceWriter(trace_path)
            )
            events = await _collect(
                orchestrator, "req-hub", ChatSession("chat-hub"), "watch it"
            )

            connects = [r for r in self._records(trace_path) if r["event"] == "mcp_connect"]
            self.assertEqual(sorted(r["server"] for r in connects), ["A", "B"])
            listed = [
                r for r in self._records(trace_path) if r["event"] == "mcp_list_tools"
            ]
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["per_server"], {"A": 9, "B": 2})
            self.assertEqual(listed[0]["tools_count"], 11)

            selected = [
                r for r in self._records(trace_path) if r["event"] == "tool_selected"
            ]
            self.assertEqual(selected[0]["server"], "B")
            call = next(event for event in events if isinstance(event, ToolCallEvent))
            self.assertEqual(call.server, "B")
            result = next(event for event in events if isinstance(event, ToolResultEvent))
            self.assertEqual(result.server, "B")
            self.assertEqual(call.payload()["server"], "B")

    async def test_legacy_client_has_no_server_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "trace.jsonl"
            mcp = FakeMcpClient()
            provider = ScriptedProvider([_tool_turn(), _answer_turn()])
            orchestrator = Orchestrator(
                provider=provider, mcp_client=mcp, trace=TraceWriter(trace_path)
            )
            events = await _collect(orchestrator, "req-legacy", ChatSession("s-legacy"), "23*17")
            call = next(event for event in events if isinstance(event, ToolCallEvent))
            result = next(event for event in events if isinstance(event, ToolResultEvent))
            self.assertNotIn("server", call.payload())
            self.assertNotIn("server", result.payload())
            selected = [
                r for r in self._records(trace_path) if r["event"] == "tool_selected"
            ]
            self.assertNotIn("server", selected[0])
            connects = [
                r for r in self._records(trace_path) if r["event"] == "mcp_connect"
            ]
            self.assertNotIn("server", connects[0])

    async def test_monitor_trigger_watch_id_and_system_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "trace.jsonl"
            provider = ScriptedProvider([_answer_turn("checked")])
            orchestrator = Orchestrator(
                provider=provider,
                mcp_client=FakeMcpClient(),
                trace=TraceWriter(trace_path),
            )
            events = await _collect(
                orchestrator,
                "req-mon",
                ChatSession("m-chat"),
                "tick",
                trigger="monitor",
                watch_id="w1",
                system_prompt="MONITOR PROMPT",
            )
            start = self._records(trace_path)[0]
            self.assertEqual(start["event"], "request_start")
            self.assertEqual(start["trigger"], "monitor")
            self.assertEqual(start["watch_id"], "w1")
            self.assertEqual(
                provider.requests[0]["messages"][0]["content"], "MONITOR PROMPT"
            )
            self.assertEqual(events[-1].payload()["request_id"], "req-mon")

    async def test_require_result_writes_monitor_incomplete_instead_of_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "trace.jsonl"
            provider = ScriptedProvider([_answer_turn("nothing")])
            orchestrator = Orchestrator(
                provider=provider,
                mcp_client=FakeMcpClient(),
                trace=TraceWriter(trace_path),
            )
            events = await _collect(
                orchestrator,
                "req-inc",
                ChatSession("m-chat"),
                "tick",
                trigger="monitor",
                watch_id="w7",
                require_result=lambda outcomes: "evaluate_run_absent",
            )
            records = self._records(trace_path)
            events_names = [r["event"] for r in records]
            self.assertIn("monitor_incomplete", events_names)
            self.assertNotIn("request_done", events_names)
            incomplete = next(
                r for r in records if r["event"] == "monitor_incomplete"
            )
            self.assertEqual(incomplete["watch_id"], "w7")
            self.assertEqual(incomplete["reason"], "evaluate_run_absent")
            self.assertFalse(any(isinstance(event, DoneEvent) for event in events))

    def _a_client(self):
        return FakeMcpClient(
            call_results={
                "get_latest_search_run": McpCallResult(
                    ok=True, text="", structured={"status": "ok", "results": []}
                )
            }
        )

    async def test_monitor_injection_overrides_the_model_task_id(self):
        # Regression (LIVE): the model copied the watch id into ``task_id`` and
        # the real server A rejected the read. The host-owned override must reach
        # the actual A call, not the model's value.
        a = self._a_client()
        hub, a_client, _b = self._hub(a=a)
        provider = ScriptedProvider(
            [
                _tool_turn(
                    name="get_latest_search_run", arguments='{"task_id": "watch-42"}'
                ),
                _answer_turn("checked"),
            ]
        )
        orchestrator = Orchestrator(
            provider=provider, mcp_client=hub, trace=NullTraceWriter()
        )
        await _collect(
            orchestrator,
            "req-inject-mon",
            ChatSession("m-chat"),
            "tick",
            trigger="monitor",
            watch_id="watch-42",
            allowed_tools=("get_latest_search_run",),
            injected_arguments={"get_latest_search_run": {"task_id": ""}},
        )
        self.assertEqual(
            a_client.calls,
            [("get_latest_search_run", {"task_id": "", "chat_id": "m-chat"})],
        )

    async def test_monitor_injection_overrides_a_wrong_type_task_id(self):
        a = self._a_client()
        hub, a_client, _b = self._hub(a=a)
        provider = ScriptedProvider(
            [
                _tool_turn(
                    name="get_latest_search_run", arguments='{"task_id": 123}'
                ),
                _answer_turn("checked"),
            ]
        )
        orchestrator = Orchestrator(
            provider=provider, mcp_client=hub, trace=NullTraceWriter()
        )
        await _collect(
            orchestrator,
            "req-inject-type",
            ChatSession("m-chat"),
            "tick",
            trigger="monitor",
            watch_id="watch-42",
            allowed_tools=("get_latest_search_run",),
            injected_arguments={"get_latest_search_run": {"task_id": ""}},
        )
        # The override lands before validation, so the wrong model type never
        # reaches a type error and the call carries the host scope.
        self.assertEqual(
            a_client.calls,
            [("get_latest_search_run", {"task_id": "", "chat_id": "m-chat"})],
        )

    async def test_chat_turn_without_injection_keeps_the_model_task_id(self):
        a = self._a_client()
        hub, a_client, _b = self._hub(a=a)
        provider = ScriptedProvider(
            [
                _tool_turn(
                    name="get_latest_search_run", arguments='{"task_id": "t1"}'
                ),
                _answer_turn("ok"),
            ]
        )
        orchestrator = Orchestrator(
            provider=provider, mcp_client=hub, trace=NullTraceWriter()
        )
        await _collect(
            orchestrator,
            "req-chat-keep",
            ChatSession("chat-keep"),
            "latest run",
        )
        self.assertEqual(
            a_client.calls,
            [("get_latest_search_run", {"task_id": "t1", "chat_id": "chat-keep"})],
        )

    async def test_injected_argument_outside_the_schema_is_dropped(self):
        a = self._a_client()
        hub, a_client, _b = self._hub(a=a)
        provider = ScriptedProvider(
            [
                _tool_turn(name="get_latest_search_run", arguments="{}"),
                _answer_turn("checked"),
            ]
        )
        orchestrator = Orchestrator(
            provider=provider, mcp_client=hub, trace=NullTraceWriter()
        )
        await _collect(
            orchestrator,
            "req-inject-unknown",
            ChatSession("chat-d"),
            "tick",
            trigger="monitor",
            watch_id="w-d",
            allowed_tools=("get_latest_search_run",),
            injected_arguments={"get_latest_search_run": {"not_in_schema": "x"}},
        )
        # The override is applied before validation, so an out-of-schema key is
        # still discarded and cannot smuggle an argument into the MCP call.
        self.assertEqual(
            a_client.calls, [("get_latest_search_run", {"chat_id": "chat-d"})]
        )

    async def test_complete_monitor_turn_records_done(self):
        from agent.monitor import monitor_result_incomplete

        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "trace.jsonl"
            a = FakeMcpClient(
                call_results={
                    "get_latest_search_run": McpCallResult(
                        ok=True, text="", structured={"status": "empty", "results": []}
                    )
                }
            )
            b = FakeMcpClient(
                tools=self._b_tools(),
                call_results={
                    "evaluate_run": McpCallResult(
                        ok=True, text="", structured={"should_notify": False}
                    )
                },
            )
            hub, _a, _b = self._hub(a=a, b=b)
            provider = ScriptedProvider(
                [
                    _tool_turn(name="get_latest_search_run", arguments="{}"),
                    _tool_turn(name="evaluate_run", arguments="{}"),
                    _answer_turn("complete"),
                ]
            )
            orchestrator = Orchestrator(
                provider=provider, mcp_client=hub, trace=TraceWriter(trace_path)
            )
            events = await _collect(
                orchestrator,
                "req-complete",
                ChatSession("m-chat"),
                "tick",
                trigger="monitor",
                watch_id="w2",
                require_result=monitor_result_incomplete,
            )
            events_names = [r["event"] for r in self._records(trace_path)]
            self.assertIn("request_done", events_names)
            self.assertNotIn("monitor_incomplete", events_names)
            self.assertIsInstance(events[-1], DoneEvent)

    async def test_allowed_tools_limits_offered_tools_and_blocks_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "trace.jsonl"
            mcp = FakeMcpClient()
            provider = ScriptedProvider(
                [
                    _tool_turn(name="search_web", arguments='{"query": "x"}'),
                    _answer_turn("I did not search."),
                ]
            )
            orchestrator = Orchestrator(
                provider=provider, mcp_client=mcp, trace=TraceWriter(trace_path)
            )
            events = await _collect(
                orchestrator,
                "req-allowed",
                ChatSession("s-allowed"),
                "only calculate",
                allowed_tools={"calculate"},
            )
            offered = [
                tool["function"]["name"] for tool in provider.requests[0]["tools"]
            ]
            self.assertEqual(offered, ["calculate"])
            self.assertEqual(mcp.calls, [])
            results = [event for event in events if isinstance(event, ToolResultEvent)]
            self.assertEqual(len(results), 1)
            self.assertFalse(results[0].ok)
            selected = [
                r for r in self._records(trace_path) if r["event"] == "tool_selected"
            ]
            self.assertFalse(selected[0].get("ok", True))
            self.assertIsInstance(events[-1], DoneEvent)

    async def test_on_tool_result_callback_receives_every_result(self):
        captured = []
        provider = ScriptedProvider([_tool_turn(), _answer_turn()])
        orchestrator = Orchestrator(
            provider=provider, mcp_client=FakeMcpClient(), trace=NullTraceWriter()
        )
        await _collect(
            orchestrator,
            "req-cb",
            ChatSession("s-cb"),
            "23*17",
            on_tool_result=lambda name, result: captured.append((name, result)),
        )
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], "calculate")
        self.assertTrue(captured[0][1].ok)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
