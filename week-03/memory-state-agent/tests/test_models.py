"""Unit tests for the model identifier helpers in ``models``.

Stdlib only: no network, no ``.env``, no real provider and no other project
module is imported.
"""

import unittest

from models import DEFAULT_MODEL, display_name, normalize_model


class DefaultModelTest(unittest.TestCase):
    def test_default_is_canonical(self):
        self.assertEqual(DEFAULT_MODEL, "deepseek-flash")


class NormalizeModelTest(unittest.TestCase):
    def test_exact_legacy_alias_maps_to_canonical(self):
        self.assertEqual(normalize_model("deepseek-v4-flash"), "deepseek-flash")

    def test_canonical_is_unchanged(self):
        self.assertEqual(normalize_model("deepseek-flash"), "deepseek-flash")

    def test_pro_is_unchanged(self):
        self.assertEqual(normalize_model("deepseek-v4-pro"), "deepseek-v4-pro")

    def test_unknown_is_unchanged(self):
        self.assertEqual(normalize_model("unknown-model"), "unknown-model")

    def test_suffix_is_not_an_alias(self):
        self.assertEqual(
            normalize_model("deepseek-v4-flash-extra"), "deepseek-v4-flash-extra"
        )

    def test_legacy_alias_is_case_sensitive(self):
        self.assertEqual(normalize_model("DeepSeek-V4-Flash"), "DeepSeek-V4-Flash")

    def test_empty_string_is_unchanged(self):
        self.assertEqual(normalize_model(""), "")

    def test_normalize_is_idempotent(self):
        once = normalize_model("deepseek-v4-flash")
        self.assertEqual(normalize_model(once), once)


class DisplayNameTest(unittest.TestCase):
    def test_canonical_flash_has_display_name(self):
        self.assertEqual(display_name("deepseek-flash"), "DeepSeek V4.1 Flash")

    def test_canonical_pro_has_display_name(self):
        self.assertEqual(display_name("deepseek-v4-pro"), "DeepSeek V4 Pro")

    def test_legacy_alias_resolves_to_canonical_name(self):
        self.assertEqual(display_name("deepseek-v4-flash"), "DeepSeek V4.1 Flash")

    def test_unknown_returns_none(self):
        self.assertIsNone(display_name("unknown-model"))

    def test_suffix_returns_none(self):
        self.assertIsNone(display_name("deepseek-v4-flash-extra"))

    def test_case_variant_returns_none(self):
        self.assertIsNone(display_name("DeepSeek-V4-Flash"))


if __name__ == "__main__":
    unittest.main()
