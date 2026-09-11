"""Unit tests for the Streamlit-free UI helpers in ``app_logic``.

No Streamlit, no network, no ``.env`` and no real database are involved; the
placeholder is a small fake recording its calls.
"""

import unittest
from dataclasses import replace

from agent import AgentConfig
from app_logic import WaitingIndicator, configs_equal


class FakePlaceholder:
    """Minimal duck-typed placeholder that records ``markdown``/``empty`` calls."""

    def __init__(self):
        self.markdown_calls = []
        self.empty_calls = 0

    def markdown(self, text):
        self.markdown_calls.append(text)

    def empty(self):
        self.empty_calls += 1


class FakeSpinner:
    """Minimal duck-typed spinner context manager recording enter/exit."""

    def __init__(self):
        self.entered = 0
        self.exited = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc):
        self.exited += 1
        return False


class ConfigsEqualTest(unittest.TestCase):
    def setUp(self):
        self.base = AgentConfig(
            model="deepseek-flash",
            system_prompt="Ты — помощник",
            temperature=0.2,
            max_tokens=1500,
            stream=True,
            demo_context_limit=None,
        )

    def test_none_vs_zero_demo_limit_equal(self):
        self.assertTrue(
            configs_equal(self.base, replace(self.base, demo_context_limit=0))
        )

    def test_zero_vs_none_demo_limit_equal(self):
        zero = replace(self.base, demo_context_limit=0)
        self.assertTrue(configs_equal(zero, replace(zero, demo_context_limit=None)))

    def test_disabled_to_positive_not_equal(self):
        self.assertFalse(
            configs_equal(self.base, replace(self.base, demo_context_limit=1000))
        )
        self.assertFalse(
            configs_equal(
                replace(self.base, demo_context_limit=0),
                replace(self.base, demo_context_limit=1000),
            )
        )

    def test_positive_to_disabled_not_equal(self):
        positive = replace(self.base, demo_context_limit=1000)
        self.assertFalse(
            configs_equal(positive, replace(positive, demo_context_limit=0))
        )
        self.assertFalse(
            configs_equal(positive, replace(positive, demo_context_limit=None))
        )

    def test_model_change_not_equal(self):
        self.assertFalse(
            configs_equal(self.base, replace(self.base, model="other-model"))
        )

    def test_system_prompt_change_not_equal(self):
        self.assertFalse(
            configs_equal(self.base, replace(self.base, system_prompt="Другой промпт"))
        )

    def test_max_tokens_change_not_equal(self):
        self.assertFalse(configs_equal(self.base, replace(self.base, max_tokens=42)))

    def test_stream_change_not_equal(self):
        self.assertFalse(configs_equal(self.base, replace(self.base, stream=False)))

    def test_summarize_change_not_equal(self):
        self.assertFalse(
            configs_equal(self.base, replace(self.base, summarize=False))
        )
        self.assertFalse(
            configs_equal(
                replace(self.base, summarize=False),
                replace(self.base, summarize=True),
            )
        )

    def test_keep_recent_turns_change_not_equal(self):
        self.assertFalse(
            configs_equal(self.base, replace(self.base, keep_recent_turns=5))
        )
        self.assertFalse(
            configs_equal(self.base, replace(self.base, keep_recent_turns=0))
        )

    def test_same_summarize_and_keep_equal(self):
        other = replace(self.base, summarize=True, keep_recent_turns=3)
        self.assertTrue(configs_equal(self.base, other))

    def test_temperature_beyond_tolerance_not_equal(self):
        self.assertFalse(
            configs_equal(self.base, replace(self.base, temperature=0.2 + 1e-6))
        )

    def test_temperature_within_tolerance_equal(self):
        self.assertTrue(
            configs_equal(self.base, replace(self.base, temperature=0.2 + 1e-10))
        )

    def test_identical_configs_equal(self):
        self.assertTrue(configs_equal(self.base, replace(self.base)))


class WaitingIndicatorTest(unittest.TestCase):
    def test_spinner_entered_at_construction_not_exited(self):
        spinner = FakeSpinner()
        WaitingIndicator(spinner, FakePlaceholder())
        self.assertEqual(spinner.entered, 1)
        self.assertEqual(spinner.exited, 0)

    def test_show_chunk_closes_spinner_and_delegates_to_markdown(self):
        spinner = FakeSpinner()
        placeholder = FakePlaceholder()
        indicator = WaitingIndicator(spinner, placeholder)
        indicator.show_chunk("Привет")
        indicator.show_chunk("Привет, мир")
        self.assertEqual(spinner.exited, 1)
        self.assertEqual(placeholder.markdown_calls, ["Привет", "Привет, мир"])

    def test_clear_closes_spinner_and_delegates_to_empty(self):
        spinner = FakeSpinner()
        placeholder = FakePlaceholder()
        indicator = WaitingIndicator(spinner, placeholder)
        indicator.clear()
        indicator.clear()
        self.assertEqual(spinner.exited, 1)
        self.assertEqual(placeholder.empty_calls, 2)

    def test_show_chunk_then_clear_closes_spinner_once(self):
        spinner = FakeSpinner()
        placeholder = FakePlaceholder()
        indicator = WaitingIndicator(spinner, placeholder)
        indicator.show_chunk("Привет")
        indicator.clear()
        self.assertEqual(spinner.exited, 1)
        self.assertEqual(spinner.entered, 1)


if __name__ == "__main__":
    unittest.main()
