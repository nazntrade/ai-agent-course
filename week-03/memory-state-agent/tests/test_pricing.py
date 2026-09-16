import unittest
from datetime import datetime, timezone

from pricing import estimate_cost, is_peak_time
from stats import TurnStats


class IsPeakTimeTest(unittest.TestCase):
    def test_weekend_is_off_peak(self):
        # Saturday 2024-01-06 02:00 UTC falls inside a peak hour window but is
        # excluded because it is a weekend day.
        self.assertFalse(is_peak_time(datetime(2024, 1, 6, 2, 0, tzinfo=timezone.utc)))

    def test_peak_windows_weekday(self):
        # Monday 2024-01-01: [01:00,04:00) and [06:00,10:00) are peak.
        for hour in (1, 2, 3, 6, 7, 8, 9):
            self.assertTrue(
                is_peak_time(datetime(2024, 1, 1, hour, 0, tzinfo=timezone.utc)),
                f"hour {hour} should be peak",
            )

    def test_off_peak_windows_weekday(self):
        for hour in (0, 4, 5, 10, 11, 23):
            self.assertFalse(
                is_peak_time(datetime(2024, 1, 1, hour, 0, tzinfo=timezone.utc)),
                f"hour {hour} should be off-peak",
            )


class EstimateCostTest(unittest.TestCase):
    def test_unknown_model(self):
        stats = TurnStats(request_tokens=100, response_tokens=50)
        est = estimate_cost("unknown-model", stats)
        self.assertIsNone(est.cost_usd)
        self.assertFalse(est.tariff_known)
        self.assertIsNone(est.window)
        self.assertIsNone(est.assumption)

    def test_missing_usage_returns_none(self):
        est = estimate_cost("deepseek-v4-flash", TurnStats())
        self.assertIsNone(est.cost_usd)
        self.assertTrue(est.tariff_known)
        self.assertIsNone(est.window)

    def test_missing_request_tokens_returns_none(self):
        stats = TurnStats(
            response_tokens=50, prompt_cache_hit_tokens=10, prompt_cache_miss_tokens=90
        )
        est = estimate_cost("deepseek-v4-flash", stats)
        self.assertIsNone(est.cost_usd)

    def test_cache_split_off_peak(self):
        dt = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)  # off-peak
        stats = TurnStats(
            request_tokens=1000,
            response_tokens=500,
            prompt_cache_hit_tokens=400,
            prompt_cache_miss_tokens=600,
        )
        est = estimate_cost("deepseek-v4-flash", stats, dt)
        input_usd = (600 * 0.15 + 400 * 0.003) / 1e6
        output_usd = 500 * 0.60 / 1e6
        self.assertAlmostEqual(est.cost_usd, input_usd + output_usd, places=12)
        self.assertTrue(est.cache_split_known)
        self.assertEqual(est.window, "off_peak")
        self.assertIn("cache hit/miss", est.assumption)

    def test_no_cache_split_uses_miss_rate(self):
        dt = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)  # off-peak
        stats = TurnStats(request_tokens=1000, response_tokens=500)
        est = estimate_cost("deepseek-v4-flash", stats, dt)
        input_usd = 1000 * 0.15 / 1e6
        output_usd = 500 * 0.60 / 1e6
        self.assertAlmostEqual(est.cost_usd, input_usd + output_usd, places=12)
        self.assertFalse(est.cache_split_known)
        self.assertIn("cache-miss", est.assumption)

    def test_peak_window_rates(self):
        dt = datetime(2024, 1, 1, 2, 0, tzinfo=timezone.utc)  # peak
        stats = TurnStats(request_tokens=1000, response_tokens=500)
        est = estimate_cost("deepseek-v4-flash", stats, dt)
        self.assertEqual(est.window, "peak")
        input_usd = 1000 * 0.30 / 1e6
        output_usd = 500 * 1.20 / 1e6
        self.assertAlmostEqual(est.cost_usd, input_usd + output_usd, places=12)


class CanonicalFlashTariffTest(unittest.TestCase):
    """The canonical ``deepseek-flash`` must be priced exactly like the legacy ID."""

    def test_canonical_model_has_tariff(self):
        est = estimate_cost(
            "deepseek-flash", TurnStats(request_tokens=1000, response_tokens=500)
        )
        self.assertTrue(est.tariff_known)
        self.assertIsNotNone(est.cost_usd)

    def test_cache_split_off_peak(self):
        dt = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)  # off-peak
        stats = TurnStats(
            request_tokens=1000,
            response_tokens=500,
            prompt_cache_hit_tokens=400,
            prompt_cache_miss_tokens=600,
        )
        est = estimate_cost("deepseek-flash", stats, dt)
        input_usd = (600 * 0.15 + 400 * 0.003) / 1e6
        output_usd = 500 * 0.60 / 1e6
        self.assertAlmostEqual(est.cost_usd, input_usd + output_usd, places=12)
        self.assertTrue(est.cache_split_known)
        self.assertEqual(est.window, "off_peak")
        self.assertIn("cache hit/miss", est.assumption)

    def test_no_cache_split_uses_miss_rate(self):
        dt = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)  # off-peak
        stats = TurnStats(request_tokens=1000, response_tokens=500)
        est = estimate_cost("deepseek-flash", stats, dt)
        input_usd = 1000 * 0.15 / 1e6
        output_usd = 500 * 0.60 / 1e6
        self.assertAlmostEqual(est.cost_usd, input_usd + output_usd, places=12)
        self.assertFalse(est.cache_split_known)
        self.assertIn("cache-miss", est.assumption)

    def test_peak_window_rates(self):
        dt = datetime(2024, 1, 1, 2, 0, tzinfo=timezone.utc)  # peak
        stats = TurnStats(request_tokens=1000, response_tokens=500)
        est = estimate_cost("deepseek-flash", stats, dt)
        self.assertEqual(est.window, "peak")
        input_usd = 1000 * 0.30 / 1e6
        output_usd = 500 * 1.20 / 1e6
        self.assertAlmostEqual(est.cost_usd, input_usd + output_usd, places=12)

    def test_canonical_and_legacy_costs_match(self):
        dt = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
        stats = TurnStats(
            request_tokens=1234,
            response_tokens=567,
            prompt_cache_hit_tokens=100,
            prompt_cache_miss_tokens=1134,
        )
        canonical = estimate_cost("deepseek-flash", stats, dt)
        legacy = estimate_cost("deepseek-v4-flash", stats, dt)
        self.assertAlmostEqual(canonical.cost_usd, legacy.cost_usd, places=12)
        self.assertEqual(canonical.assumption, legacy.assumption)
        self.assertEqual(canonical.window, legacy.window)


if __name__ == "__main__":
    unittest.main()
