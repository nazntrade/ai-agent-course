"""Unit tests for the statistics aggregator in ``stats``.

Stdlib only: no network, no ``.env`` and no real provider.
"""

import unittest

from stats import TurnStats, aggregate_stats


class AggregateStatsTest(unittest.TestCase):
    def test_unknown_values_stay_none_and_last_finish_reason_wins(self):
        # Every attempt reports no exact usage or cost, so the aggregate must
        # keep None instead of inventing zeros; only the finish reason is known.
        attempts = [
            TurnStats(),
            TurnStats(),
            TurnStats(finish_reason="length"),
        ]

        aggregate = aggregate_stats(attempts)

        self.assertIsNone(aggregate.request_tokens)
        self.assertIsNone(aggregate.response_tokens)
        self.assertIsNone(aggregate.total_tokens)
        self.assertIsNone(aggregate.prompt_cache_hit_tokens)
        self.assertIsNone(aggregate.prompt_cache_miss_tokens)
        self.assertIsNone(aggregate.cost_usd)
        self.assertIsNone(aggregate.cost_assumption)
        self.assertEqual(aggregate.finish_reason, "length")


if __name__ == "__main__":
    unittest.main()
