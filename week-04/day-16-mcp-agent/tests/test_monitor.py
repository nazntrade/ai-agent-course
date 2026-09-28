"""Unit tests of the day-20 host-side monitor (no network, no model)."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone

from agent.monitor import (
    MONITOR_ALLOWED_TOOLS,
    MONITOR_INJECTED_ARGUMENTS,
    MONITOR_TRIGGER,
    NotifierMonitor,
    monitor_result_incomplete,
)
from agent.notifier_client import NotifierUnavailable


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class _FakeChats:
    def __init__(self, chat_ids):
        self._chat_ids = list(chat_ids)

    def list_chats(self):
        return [{"id": chat_id} for chat_id in self._chat_ids]


class _FakeNotifier:
    def __init__(self, watches_by_chat=None, error=None):
        self.watches_by_chat = watches_by_chat or {}
        self.error = error
        self.calls = []

    async def list_watches(self, chat_id):
        self.calls.append(chat_id)
        if self.error is not None:
            raise self.error
        watches = self.watches_by_chat.get(chat_id, [])
        return {"count": len(watches), "watches": watches}


class _FakeOrchestrator:
    def __init__(self):
        self.runs = []

    async def run(self, request_id, session, user_message, **kwargs):
        self.runs.append(
            {
                "request_id": request_id,
                "session": session,
                "user_message": user_message,
                **kwargs,
            }
        )
        for _event in ():
            yield _event


def _watch(watch_id, *, status="active", next_check_at):
    return {
        "watch_id": watch_id,
        "status": status,
        "next_check_at": next_check_at,
        "interval_seconds": 60,
    }


class CompletenessRuleTest(unittest.TestCase):
    """The completeness rule matches SPEC §3.6."""

    def test_absent_evaluate_is_incomplete(self):
        self.assertEqual(monitor_result_incomplete({}), "evaluate_run_absent")

    def test_should_notify_false_is_complete(self):
        from agent.mcp_adapter import McpCallResult

        outcomes = {
            "evaluate_run": McpCallResult(
                ok=True, text="", structured={"should_notify": False}
            )
        }
        self.assertIsNone(monitor_result_incomplete(outcomes))

    def test_missing_send_is_incomplete(self):
        from agent.mcp_adapter import McpCallResult

        outcomes = {
            "evaluate_run": McpCallResult(
                ok=True, text="", structured={"should_notify": True}
            )
        }
        self.assertEqual(
            monitor_result_incomplete(outcomes), "send_notification_missing"
        )

    def test_failed_send_is_incomplete(self):
        from agent.mcp_adapter import McpCallResult

        outcomes = {
            "evaluate_run": McpCallResult(
                ok=True, text="", structured={"should_notify": True}
            ),
            "send_notification": McpCallResult(
                ok=True, text="", structured={"status": "failed"}
            ),
        }
        self.assertEqual(monitor_result_incomplete(outcomes), "send_notification_failed")

    def test_sent_send_is_complete(self):
        from agent.mcp_adapter import McpCallResult

        for status in ("sent", "duplicate"):
            outcomes = {
                "evaluate_run": McpCallResult(
                    ok=True, text="", structured={"should_notify": True}
                ),
                "send_notification": McpCallResult(
                    ok=True, text="", structured={"status": status}
                ),
            }
            self.assertIsNone(monitor_result_incomplete(outcomes))


class TickTest(unittest.IsolatedAsyncioTestCase):
    """One tick lists chats, filters due watches and aborts on a down B."""

    async def test_tick_runs_only_active_due_watches(self):
        notifier = _FakeNotifier(
            {
                "c1": [
                    _watch("due", next_check_at=_iso(900)),
                    _watch("future", next_check_at=_iso(2000)),
                    _watch("stopped", status="stopped", next_check_at=_iso(900)),
                ]
            }
        )
        orchestrator = _FakeOrchestrator()
        monitor = NotifierMonitor(
            chats=_FakeChats(["c1"]),
            notifier=notifier,
            orchestrator_factory=lambda: orchestrator,
            clock=lambda: 1000.0,
        )
        summary = await monitor.tick()
        self.assertEqual(summary["runs"], 1)
        self.assertEqual(summary["due"], 1)
        self.assertEqual(len(orchestrator.runs), 1)
        self.assertEqual(orchestrator.runs[0]["watch_id"], "due")

    async def test_tick_aborts_on_the_first_unavailable_b(self):
        notifier = _FakeNotifier(
            error=NotifierUnavailable("unreachable", "The notifier is not reachable")
        )
        orchestrator = _FakeOrchestrator()
        monitor = NotifierMonitor(
            chats=_FakeChats(["c1", "c2"]),
            notifier=notifier,
            orchestrator_factory=lambda: orchestrator,
            clock=lambda: 1000.0,
        )
        summary = await monitor.tick()
        self.assertTrue(summary["aborted"])
        self.assertEqual(summary["runs"], 0)
        self.assertEqual(notifier.calls, ["c1"])
        self.assertEqual(orchestrator.runs, [])

    async def test_monitor_turn_uses_the_restricted_contract(self):
        notifier = _FakeNotifier({"c1": [_watch("w1", next_check_at=_iso(900))]})
        orchestrator = _FakeOrchestrator()
        monitor = NotifierMonitor(
            chats=_FakeChats(["c1"]),
            notifier=notifier,
            orchestrator_factory=lambda: orchestrator,
            clock=lambda: 1000.0,
        )
        await monitor.tick()
        run = orchestrator.runs[0]
        self.assertEqual(run["trigger"], MONITOR_TRIGGER)
        self.assertEqual(run["watch_id"], "w1")
        self.assertEqual(set(run["allowed_tools"]), set(MONITOR_ALLOWED_TOOLS))
        self.assertTrue(run["system_prompt"])
        self.assertIs(run["require_result"], monitor_result_incomplete)
        # The monitor host owns the A-read scope: the orchestrator receives the
        # fixed override so the model cannot copy the watch id into ``task_id``.
        self.assertEqual(run["injected_arguments"], MONITOR_INJECTED_ARGUMENTS)
        # The monitor session carries the chat id for tool injection and has no
        # persistence callback, so a monitor turn never writes chat history.
        self.assertEqual(run["session"].chat_id, "c1")
        self.assertIsNone(getattr(run["session"], "_on_success", None))

    async def test_monitor_turn_tells_the_model_the_watch_id(self):
        # Regression (LIVE): the watch id is not part of any system prompt, so
        # the synthetic user message must carry it. Without it the real model
        # guessed a placeholder and ``evaluate_run`` answered ``unknown_watch``,
        # leaving the baseline unconsumed.
        notifier = _FakeNotifier(
            {"c1": [_watch("watch-42", next_check_at=_iso(900))]}
        )
        orchestrator = _FakeOrchestrator()
        monitor = NotifierMonitor(
            chats=_FakeChats(["c1"]),
            notifier=notifier,
            orchestrator_factory=lambda: orchestrator,
            clock=lambda: 1000.0,
        )
        await monitor.tick()
        run = orchestrator.runs[0]
        self.assertIn("watch-42", run["user_message"])


class LifecycleTest(unittest.IsolatedAsyncioTestCase):
    """``start``/``stop`` manage a single, idempotent background task."""

    async def test_start_and_stop_are_idempotent(self):
        notifier = _FakeNotifier({"c1": []})
        monitor = NotifierMonitor(
            chats=_FakeChats(["c1"]),
            notifier=notifier,
            orchestrator_factory=_FakeOrchestrator,
            tick_seconds=3600,
            clock=lambda: 1000.0,
        )
        self.assertFalse(monitor.running)
        await monitor.start()
        self.assertTrue(monitor.running)
        task = monitor._task
        await monitor.start()
        self.assertIs(monitor._task, task)
        await asyncio.sleep(0)
        await monitor.stop()
        self.assertFalse(monitor.running)
        await monitor.stop()
        self.assertFalse(monitor.running)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
