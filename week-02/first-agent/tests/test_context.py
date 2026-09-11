"""Unit tests for the pure context-compression logic in ``context.py``.

No network, no storage and no provider client are involved; everything works on
plain dicts.
"""

import unittest

from context import (
    COMPRESSION_BATCH_TURNS,
    build_payload,
    build_summarization_messages,
    clamp_covered_messages,
    plan_compression,
    validate_summary,
)


def make_pairs(turns):
    """Build ``turns`` user/assistant message pairs."""
    pairs = []
    for i in range(1, turns + 1):
        pairs.append({"role": "user", "content": f"вопрос {i}"})
        pairs.append({"role": "assistant", "content": f"ответ {i}"})
    return pairs


class BuildPayloadTest(unittest.TestCase):
    def test_without_summary_sends_full_history(self):
        pairs = make_pairs(2)
        payload = build_payload("система", pairs, "новый вопрос")
        self.assertEqual(
            [m["role"] for m in payload],
            ["system", "user", "assistant", "user", "assistant", "user"],
        )
        self.assertEqual(payload[0]["content"], "система")
        self.assertEqual(payload[-1]["content"], "новый вопрос")

    def test_with_summary_replaces_covered_messages(self):
        pairs = make_pairs(3)
        payload = build_payload(
            "система",
            pairs,
            "новый вопрос",
            summary_content="Сводка",
            covered_messages_count=2,
        )
        self.assertEqual(
            [m["role"] for m in payload],
            ["system", "system", "user", "assistant", "user", "assistant", "user"],
        )
        self.assertIn("Сводка", payload[1]["content"])
        # Covered messages (turn 1) are gone; turns 2 and 3 remain.
        contents = [m["content"] for m in payload]
        self.assertNotIn("вопрос 1", contents)
        self.assertIn("вопрос 2", contents)
        self.assertIn("вопрос 3", contents)

    def test_summarize_disabled_ignores_summary(self):
        pairs = make_pairs(2)
        payload = build_payload(
            "система",
            pairs,
            "новый вопрос",
            summary_content="Сводка",
            covered_messages_count=2,
            summarize_enabled=False,
        )
        self.assertEqual(
            [m["role"] for m in payload],
            ["system", "user", "assistant", "user", "assistant", "user"],
        )
        self.assertIn("вопрос 1", [m["content"] for m in payload])


class PlanCompressionTest(unittest.TestCase):
    def test_no_trigger_when_uncovered_within_keep(self):
        pairs = make_pairs(3)
        self.assertIsNone(plan_compression(pairs, 0, keep_recent_turns=3))

    def test_triggers_exactly_one_turn(self):
        pairs = make_pairs(4)
        plan = plan_compression(pairs, 0, keep_recent_turns=3)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.turns_to_merge, 1)
        self.assertEqual(len(plan.messages_to_merge), 2)
        self.assertEqual(plan.new_covered_messages_count, 2)

    def test_merges_multiple_turns_when_keep_lowered(self):
        pairs = make_pairs(6)
        plan = plan_compression(pairs, 0, keep_recent_turns=2)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.turns_to_merge, 4)
        self.assertEqual(len(plan.messages_to_merge), 8)
        self.assertEqual(plan.new_covered_messages_count, 8)

    def test_anchor_advances_by_merged_count(self):
        pairs = make_pairs(5)
        plan = plan_compression(pairs, 2, keep_recent_turns=2)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.new_covered_messages_count, 2 + len(plan.messages_to_merge))
        # Turn 1 already covered, turns 2 and 3 are the two merged.
        self.assertEqual(plan.messages_to_merge[0]["content"], "вопрос 2")

    def test_anchor_clamped_to_pair_count(self):
        pairs = make_pairs(2)
        self.assertIsNone(plan_compression(pairs, 100, keep_recent_turns=3))

    def test_odd_anchor_rounded_down(self):
        pairs = make_pairs(4)
        plan = plan_compression(pairs, 3, keep_recent_turns=2)
        self.assertIsNotNone(plan)
        # Anchor is clamped to an even 2, so merging starts at turn 2.
        self.assertEqual(plan.messages_to_merge[0]["content"], "вопрос 2")

    def test_batch_threshold_defers_compression(self):
        # With an existing summary (covered > 0), an excess below the batch
        # threshold yields no plan.
        pairs = make_pairs(5)
        self.assertIsNone(
            plan_compression(
                pairs, 2, keep_recent_turns=3, min_batch=COMPRESSION_BATCH_TURNS
            )
        )
        # Exactly COMPRESSION_BATCH_TURNS excess turns produce a plan that
        # folds the whole excess in one go.
        pairs = make_pairs(7)
        plan = plan_compression(
            pairs, 2, keep_recent_turns=3, min_batch=COMPRESSION_BATCH_TURNS
        )
        self.assertIsNotNone(plan)
        self.assertEqual(plan.turns_to_merge, COMPRESSION_BATCH_TURNS)
        self.assertEqual(len(plan.messages_to_merge), 6)
        self.assertEqual(plan.new_covered_messages_count, 8)

    def test_keep_change_never_skips_or_duplicates(self):
        pairs = make_pairs(6)
        covered = 4  # turns 1-2 already covered by the summary

        # A large keep suppresses compression; the payload holds the full
        # uncovered tail with no skips and no duplicates.
        self.assertIsNone(
            plan_compression(pairs, covered, keep_recent_turns=10, min_batch=1)
        )
        payload = build_payload(
            "система", pairs, "новый",
            summary_content="Сводка", covered_messages_count=covered,
        )
        contents = [m["content"] for m in payload]
        for i in range(1, 3):  # covered turns are replaced by the summary
            self.assertNotIn(f"вопрос {i}", contents)
            self.assertNotIn(f"ответ {i}", contents)
        for i in range(3, 7):  # uncovered turns appear exactly once
            self.assertEqual(contents.count(f"вопрос {i}"), 1)
            self.assertEqual(contents.count(f"ответ {i}"), 1)

        # A lowered keep folds a continuous range starting right after the
        # anchor, again with no skips or duplicates.
        plan = plan_compression(pairs, covered, keep_recent_turns=2, min_batch=1)
        self.assertIsNotNone(plan)
        merged = plan.messages_to_merge
        self.assertEqual(merged[0]["content"], "вопрос 3")
        self.assertEqual(merged[-1]["content"], "ответ 4")
        merged_contents = [m["content"] for m in merged]
        self.assertEqual(len(merged_contents), 4)
        self.assertEqual(merged_contents.count("вопрос 3"), 1)
        self.assertEqual(merged_contents.count("ответ 4"), 1)


class ClampCoveredTest(unittest.TestCase):
    def test_clamp_and_round_down(self):
        self.assertEqual(clamp_covered_messages(0, 10), 0)
        self.assertEqual(clamp_covered_messages(3, 10), 2)
        self.assertEqual(clamp_covered_messages(9, 10), 8)
        self.assertEqual(clamp_covered_messages(100, 10), 10)
        self.assertEqual(clamp_covered_messages(-5, 10), 0)


class BuildSummarizationMessagesTest(unittest.TestCase):
    def test_includes_previous_summary_only_when_present(self):
        to_merge = make_pairs(1)
        without = build_summarization_messages(None, to_merge)
        joined = " ".join(m["content"] for m in without)
        self.assertNotIn("Предыдущая сводка", joined)
        self.assertIn("вопрос 1", joined)
        self.assertIn("ответ 1", joined)

        with_prev = build_summarization_messages("Старая сводка", to_merge)
        roles = [m["role"] for m in with_prev]
        self.assertEqual(roles, ["system", "user", "user"])
        self.assertIn("Старая сводка", with_prev[1]["content"])


class ValidateSummaryTest(unittest.TestCase):
    def test_rejects_empty_or_whitespace(self):
        for bad in ("", "   ", "\n\t"):
            with self.assertRaises(ValueError):
                validate_summary(bad)

    def test_rejects_none(self):
        with self.assertRaises(ValueError):
            validate_summary(None)

    def test_accepts_non_empty(self):
        validate_summary("Короткая сводка")  # must not raise


if __name__ == "__main__":
    unittest.main()
