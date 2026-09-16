"""Unit tests for the pure context-strategy helpers and config normalization."""

import unittest

from strategies import (
    DEFAULT_FACTS_WINDOW,
    DEFAULT_SLIDING_WINDOW,
    DEFAULT_STRATEGY,
    STRATEGY_CHOICES,
    STRATEGY_FACTS,
    STRATEGY_FULL,
    STRATEGY_SLIDING,
    STRATEGY_SUMMARY,
    normalize_strategy,
    normalize_window,
    strategy_label,
    summary_compression_enabled,
)
from agent import AgentConfig


class StrategyHelpersTest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(DEFAULT_STRATEGY, STRATEGY_SUMMARY)
        self.assertEqual(DEFAULT_SLIDING_WINDOW, 6)
        self.assertEqual(DEFAULT_FACTS_WINDOW, 6)
        self.assertEqual(len(STRATEGY_CHOICES), 5)

    def test_normalize_known_values(self):
        for value in ("full", "summary", "sliding", "sticky_facts", "branching"):
            self.assertEqual(normalize_strategy(value), value)

    def test_normalize_unknown_falls_back_to_summary(self):
        for value in (None, "", "nonsense", 5):
            self.assertEqual(normalize_strategy(value), STRATEGY_SUMMARY)

    def test_strategy_label_returns_label_for_known_and_fallback(self):
        self.assertEqual(strategy_label(STRATEGY_FULL), "Full history")
        self.assertEqual(strategy_label("unknown"), strategy_label(STRATEGY_SUMMARY))

    def test_normalize_window(self):
        self.assertEqual(normalize_window(3, 6), 3)
        self.assertEqual(normalize_window(1, 6), 1)
        self.assertEqual(normalize_window(0, 6), 1)
        self.assertEqual(normalize_window(-4, 6), 1)
        self.assertEqual(normalize_window(None, 6), 6)
        self.assertEqual(normalize_window("bad", 6), 6)
        self.assertEqual(normalize_window("7", 6), 7)

    def test_summary_compression_enabled(self):
        self.assertTrue(summary_compression_enabled(STRATEGY_SUMMARY, True))
        self.assertFalse(summary_compression_enabled(STRATEGY_SUMMARY, False))
        self.assertFalse(summary_compression_enabled(STRATEGY_FULL, True))
        self.assertFalse(summary_compression_enabled(STRATEGY_SLIDING, True))
        self.assertFalse(summary_compression_enabled(STRATEGY_FACTS, True))


class AgentConfigStrategyTest(unittest.TestCase):
    def test_defaults(self):
        config = AgentConfig()
        self.assertEqual(config.context_strategy, STRATEGY_SUMMARY)
        self.assertTrue(config.summarize)
        self.assertEqual(config.sliding_window_messages, DEFAULT_SLIDING_WINDOW)
        self.assertEqual(config.facts_window_messages, DEFAULT_FACTS_WINDOW)

    def test_summarize_false_maps_to_full(self):
        self.assertEqual(AgentConfig(summarize=False).context_strategy, STRATEGY_FULL)

    def test_unknown_strategy_falls_back_to_summary(self):
        self.assertEqual(
            AgentConfig(context_strategy="wat").context_strategy, STRATEGY_SUMMARY
        )

    def test_explicit_strategy_is_not_overwritten(self):
        config = AgentConfig(context_strategy=STRATEGY_SLIDING, summarize=True)
        self.assertEqual(config.context_strategy, STRATEGY_SLIDING)
        # summarize is left untouched, it only gates the summary pipeline.
        self.assertTrue(config.summarize)

    def test_windows_are_clamped(self):
        config = AgentConfig(sliding_window_messages=0, facts_window_messages=-3)
        self.assertEqual(config.sliding_window_messages, 1)
        self.assertEqual(config.facts_window_messages, 1)

    def test_none_windows_use_defaults(self):
        config = AgentConfig(
            sliding_window_messages=None, facts_window_messages=None
        )
        self.assertEqual(config.sliding_window_messages, DEFAULT_SLIDING_WINDOW)
        self.assertEqual(config.facts_window_messages, DEFAULT_FACTS_WINDOW)


if __name__ == "__main__":
    unittest.main()
