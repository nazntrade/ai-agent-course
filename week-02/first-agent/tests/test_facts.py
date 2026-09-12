"""Tests for sticky facts: pure parsing/formatting and storage merging.

No network and no real provider are involved. Storage tests use a temporary
SQLite database only.
"""

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from agent import AgentConfig
from facts import (
    FACT_STATUS_ACTIVE,
    FACT_STATUS_CANCELLED,
    FACT_STATUS_REPLACED,
    Fact,
    FactOperation,
    build_facts_extraction_messages,
    format_facts_block,
    parse_facts_response,
)
from storage import ChatStore
from stats import TurnStats


class ParseFactsResponseTest(unittest.TestCase):
    def test_valid_active_fact(self):
        ops = parse_facts_response(
            '{"facts": [{"key": "city", "value": "Москва", "category": "parameter"}]}'
        )
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].key, "city")
        self.assertEqual(ops[0].value, "Москва")
        self.assertEqual(ops[0].category, "parameter")
        self.assertEqual(ops[0].status, FACT_STATUS_ACTIVE)

    def test_valid_cancelled_fact_with_reason(self):
        ops = parse_facts_response(
            '{"facts": [{"key": "city", "status": "cancelled", "reason": "неактуально"}]}'
        )
        self.assertEqual(ops[0].status, FACT_STATUS_CANCELLED)
        self.assertEqual(ops[0].reason, "неактуально")
        self.assertEqual(ops[0].value, "")

    def test_empty_list_is_valid(self):
        self.assertEqual(parse_facts_response('{"facts": []}'), [])

    def test_markdown_fence_is_stripped(self):
        text = '```json\n{"facts": [{"key": "k", "value": "v"}]}\n```'
        ops = parse_facts_response(text)
        self.assertEqual(ops[0].key, "k")

    def test_unknown_category_falls_back_to_other(self):
        ops = parse_facts_response(
            '{"facts": [{"key": "k", "value": "v", "category": "unknown"}]}'
        )
        self.assertEqual(ops[0].category, "other")

    def test_missing_status_defaults_to_active(self):
        ops = parse_facts_response('{"facts": [{"key": "k", "value": "v"}]}')
        self.assertEqual(ops[0].status, FACT_STATUS_ACTIVE)

    def test_invalid_responses_raise(self):
        invalid = [
            "",
            "not json",
            "[]",
            '{"facts": {}}',
            '{"facts": [1]}',
            '{"facts": [{"value": "v"}]}',
            '{"facts": [{"key": "k"}]}',
            '{"facts": [{"key": "k", "value": "v", "status": "replaced"}]}',
        ]
        for text in invalid:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_facts_response(text)

    def test_none_raises(self):
        with self.assertRaises(ValueError):
            parse_facts_response(None)


class FormatFactsBlockTest(unittest.TestCase):
    @staticmethod
    def _fact(fact_id, category, key, value, status=FACT_STATUS_ACTIVE):
        return Fact(
            id=fact_id,
            category=category,
            key=key,
            value=value,
            status=status,
            reason=None,
            updated_at=None,
        )

    def test_none_when_no_active_facts(self):
        facts = [self._fact(1, "goal", "a", "b", FACT_STATUS_CANCELLED)]
        self.assertIsNone(format_facts_block(facts))
        self.assertIsNone(format_facts_block([]))

    def test_only_active_included_and_deterministic_order(self):
        facts = [
            self._fact(1, "other", "b", "2"),
            self._fact(2, "goal", "z", "1"),
            self._fact(3, "goal", "a", "0"),
            self._fact(4, "goal", "c", "старое", FACT_STATUS_REPLACED),
        ]
        block = format_facts_block(facts)
        goal_a = block.index("a")
        goal_z = block.index("z")
        other_b = block.index("b")
        self.assertLess(goal_a, goal_z)  # same category sorted by key
        self.assertLess(goal_z, other_b)  # category order wins
        self.assertNotIn("старое", block)


class BuildFactsExtractionMessagesTest(unittest.TestCase):
    def test_includes_prompt_active_facts_and_messages(self):
        fact = Fact(1, "goal", "проект", "Орбита", FACT_STATUS_ACTIVE, None, None)
        messages = [{"role": "user", "content": "привет"}]
        payload = build_facts_extraction_messages([fact], messages)
        self.assertEqual(payload[0]["role"], "system")
        joined = " ".join(m["content"] for m in payload)
        self.assertIn("проект", joined)
        self.assertIn("привет", joined)

    def test_without_active_facts_has_no_facts_block(self):
        payload = build_facts_extraction_messages([], [{"role": "user", "content": "x"}])
        self.assertEqual(len(payload), 2)
        self.assertNotIn("Текущие активные факты", payload[1]["content"])


class FactsStorageTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "test.db")
        self.store = ChatStore(self.path)
        self.chat_id = self.store.create_chat(AgentConfig(context_strategy="sticky_facts"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_insert_refine_noop_cancel(self):
        self.store.save_facts(
            self.chat_id,
            [
                FactOperation("city", "Москва", "parameter"),
                FactOperation("name", "Алекс", "other"),
            ],
            2,
        )
        facts = {fact.key: fact for fact in self.store.load_facts(self.chat_id)}
        self.assertEqual(facts["city"].value, "Москва")
        self.assertEqual(facts["name"].value, "Алекс")
        # Repeating the same value is a no-op: the row id is unchanged.
        city_id = facts["city"].id
        self.store.save_facts(self.chat_id, [FactOperation("city", "Москва")], 4)
        facts = {fact.key: fact for fact in self.store.load_facts(self.chat_id)}
        self.assertEqual(facts["city"].id, city_id)
        # A different value replaces the old row and inserts exactly one active.
        self.store.save_facts(self.chat_id, [FactOperation("city", "Казань")], 6)
        active = [f for f in self.store.load_facts(self.chat_id) if f.key == "city"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].value, "Казань")
        all_city = [
            f for f in self.store.load_facts(self.chat_id, include_inactive=True)
            if f.key == "city"
        ]
        statuses = sorted(f.status for f in all_city if f.id != active[0].id)
        self.assertEqual(statuses, [FACT_STATUS_REPLACED])
        # Cancellation marks the active row and leaves no active one.
        self.store.save_facts(
            self.chat_id, [FactOperation("city", "", status=FACT_STATUS_CANCELLED)], 8
        )
        self.assertEqual(
            [f for f in self.store.load_facts(self.chat_id) if f.key == "city"], []
        )
        cancelled = [
            f for f in self.store.load_facts(self.chat_id, include_inactive=True)
            if f.key == "city" and f.status == FACT_STATUS_CANCELLED
        ]
        self.assertEqual(len(cancelled), 1)

    def test_cancel_without_active_is_noop(self):
        self.store.save_facts(
            self.chat_id, [FactOperation("ghost", "", status=FACT_STATUS_CANCELLED)], 5
        )
        self.assertEqual(self.store.load_facts(self.chat_id), [])
        self.assertEqual(self.store.get_facts_anchor(self.chat_id), 5)

    def test_empty_operations_advance_anchor(self):
        self.store.save_facts(self.chat_id, [], 3)
        self.assertEqual(self.store.get_facts_anchor(self.chat_id), 3)
        self.store.save_facts(self.chat_id, [], 10)
        self.assertEqual(self.store.get_facts_anchor(self.chat_id), 10)

    def test_anchor_is_monotonic(self):
        self.store.save_facts(self.chat_id, [], 10)
        self.store.save_facts(self.chat_id, [], 4)
        self.assertEqual(self.store.get_facts_anchor(self.chat_id), 10)

    def test_ok_and_fail_buckets_are_separate_and_skip_none(self):
        self.store.record_facts_attempt(
            self.chat_id,
            TurnStats(request_tokens=100, response_tokens=50, cost_usd=0.001),
            failed=False,
        )
        self.store.record_facts_attempt(
            self.chat_id,
            TurnStats(request_tokens=10, response_tokens=5),
            failed=True,
        )
        self.store.record_facts_attempt(self.chat_id, TurnStats(), failed=True)
        stats = self.store.get_chat_stats(self.chat_id)
        self.assertEqual(stats.facts_ok_input_tokens, 100)
        self.assertEqual(stats.facts_ok_output_tokens, 50)
        self.assertAlmostEqual(stats.facts_ok_cost_usd, 0.001)
        self.assertEqual(stats.facts_fail_input_tokens, 10)
        self.assertEqual(stats.facts_fail_output_tokens, 5)
        self.assertIsNone(stats.facts_fail_cost_usd)

    def test_record_facts_attempt_does_not_touch_updated_at(self):
        with closing(sqlite3.connect(self.path)) as conn:
            before = conn.execute(
                "SELECT updated_at FROM chats WHERE id = ?", (self.chat_id,)
            ).fetchone()[0]
        self.store.record_facts_attempt(
            self.chat_id, TurnStats(request_tokens=1), failed=False
        )
        with closing(sqlite3.connect(self.path)) as conn:
            after = conn.execute(
                "SELECT updated_at FROM chats WHERE id = ?", (self.chat_id,)
            ).fetchone()[0]
        self.assertEqual(before, after)

    def test_save_facts_error_rolls_back_facts_and_anchor(self):
        self.store.save_facts(self.chat_id, [FactOperation("city", "Москва")], 2)
        before = self.store.load_facts(self.chat_id, include_inactive=True)
        anchor_before = self.store.get_facts_anchor(self.chat_id)

        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                "CREATE TRIGGER fail_facts BEFORE INSERT ON facts "
                "BEGIN SELECT RAISE(ABORT, 'injected failure'); END;"
            )
            conn.commit()

        with self.assertRaises(sqlite3.Error):
            self.store.save_facts(
                self.chat_id, [FactOperation("name", "Алекс")], 20
            )

        after = self.store.load_facts(self.chat_id, include_inactive=True)
        self.assertEqual([f.id for f in after], [f.id for f in before])
        self.assertEqual(self.store.get_facts_anchor(self.chat_id), anchor_before)

    def test_delete_chat_cascades_facts(self):
        self.store.save_facts(self.chat_id, [FactOperation("city", "Москва")], 2)
        self.store.delete_chat(self.chat_id)
        with closing(sqlite3.connect(self.path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM facts WHERE chat_id = ?", (self.chat_id,)
            ).fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
