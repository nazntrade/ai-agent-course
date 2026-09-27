"""Integration tests of the notifier MCP server (B) over real processes.

Skipped unless ``RUN_LIVE_MCP=1``. ``harness/live_mcp.py`` starts two real MCP
processes — server A (``mcp_server``, search and scheduled runs) and server B
(``notifier_server``, watches and Telegram delivery) — plus a loopback
``FakeSearchServer`` for A and a loopback ``FakeTelegramServer`` for B. This
module drives both servers over the real Streamable HTTP transport and inspects
the loopback Telegram fake over its loopback diagnostics endpoint.

Nothing is mocked at the MCP or HTTP level and the real Telegram service is never
called; the fake search and the fake Telegram server only answer on ``127.0.0.1``.
The two process boundaries under test are:

* server A produces real scheduled-search runs through the real scheduler;
* server B applies the criterion, records seen items and deliveries and calls the
  loopback Telegram fake through its single network boundary.
"""

from __future__ import annotations

import asyncio
import os
import time
import unittest

import httpx

from agent.mcp_adapter import SdkMcpClient, inspect_tools
from tests.support.fake_search import EMPTY_MARKER, MANY_MARKER, RESULT_URLS
from tests.support.fake_telegram import fetch_stats

RUN_LIVE = os.environ.get("RUN_LIVE_MCP") == "1"
MCP_URL = os.environ.get("MCP_TEST_URL", "")
NOTIFIER_URL = os.environ.get("NOTIFIER_TEST_URL", "")
BACKEND_URL = os.environ.get("BACKEND_TEST_URL", "")
FAKE_TELEGRAM_URL = os.environ.get("FAKE_TELEGRAM_URL", "")

NOTIFIER_SERVER_NAME = "day-20-notifier"

NOTIFIER_TOOLS = (
    "create_notification_watch",
    "evaluate_run",
    "get_delivery_status",
    "list_notification_watches",
    "send_notification",
    "stop_notification_watch",
)

RUN_TIMEOUT_SECONDS = 30.0
POLL_SECONDS = 0.5
SEND_TIMEOUT_SECONDS = 30.0

WATCH_KEYWORDS = ["python"]
WATCH_QUERY = "python documentation"


@unittest.skipUnless(
    RUN_LIVE and MCP_URL and NOTIFIER_URL and BACKEND_URL, "set RUN_LIVE_MCP=1"
)
class NotifierLiveTest(unittest.IsolatedAsyncioTestCase):
    """The real notifier server over real HTTP, driven with real server A runs."""

    def setUp(self):
        # A clean chat list keeps the 5-chat limit from making this module flaky
        # and removes any scheduled task left by an earlier integration module.
        try:
            chats = httpx.get(f"{BACKEND_URL}/api/chats", timeout=20.0).json()
        except Exception:  # noqa: BLE001 - prerequisites are reported by the tests
            return
        for chat in chats.get("chats", []):
            httpx.delete(
                f"{BACKEND_URL}/api/chats/{chat['id']}?force=true", timeout=20.0
            )

    # -- clients and helpers ------------------------------------------------

    def _a_client(self) -> SdkMcpClient:
        return SdkMcpClient(MCP_URL, call_timeout_s=30.0)

    def _b_client(self) -> SdkMcpClient:
        return SdkMcpClient(NOTIFIER_URL, call_timeout_s=SEND_TIMEOUT_SECONDS)

    async def _call_a(self, tool: str, arguments: dict):
        return await self._a_client().call_tool(tool, arguments)

    async def _call_b(self, tool: str, arguments: dict):
        return await self._b_client().call_tool(tool, arguments)

    def _create_chat(self, title: str = "Notifier chat") -> str:
        for _ in range(6):
            response = httpx.post(
                f"{BACKEND_URL}/api/chats", json={"title": title}, timeout=20.0
            )
            if response.status_code == 201:
                return response.json()["id"]
            if response.status_code != 409:
                self.fail(response.text)
            chats = httpx.get(
                f"{BACKEND_URL}/api/chats", timeout=20.0
            ).json().get("chats", [])
            if not chats:
                self.fail("the chat limit is reached but there is no chat to delete")
            httpx.delete(
                f"{BACKEND_URL}/api/chats/{chats[-1]['id']}?force=true", timeout=20.0
            )
        self.fail("could not create a chat")

    async def _schedule(self, chat_id: str, query: str) -> str:
        created = await self._call_a(
            "schedule_search_task",
            {"query": query, "interval_seconds": 3600, "chat_id": chat_id},
        )
        self.assertTrue(created.ok, msg=created.text)
        task_id = created.structured["task_id"]
        self.assertTrue(task_id)
        return task_id

    async def _wait_for_status(
        self, chat_id: str, *, task_id: str = "", statuses=("ok", "empty", "error")
    ) -> dict:
        deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
        payload: dict = {"status": "pending"}
        while time.monotonic() < deadline:
            result = await self._call_a(
                "get_latest_search_run",
                {"chat_id": chat_id, "task_id": task_id},
            )
            if result.ok:
                payload = result.structured or {"status": "pending"}
                if payload.get("status") in statuses:
                    return payload
            await asyncio.sleep(POLL_SECONDS)
        return payload

    async def _latest_run(self, chat_id: str) -> dict:
        result = await self._call_a(
            "get_latest_search_run", {"chat_id": chat_id}
        )
        self.assertTrue(result.ok, msg=result.text)
        return result.structured or {}

    async def _create_watch(self, chat_id: str, **overrides) -> dict:
        arguments = {
            "query": WATCH_QUERY,
            "keywords": list(WATCH_KEYWORDS),
            "interval_seconds": 60,
            "summary_interval_seconds": 3600,
            "chat_id": chat_id,
        }
        arguments.update(overrides)
        created = await self._call_b("create_notification_watch", arguments)
        self.assertTrue(created.ok, msg=created.text)
        return created.structured

    async def _consume_baseline(self, chat_id: str, watch_id: str) -> dict:
        """Drive a real empty A run and consume the baseline through server B."""
        task_id = await self._schedule(chat_id, EMPTY_MARKER)
        empty_run = await self._wait_for_status(
            chat_id, task_id=task_id, statuses=("empty",)
        )
        self.assertEqual(empty_run["status"], "empty", empty_run)
        evaluated = await self._call_b(
            "evaluate_run",
            {"watch_id": watch_id, "run": empty_run, "chat_id": chat_id},
        )
        self.assertTrue(evaluated.ok, msg=evaluated.text)
        return evaluated.structured

    def _telegram_messages(self) -> list:
        return fetch_stats(FAKE_TELEGRAM_URL)["messages"]

    def _telegram_count(self) -> int:
        return int(fetch_stats(FAKE_TELEGRAM_URL)["count"])

    # -- tests --------------------------------------------------------------

    async def test_notifier_tools_list_has_six_tools(self):
        status, tools = await inspect_tools(NOTIFIER_URL, connect_timeout_s=10.0)
        self.assertTrue(status.connected, msg=str(status.error))
        self.assertEqual(status.server_name, NOTIFIER_SERVER_NAME)
        self.assertEqual(status.tools_count, 6)
        self.assertEqual(sorted(tool.name for tool in tools), sorted(NOTIFIER_TOOLS))

    async def test_create_watch_evaluate_and_send_flow(self):
        chat_id = self._create_chat()
        watch = await self._create_watch(chat_id)
        self.assertTrue(watch["watch_id"])
        self.assertEqual(watch["status"], "active")
        self.assertEqual(watch["keywords"], WATCH_KEYWORDS)
        self.assertEqual(watch["interval_seconds"], 60)

        baseline = await self._consume_baseline(chat_id, watch["watch_id"])
        self.assertTrue(baseline["is_baseline"])
        self.assertFalse(baseline["should_notify"])

        before = self._telegram_count()
        task_id = await self._schedule(chat_id, WATCH_QUERY)
        run = await self._wait_for_status(chat_id, task_id=task_id, statuses=("ok",))
        self.assertEqual(run["status"], "ok", run)

        evaluated = await self._call_b(
            "evaluate_run",
            {"watch_id": watch["watch_id"], "run": run, "chat_id": chat_id},
        )
        self.assertTrue(evaluated.ok, msg=evaluated.text)
        evaluated_payload = evaluated.structured
        self.assertTrue(evaluated_payload["should_notify"], evaluated_payload)
        new_items = evaluated_payload["new_items"]
        self.assertTrue(new_items)

        sent = await self._call_b(
            "send_notification",
            {
                "watch_id": watch["watch_id"],
                "chat_id": chat_id,
                "kind": "new_items",
                "items": new_items,
            },
        )
        self.assertTrue(sent.ok, msg=sent.text)
        self.assertEqual(sent.structured["status"], "sent", sent.structured)
        self.assertTrue(sent.structured["delivery_id"])

        after = self._telegram_count()
        self.assertEqual(after - before, 1)
        delivered_texts = self._telegram_messages()[before:after]
        self.assertTrue(
            any(
                any(url in text for url in RESULT_URLS)
                for text in delivered_texts
                if text
            ),
            msg=delivered_texts,
        )

    async def test_no_backfill_uses_only_the_latest_run(self):
        chat_id = self._create_chat()
        watch = await self._create_watch(chat_id)
        await self._consume_baseline(chat_id, watch["watch_id"])

        # An older run accumulates while the watch has not seen it; the latest
        # run must be the only one processed, so the older items are never sent.
        old_task = await self._schedule(chat_id, f"{MANY_MARKER} accumulated")
        old_run = await self._wait_for_status(
            chat_id, task_id=old_task, statuses=("ok",)
        )
        old_urls = {item["url"] for item in old_run.get("results", [])}
        self.assertTrue(old_urls)
        self.assertTrue(all("reference/" in url for url in old_urls))

        new_task = await self._schedule(chat_id, WATCH_QUERY)
        new_run = await self._wait_for_status(
            chat_id, task_id=new_task, statuses=("ok",)
        )

        latest = await self._latest_run(chat_id)
        self.assertEqual(latest.get("task_id"), new_task, latest)
        latest_urls = {item["url"] for item in latest.get("results", [])}
        self.assertEqual(latest_urls, set(RESULT_URLS))

        evaluated = await self._call_b(
            "evaluate_run",
            {"watch_id": watch["watch_id"], "run": latest, "chat_id": chat_id},
        )
        self.assertTrue(evaluated.ok, msg=evaluated.text)
        payload = evaluated.structured
        self.assertTrue(payload["should_notify"], payload)
        new_urls = {item["url"] for item in payload["new_items"]}
        self.assertEqual(new_urls, set(RESULT_URLS))
        # No backfill: nothing from the older, unprocessed run is reported.
        self.assertFalse(new_urls & old_urls, msg=new_urls & old_urls)

        before = self._telegram_count()
        sent = await self._call_b(
            "send_notification",
            {
                "watch_id": watch["watch_id"],
                "chat_id": chat_id,
                "kind": "new_items",
                "items": payload["new_items"],
            },
        )
        self.assertTrue(sent.ok, msg=sent.text)
        self.assertEqual(sent.structured["status"], "sent", sent.structured)
        after = self._telegram_count()
        self.assertEqual(after - before, 1)
        delivered = self._telegram_messages()[before:after]
        self.assertTrue(any(url in text for url in RESULT_URLS for text in delivered if text))
        self.assertFalse(any("reference/" in text for text in delivered if text))

    async def test_two_materials_in_one_period_are_two_deliveries(self):
        chat_id = self._create_chat()
        watch = await self._create_watch(chat_id)
        await self._consume_baseline(chat_id, watch["watch_id"])

        # First material: the default Python documentation results.
        first_task = await self._schedule(chat_id, WATCH_QUERY)
        first_run = await self._wait_for_status(
            chat_id, task_id=first_task, statuses=("ok",)
        )
        first_eval = await self._call_b(
            "evaluate_run",
            {"watch_id": watch["watch_id"], "run": first_run, "chat_id": chat_id},
        )
        self.assertTrue(first_eval.ok, msg=first_eval.text)
        first_items = first_eval.structured["new_items"]
        self.assertTrue(first_items)

        before = self._telegram_count()
        first_sent = await self._call_b(
            "send_notification",
            {
                "watch_id": watch["watch_id"],
                "chat_id": chat_id,
                "kind": "new_items",
                "items": first_items,
            },
        )
        self.assertTrue(first_sent.ok, msg=first_sent.text)
        self.assertEqual(first_sent.structured["status"], "sent", first_sent.structured)

        # Second, different material in the same time window: a content period
        # key must not suppress it.
        second_task = await self._schedule(chat_id, f"{MANY_MARKER} second")
        second_run = await self._wait_for_status(
            chat_id, task_id=second_task, statuses=("ok",)
        )
        second_eval = await self._call_b(
            "evaluate_run",
            {"watch_id": watch["watch_id"], "run": second_run, "chat_id": chat_id},
        )
        self.assertTrue(second_eval.ok, msg=second_eval.text)
        second_items = second_eval.structured["new_items"]
        self.assertTrue(second_items)
        second_urls = {item["url"] for item in second_items}
        self.assertTrue(all("reference/" in url for url in second_urls))

        second_sent = await self._call_b(
            "send_notification",
            {
                "watch_id": watch["watch_id"],
                "chat_id": chat_id,
                "kind": "new_items",
                "items": second_items,
            },
        )
        self.assertTrue(second_sent.ok, msg=second_sent.text)
        self.assertEqual(
            second_sent.structured["status"], "sent", second_sent.structured
        )
        self.assertNotEqual(
            first_sent.structured["period_key"],
            second_sent.structured["period_key"],
        )

        # Two sent deliveries with two distinct content period keys.
        status = await self._call_b(
            "get_delivery_status",
            {"watch_id": watch["watch_id"], "chat_id": chat_id},
        )
        self.assertTrue(status.ok, msg=status.text)
        deliveries = status.structured["deliveries"]
        self.assertEqual(len(deliveries), 2, deliveries)
        self.assertEqual({row["status"] for row in deliveries}, {"sent"})
        self.assertEqual(len({row["period_key"] for row in deliveries}), 2)

        # Re-sending the exact same content is a duplicate without a new message.
        repeat = await self._call_b(
            "send_notification",
            {
                "watch_id": watch["watch_id"],
                "chat_id": chat_id,
                "kind": "new_items",
                "items": second_items,
            },
        )
        self.assertTrue(repeat.ok, msg=repeat.text)
        self.assertEqual(repeat.structured["status"], "duplicate", repeat.structured)
        self.assertEqual(
            repeat.structured["delivery_id"], second_sent.structured["delivery_id"]
        )

        after = self._telegram_count()
        self.assertEqual(after - before, 2, "exactly two Telegram messages were sent")


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
