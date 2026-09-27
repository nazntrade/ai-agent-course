"""Unit tests of the notifier watch service (no network, temporary database).

The Telegram boundary is replaced by a scripted double, the database lives in a
temporary directory and the clock is injectable, so the tests exercise the real
criteria, baseline, deduplication and retry rules without a socket or the real
``.env``.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from notifier_server.db import Database
from notifier_server.telegram import (
    STATUS_FAILED,
    STATUS_NOT_CONFIGURED,
    STATUS_SENT,
    TelegramResult,
)
from notifier_server.watches import WatchService


class Clock:
    """A settable clock so schedule moves are deterministic."""

    def __init__(self, now: float = 1000.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class FakeTelegram:
    """A scripted Telegram boundary that records the messages it receives."""

    def __init__(self, status: str = STATUS_SENT, error=None, message_id="11"):
        self.status = status
        self.error = error
        self.message_id = message_id
        self.calls: list = []

    def send_message(self, text: str) -> TelegramResult:
        self.calls.append(text)
        return TelegramResult(
            status=self.status, message_id=self.message_id, error=self.error
        )


def item(title: str, url: str = "", description: str = "") -> dict:
    return {"title": title, "url": url, "description": description}


def run_ok(results: list) -> dict:
    return {"status": "ok", "results": results}


class _WatchTestCase(unittest.TestCase):
    """Every test gets its own database, clock and Telegram double."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "notifier.sqlite3"
        self.db = Database(self.path)
        self.clock = Clock()
        self.telegram = FakeTelegram()
        self.service = WatchService(
            self.db, telegram=self.telegram, clock=self.clock
        )

    def create(self, **overrides) -> dict:
        params = {
            "chat_id": "chat-1",
            "query": "xbox news",
            "keywords": ["xbox"],
            "interval_seconds": 60,
            "summary_interval_seconds": 3600,
        }
        params.update(overrides)
        return self.service.create_watch(**params)

    def consume_baseline(self, watch_id: str) -> None:
        self.service.evaluate_run(watch_id, {"status": "empty", "results": []}, "chat-1")

    def seen_fingerprints(self, watch_id: str) -> set:
        from notifier_server.repository import SeenItemRepository

        return SeenItemRepository(self.db).fingerprints(watch_id)


class CreateWatchTest(_WatchTestCase):
    """``create_watch`` validates arguments and returns the explicit criterion."""

    def test_returns_criteria_schedule_and_note(self):
        created = self.create(
            keywords=["Xbox", "Game Pass"],
            exclude=["refurbished"],
            interval_seconds=120,
            summary_interval_seconds=7200,
            source_task_id="task-1",
        )
        self.assertTrue(created["watch_id"])
        self.assertEqual(created["status"], "active")
        self.assertEqual(created["query"], "xbox news")
        self.assertEqual(created["keywords"], ["Xbox", "Game Pass"])
        self.assertEqual(created["exclude"], ["refurbished"])
        self.assertEqual(created["interval_seconds"], 120)
        self.assertEqual(created["summary_interval_seconds"], 7200)
        self.assertEqual(created["source_task_id"], "task-1")
        self.assertTrue(created["next_check_at"].endswith("Z"))

    def test_list_returns_the_stored_values(self):
        created = self.create(keywords=["Xbox"], exclude=["used"])
        listed = self.service.list_watches("chat-1")
        self.assertEqual(listed["count"], 1)
        stored = listed["watches"][0]
        self.assertEqual(stored["watch_id"], created["watch_id"])
        self.assertEqual(stored["keywords"], ["Xbox"])
        self.assertEqual(stored["exclude"], ["used"])
        self.assertEqual(stored["seen_count"], 0)
        self.assertIsNone(stored["last_delivery"])

    def test_empty_list_has_a_note(self):
        listed = self.service.list_watches("chat-1")
        self.assertEqual(listed["count"], 0)
        self.assertEqual(listed["watches"], [])
        self.assertIn("No notification watches", listed["note"])

    def test_empty_chat_is_rejected(self):
        with self.assertRaises(ToolError) as caught:
            self.create(chat_id="")
        self.assertEqual(str(caught.exception), "This tool needs an active chat context")

    def test_blank_query_is_rejected(self):
        for value in ("", "   ", None, 5):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ToolError):
                    self.create(query=value)

    def test_keywords_must_be_a_non_empty_list(self):
        for value in ([], "xbox", [""], ["  "], [1]):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ToolError):
                    self.create(keywords=value)

    def test_exclude_must_be_a_list_of_non_empty_strings(self):
        with self.assertRaises(ToolError):
            self.create(exclude="used")
        with self.assertRaises(ToolError):
            self.create(exclude=[""])

    def test_intervals_must_be_integers_above_the_minimum(self):
        for field in ("interval_seconds", "summary_interval_seconds"):
            for value in (True, 59, 0, "60", None):
                with self.subTest(field=field, value=repr(value)):
                    with self.assertRaises(ToolError):
                        self.create(**{field: value})


class CriterionTest(_WatchTestCase):
    """The criterion is deterministic, case-insensitive and excludes matches."""

    def test_keyword_matches_title_or_description_case_insensitively(self):
        watch = self.create(keywords=["xbox", "game pass"])
        self.consume_baseline(watch["watch_id"])
        result = self.service.evaluate_run(
            watch["watch_id"],
            run_ok(
                [
                    item("XBOX Series X", "https://a.test/1"),
                    item("PlayStation news", "https://b.test/2", "no match here"),
                    item("Microsoft update", "https://c.test/3", "Game Pass adds titles"),
                ]
            ),
            "chat-1",
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["matched_count"], 2)
        self.assertTrue(result["should_notify"])
        urls = {entry["url"] for entry in result["new_items"]}
        self.assertEqual(urls, {"https://a.test/1", "https://c.test/3"})

    def test_exclude_removes_matching_items(self):
        watch = self.create(keywords=["xbox"], exclude=["refurbished"])
        self.consume_baseline(watch["watch_id"])
        result = self.service.evaluate_run(
            watch["watch_id"],
            run_ok(
                [
                    item("Xbox refurbished unit", "https://a.test/1"),
                    item("Xbox new release", "https://b.test/2"),
                ]
            ),
            "chat-1",
        )
        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(result["new_items"][0]["url"], "https://b.test/2")


class BaselineTest(_WatchTestCase):
    """The first usable run is the baseline; pending/error never consume it."""

    def test_first_ok_run_is_baseline_and_writes_seen(self):
        watch = self.create()
        result = self.service.evaluate_run(
            watch["watch_id"],
            run_ok([item("Xbox one", "https://a.test/1"), item("Other", "https://b.test/2")]),
            "chat-1",
        )
        self.assertTrue(result["is_baseline"])
        self.assertFalse(result["should_notify"])
        self.assertEqual(result["new_items"], [])
        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(len(self.seen_fingerprints(watch["watch_id"])), 1)
        stored = self.service.list_watches("chat-1")["watches"][0]
        self.assertEqual(stored["seen_count"], 1)
        self.assertIsNotNone(stored["last_check_at"])

    def test_empty_run_consumes_the_baseline(self):
        watch = self.create()
        result = self.service.evaluate_run(
            watch["watch_id"], {"status": "empty", "results": []}, "chat-1"
        )
        self.assertTrue(result["is_baseline"])
        self.assertFalse(result["should_notify"])
        # The next usable run is no longer a baseline and can be notified.
        follow_up = self.service.evaluate_run(
            watch["watch_id"], run_ok([item("Xbox news", "https://a.test/1")]), "chat-1"
        )
        self.assertFalse(follow_up["is_baseline"])
        self.assertTrue(follow_up["should_notify"])

    def test_pending_does_not_consume_the_baseline_but_moves_the_schedule(self):
        watch = self.create()
        result = self.service.evaluate_run(
            watch["watch_id"], {"status": "pending"}, "chat-1"
        )
        self.assertFalse(result["is_baseline"])
        self.assertFalse(result["should_notify"])
        self.assertEqual(result["new_items"], [])
        stored = self.service.list_watches("chat-1")["watches"][0]
        self.assertIsNone(stored["last_check_at"])
        self.assertEqual(stored["next_check_at"], "1970-01-01T00:17:40Z")

        follow_up = self.service.evaluate_run(
            watch["watch_id"], {"status": "empty", "results": []}, "chat-1"
        )
        self.assertTrue(follow_up["is_baseline"])

    def test_error_does_not_consume_the_baseline(self):
        watch = self.create()
        self.service.evaluate_run(
            watch["watch_id"], {"status": "error", "error": "boom"}, "chat-1"
        )
        stored = self.service.list_watches("chat-1")["watches"][0]
        self.assertIsNone(stored["last_check_at"])
        follow_up = self.service.evaluate_run(
            watch["watch_id"], {"status": "empty", "results": []}, "chat-1"
        )
        self.assertTrue(follow_up["is_baseline"])

    def test_schedule_is_not_moved_when_something_is_new(self):
        watch = self.create()
        self.consume_baseline(watch["watch_id"])
        before = self.service.list_watches("chat-1")["watches"][0]["next_check_at"]
        result = self.service.evaluate_run(
            watch["watch_id"], run_ok([item("Xbox news", "https://a.test/1")]), "chat-1"
        )
        self.assertTrue(result["should_notify"])
        after = self.service.list_watches("chat-1")["watches"][0]["next_check_at"]
        self.assertEqual(before, after)


class EvaluateDataTest(_WatchTestCase):
    """Malformed runs and foreign watches are reported without an exception."""

    def test_malformed_runs_return_structured_error(self):
        watch = self.create()
        for value in (None, [], {"status": "weird"}, {"status": "ok"}, {"status": "ok", "results": {}}):
            with self.subTest(value=repr(value)):
                result = self.service.evaluate_run(watch["watch_id"], value, "chat-1")
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["new_items"], [])
                self.assertFalse(result["should_notify"])

    def test_unknown_or_foreign_watch_is_structured(self):
        watch = self.create()
        unknown = self.service.evaluate_run("missing", {"status": "empty", "results": []}, "chat-1")
        self.assertEqual(unknown["status"], "unknown_watch")
        foreign = self.service.evaluate_run(
            watch["watch_id"], {"status": "empty", "results": []}, "other-chat"
        )
        self.assertEqual(foreign["status"], "unknown_watch")

    def test_normal_evaluate_does_not_change_seen(self):
        watch = self.create()
        self.consume_baseline(watch["watch_id"])
        before = self.seen_fingerprints(watch["watch_id"])
        self.service.evaluate_run(
            watch["watch_id"], run_ok([item("Xbox news", "https://a.test/1")]), "chat-1"
        )
        self.assertEqual(before, self.seen_fingerprints(watch["watch_id"]))


class FingerprintTest(_WatchTestCase):
    """Fingerprints reuse ``normalize_url`` and collapse URL variants."""

    def test_url_variants_share_one_fingerprint_across_runs(self):
        watch = self.create()
        self.consume_baseline(watch["watch_id"])
        first = self.service.evaluate_run(
            watch["watch_id"],
            run_ok([item("Xbox", "https://docs.example.test/1")]),
            "chat-1",
        )
        self.service.send_notification(watch["watch_id"], "chat-1", "new_items", first["new_items"])

        second = self.service.evaluate_run(
            watch["watch_id"],
            run_ok([item("Xbox dup", "https://Docs.Example.Test/1/#fragment")]),
            "chat-1",
        )
        self.assertEqual(second["matched_count"], 1)
        self.assertEqual(second["known_count"], 1)
        self.assertFalse(second["should_notify"])

    def test_title_is_the_fallback_fingerprint(self):
        watch = self.create()
        self.consume_baseline(watch["watch_id"])
        first = self.service.evaluate_run(
            watch["watch_id"], run_ok([item("Xbox Weekly", "")]), "chat-1"
        )
        self.assertEqual(len(first["new_items"]), 1)
        self.service.send_notification(watch["watch_id"], "chat-1", "new_items", first["new_items"])
        second = self.service.evaluate_run(
            watch["watch_id"], run_ok([item("xbox weekly", "")]), "chat-1"
        )
        self.assertFalse(second["should_notify"])


class DeliveryTest(_WatchTestCase):
    """``send_notification`` sends, records seen and deduplicates by content."""

    def _fresh_items(self, watch_id: str, results: list) -> list:
        self.consume_baseline(watch_id)
        evaluated = self.service.evaluate_run(watch_id, run_ok(results), "chat-1")
        self.assertTrue(evaluated["should_notify"])
        return evaluated["new_items"]

    def test_sent_records_seen_and_moves_the_schedule(self):
        watch = self.create()
        items = self._fresh_items(watch["watch_id"], [item("Xbox one", "https://a.test/1")])
        result = self.service.send_notification(watch["watch_id"], "chat-1", "new_items", items)
        self.assertEqual(result["status"], STATUS_SENT)
        self.assertTrue(result["delivery_id"])
        self.assertEqual(len(self.telegram.calls), 1)
        self.assertIn("https://a.test/1", self.telegram.calls[0])
        self.assertEqual(len(self.seen_fingerprints(watch["watch_id"])), 1)
        stored = self.service.list_watches("chat-1")["watches"][0]
        self.assertIsNotNone(stored["last_delivery"])
        self.assertEqual(stored["last_delivery"]["status"], STATUS_SENT)

    def test_same_content_is_a_duplicate_without_network(self):
        watch = self.create()
        items = self._fresh_items(watch["watch_id"], [item("Xbox one", "https://a.test/1")])
        first = self.service.send_notification(watch["watch_id"], "chat-1", "new_items", items)
        second = self.service.send_notification(watch["watch_id"], "chat-1", "new_items", items)
        self.assertEqual(first["status"], STATUS_SENT)
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["delivery_id"], first["delivery_id"])
        self.assertEqual(len(self.telegram.calls), 1)

    def test_item_order_does_not_change_the_period_key(self):
        watch = self.create()
        items = self._fresh_items(
            watch["watch_id"],
            [item("Xbox one", "https://a.test/1"), item("Xbox two", "https://b.test/2")],
        )
        first = self.service.send_notification(watch["watch_id"], "chat-1", "new_items", items)
        reordered = list(reversed(items))
        second = self.service.send_notification(watch["watch_id"], "chat-1", "new_items", reordered)
        self.assertEqual(first["period_key"], second["period_key"])
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(len(self.telegram.calls), 1)

    def test_two_different_items_in_one_period_are_two_deliveries(self):
        watch = self.create()
        first_items = self._fresh_items(watch["watch_id"], [item("Xbox one", "https://a.test/1")])
        first = self.service.send_notification(watch["watch_id"], "chat-1", "new_items", first_items)

        # A second, different material arrives in the same time window.
        second_items = self.service.evaluate_run(
            watch["watch_id"], run_ok([item("Xbox two", "https://b.test/2")]), "chat-1"
        )["new_items"]
        second = self.service.send_notification(
            watch["watch_id"], "chat-1", "new_items", second_items
        )
        self.assertNotEqual(first["period_key"], second["period_key"])
        self.assertEqual(first["status"], STATUS_SENT)
        self.assertEqual(second["status"], STATUS_SENT)
        self.assertEqual(len(self.telegram.calls), 2)

    def test_empty_items_are_not_required_and_create_no_row(self):
        watch = self.create()
        before = self.service.get_delivery_status("", "chat-1")["count"]
        result = self.service.send_notification(watch["watch_id"], "chat-1", "new_items", [])
        self.assertEqual(result["status"], "not_required")
        self.assertIsNone(result["delivery_id"])
        after = self.service.get_delivery_status("", "chat-1")["count"]
        self.assertEqual(before, after)
        self.assertEqual(len(self.telegram.calls), 0)

    def test_wrong_chat_and_stopped_watch_are_tool_errors(self):
        watch = self.create()
        with self.assertRaises(ToolError):
            self.service.send_notification(watch["watch_id"], "other-chat", "new_items", [])
        self.service.stop_watch(watch["watch_id"], "chat-1")
        with self.assertRaises(ToolError):
            self.service.send_notification(watch["watch_id"], "chat-1", "new_items", [])

    def test_bad_kind_is_rejected(self):
        watch = self.create()
        with self.assertRaises(ToolError):
            self.service.send_notification(watch["watch_id"], "chat-1", "digest", [])


class RetryTest(_WatchTestCase):
    """A failed or unconfigured delivery is retried on the same row."""

    def test_failed_delivery_increments_attempts_and_keeps_items_new(self):
        watch = self.create()
        self.consume_baseline(watch["watch_id"])
        evaluated = self.service.evaluate_run(
            watch["watch_id"], run_ok([item("Xbox one", "https://a.test/1")]), "chat-1"
        )
        items = evaluated["new_items"]

        self.telegram.status = STATUS_FAILED
        self.telegram.error = "The Telegram API is unreachable."
        first = self.service.send_notification(watch["watch_id"], "chat-1", "new_items", items)
        self.assertEqual(first["status"], STATUS_FAILED)
        self.assertEqual(first["attempts"], 1)
        self.assertEqual(len(self.seen_fingerprints(watch["watch_id"])), 0)

        second = self.service.send_notification(watch["watch_id"], "chat-1", "new_items", items)
        self.assertEqual(second["status"], STATUS_FAILED)
        self.assertEqual(second["attempts"], 2)
        self.assertEqual(second["delivery_id"], first["delivery_id"])
        self.assertEqual(len(self.telegram.calls), 2)

        # Items are still new after a failed delivery.
        again = self.service.evaluate_run(
            watch["watch_id"], run_ok([item("Xbox one", "https://a.test/1")]), "chat-1"
        )
        self.assertTrue(again["should_notify"])

    def test_not_configured_is_honest_and_retryable(self):
        watch = self.create()
        self.consume_baseline(watch["watch_id"])
        evaluated = self.service.evaluate_run(
            watch["watch_id"], run_ok([item("Xbox one", "https://a.test/1")]), "chat-1"
        )
        items = evaluated["new_items"]

        self.telegram.status = STATUS_NOT_CONFIGURED
        self.telegram.error = "Telegram delivery is not configured on this server."
        first = self.service.send_notification(watch["watch_id"], "chat-1", "new_items", items)
        second = self.service.send_notification(watch["watch_id"], "chat-1", "new_items", items)
        self.assertEqual(first["status"], STATUS_NOT_CONFIGURED)
        self.assertEqual(second["status"], STATUS_NOT_CONFIGURED)
        self.assertEqual(second["attempts"], 2)

    def test_sent_after_failure_updates_the_same_row(self):
        watch = self.create()
        self.consume_baseline(watch["watch_id"])
        items = self.service.evaluate_run(
            watch["watch_id"], run_ok([item("Xbox one", "https://a.test/1")]), "chat-1"
        )["new_items"]

        self.telegram.status = STATUS_FAILED
        failed = self.service.send_notification(watch["watch_id"], "chat-1", "new_items", items)
        self.telegram.status = STATUS_SENT
        sent = self.service.send_notification(watch["watch_id"], "chat-1", "new_items", items)
        self.assertEqual(sent["status"], STATUS_SENT)
        self.assertEqual(sent["delivery_id"], failed["delivery_id"])
        self.assertEqual(sent["attempts"], 2)
        self.assertEqual(len(self.seen_fingerprints(watch["watch_id"])), 1)


class SummaryTest(_WatchTestCase):
    """``summary`` uses a time-based period key, not a content hash."""

    def test_summary_period_key_is_time_based(self):
        watch = self.create(summary_interval_seconds=3600)
        first = self.service.send_notification(
            watch["watch_id"], "chat-1", "summary", [item("Summary one", "https://a.test/1")]
        )
        self.assertEqual(first["status"], STATUS_SENT)
        self.assertEqual(first["period_key"], "summary:0")

        self.clock.advance(30)
        second = self.service.send_notification(
            watch["watch_id"], "chat-1", "summary", [item("Summary two", "https://b.test/2")]
        )
        # Same period: one summary per period, even with different items.
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(len(self.telegram.calls), 1)

        self.clock.advance(3600)
        third = self.service.send_notification(
            watch["watch_id"], "chat-1", "summary", [item("Summary three", "https://c.test/3")]
        )
        self.assertEqual(third["status"], STATUS_SENT)
        self.assertEqual(third["period_key"], "summary:1")


class AccumulatedRunsTest(_WatchTestCase):
    """Only the latest run is processed; intermediate items are not backfilled."""

    def test_only_the_latest_run_is_reported(self):
        watch = self.create()
        self.consume_baseline(watch["watch_id"])

        # The first, older payload is delivered and its items become seen.
        older = self.service.evaluate_run(
            watch["watch_id"],
            run_ok([item("Xbox old A", "https://a.test/old"), item("Xbox old B", "https://a.test/old-b")]),
            "chat-1",
        )["new_items"]
        self.service.send_notification(watch["watch_id"], "chat-1", "new_items", older)

        # While the monitor was stopped, other runs existed; only the latest is
        # available. The older items are absent from it and are not backfilled.
        latest = self.service.evaluate_run(
            watch["watch_id"],
            run_ok([item("Xbox new C", "https://a.test/new")]),
            "chat-1",
        )
        urls = {entry["url"] for entry in latest["new_items"]}
        self.assertEqual(urls, {"https://a.test/new"})
        self.assertEqual(latest["known_count"], 0)


class DeliveryStatusAndStopTest(_WatchTestCase):
    """``get_delivery_status`` and ``stop_watch`` report honestly."""

    def test_status_lists_recent_deliveries(self):
        watch = self.create()
        self.consume_baseline(watch["watch_id"])
        items = self.service.evaluate_run(
            watch["watch_id"], run_ok([item("Xbox one", "https://a.test/1")]), "chat-1"
        )["new_items"]
        self.service.send_notification(watch["watch_id"], "chat-1", "new_items", items)

        status = self.service.get_delivery_status(watch["watch_id"], "chat-1")
        self.assertEqual(status["count"], 1)
        delivery = status["deliveries"][0]
        self.assertEqual(delivery["status"], STATUS_SENT)
        self.assertEqual(delivery["items_count"], 1)
        self.assertTrue(delivery["created_at"].endswith("Z"))

    def test_status_requires_a_chat_and_rejects_a_foreign_watch(self):
        watch = self.create()
        with self.assertRaises(ToolError):
            self.service.get_delivery_status("", "")
        with self.assertRaises(ToolError):
            self.service.get_delivery_status(watch["watch_id"], "other-chat")

    def test_stop_is_idempotent(self):
        watch = self.create()
        first = self.service.stop_watch(watch["watch_id"], "chat-1")
        second = self.service.stop_watch(watch["watch_id"], "chat-1")
        self.assertEqual(first["status"], "stopped")
        self.assertEqual(second["status"], "stopped")
        self.assertNotEqual(first["note"], second["note"])

    def test_stop_rejects_unknown_and_foreign(self):
        watch = self.create()
        with self.assertRaises(ToolError):
            self.service.stop_watch("missing", "chat-1")
        with self.assertRaises(ToolError):
            self.service.stop_watch(watch["watch_id"], "other-chat")


class SourceGuardTest(unittest.TestCase):
    """Server B must not copy ``normalize_url`` nor import ``agent.*``."""

    def test_no_local_normalize_url_copy(self):
        root = Path(__file__).resolve().parent.parent / "notifier_server"
        for path in root.glob("*.py"):
            with self.subTest(path=path.name):
                source = path.read_text(encoding="utf-8")
                self.assertNotIn("def normalize_url", source)

    def test_no_agent_import(self):
        root = Path(__file__).resolve().parent.parent / "notifier_server"
        for path in root.glob("*.py"):
            with self.subTest(path=path.name):
                for line in path.read_text(encoding="utf-8").splitlines():
                    stripped = line.strip()
                    self.assertFalse(
                        stripped.startswith("import agent")
                        or stripped.startswith("from agent"),
                        line,
                    )


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
