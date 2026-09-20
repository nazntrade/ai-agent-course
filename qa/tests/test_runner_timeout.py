"""Regression tests of the live scenario timeout selection.

The real local endpoint served a 1049-token prefill in ~72 s and then generated
for minutes, so the recipe's short 90 s default must never bound a live run. The
live budget is resolved from the configuration, while the MOCK provider keeps the
short deterministic recipe budget.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import run_local_e2e
from integrations.memory_state_agent import recipe
from lib.config import (
    DEFAULT_LIVE_TIMEOUT_SECONDS,
    LOCAL_LIVE_TIMEOUT_ENV,
    LocalLlmConfig,
    load_config,
)


class ScenarioTimeoutTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.missing = str(Path(self._tmp.name) / "missing.local.json")

    def test_live_budget_is_not_the_short_recipe_default(self):
        timeout = run_local_e2e.resolve_scenario_timeout_ms(True, LocalLlmConfig())
        self.assertEqual(timeout, DEFAULT_LIVE_TIMEOUT_SECONDS * 1000)
        self.assertGreater(timeout, recipe.DEFAULT_TIMEOUT_MS * 10)

    def test_live_budget_covers_the_measured_prefill(self):
        # The real endpoint reported ~72.2 s for the prefill alone; the budget
        # must leave room for generation and one corrective retry on top.
        timeout = run_local_e2e.resolve_scenario_timeout_ms(True, LocalLlmConfig())
        self.assertGreater(timeout, 72_200)

    def test_live_budget_comes_from_the_configuration(self):
        config = load_config(
            env={LOCAL_LIVE_TIMEOUT_ENV: "2400"}, config_path=self.missing
        )
        self.assertEqual(
            run_local_e2e.resolve_scenario_timeout_ms(True, config), 2_400_000
        )

    def test_mock_budget_is_unchanged(self):
        config = load_config(
            env={LOCAL_LIVE_TIMEOUT_ENV: "2400"}, config_path=self.missing
        )
        self.assertEqual(
            run_local_e2e.resolve_scenario_timeout_ms(False, config),
            recipe.DEFAULT_TIMEOUT_MS,
        )


if __name__ == "__main__":
    unittest.main()
