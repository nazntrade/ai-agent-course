"""Unit tests for the pure user-profile module.

No database, network, Streamlit or ``.env`` is involved: everything operates on
plain ``UserProfile`` objects.
"""

import unittest

from profile import (
    PROFILE_BLOCK_TITLE,
    UserProfile,
    format_profile_block,
    normalize_profile_name,
    validate_profile_name,
)


class ValidateProfileNameTest(unittest.TestCase):
    def test_name_is_stripped(self):
        self.assertEqual(validate_profile_name("  Concise engineer  "), "Concise engineer")

    def test_non_string_is_coerced(self):
        self.assertEqual(validate_profile_name(123), "123")

    def test_empty_name_rejected(self):
        for empty in (None, "", "   ", "\n\t"):
            with self.subTest(empty=empty):
                with self.assertRaises(ValueError):
                    validate_profile_name(empty)

    def test_error_message_is_english(self):
        with self.assertRaises(ValueError) as ctx:
            validate_profile_name("")
        self.assertIn("name", str(ctx.exception).lower())


class NormalizeProfileNameTest(unittest.TestCase):
    def test_spaces_collapsed_and_case_folded(self):
        self.assertEqual(
            normalize_profile_name("  Concise   ENGINEER  "), "concise engineer"
        )

    def test_none_and_blank_become_empty(self):
        self.assertEqual(normalize_profile_name(None), "")
        self.assertEqual(normalize_profile_name("   "), "")


class UserProfileTest(unittest.TestCase):
    def test_optional_fields_default_to_empty(self):
        profile = UserProfile(id=1, name="Concise engineer")
        self.assertEqual(profile.addressing, "")
        self.assertEqual(profile.style, "")
        self.assertEqual(profile.format, "")
        self.assertEqual(profile.constraints, "")
        self.assertEqual(profile.domain_context, "")
        self.assertIsNone(profile.created_at)
        self.assertIsNone(profile.updated_at)

    def test_is_frozen(self):
        profile = UserProfile(id=1, name="p")
        with self.assertRaises(Exception):
            profile.name = "other"


class FormatProfileBlockTest(unittest.TestCase):
    def test_none_profile_is_none(self):
        self.assertIsNone(format_profile_block(None))

    def test_empty_name_is_none(self):
        self.assertIsNone(format_profile_block(UserProfile(id=1, name="")))
        self.assertIsNone(format_profile_block(UserProfile(id=1, name="   ")))

    def test_title_states_the_invariants_priority_rule(self):
        block = format_profile_block(UserProfile(id=1, name="p"))
        self.assertEqual(block.splitlines()[0], PROFILE_BLOCK_TITLE)
        self.assertIn("инварианты выше по приоритету", PROFILE_BLOCK_TITLE)

    def test_lines_and_order(self):
        profile = UserProfile(
            id=1,
            name="Concise engineer",
            addressing="direct",
            style="technical",
            format="result first",
            constraints="no filler",
            domain_context="software",
        )
        self.assertEqual(
            format_profile_block(profile).splitlines(),
            [
                PROFILE_BLOCK_TITLE,
                "- Профиль: Concise engineer",
                "- Обращение: direct",
                "- Стиль: technical",
                "- Формат: result first",
                "- Ограничения: no filler",
                "- Контекст: software",
            ],
        )

    def test_empty_fields_are_skipped(self):
        profile = UserProfile(
            id=1, name="p", style="short", domain_context="   "
        )
        self.assertEqual(
            format_profile_block(profile).splitlines(),
            [PROFILE_BLOCK_TITLE, "- Профиль: p", "- Стиль: short"],
        )

    def test_fields_are_stripped(self):
        profile = UserProfile(id=1, name="  p  ", style="  short  ")
        block = format_profile_block(profile)
        self.assertIn("- Профиль: p", block)
        self.assertIn("- Стиль: short", block)
        self.assertNotIn("  short  ", block)


if __name__ == "__main__":
    unittest.main()
