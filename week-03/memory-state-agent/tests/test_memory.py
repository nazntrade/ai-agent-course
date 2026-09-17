"""Unit tests for the pure explicit-memory module.

No database, network, Streamlit or ``.env`` is involved: everything operates on
plain ``MemoryItem`` objects and message lists.
"""

import unittest

from memory import (
    INVARIANTS_BLOCK_TITLE,
    LONG_TERM_MEMORY_BLOCK_TITLE,
    MEMORY_SCOPE_LONG_TERM,
    MEMORY_SCOPE_WORKING,
    WORKING_MEMORY_BLOCK_TITLE,
    MemoryItem,
    build_memory_blocks,
    format_invariants_block,
    format_memory_block,
    insert_system_blocks,
    validate_memory_key,
    validate_memory_value,
)


class MemoryConstantsTest(unittest.TestCase):
    def test_scope_machine_values(self):
        self.assertEqual(MEMORY_SCOPE_WORKING, "working")
        self.assertEqual(MEMORY_SCOPE_LONG_TERM, "long_term")


class MemoryItemTest(unittest.TestCase):
    def test_defaults(self):
        item = MemoryItem(id=1, key="k", value="v")
        self.assertTrue(item.included)
        self.assertIsNone(item.updated_at)

    def test_is_frozen(self):
        item = MemoryItem(id=1, key="k", value="v")
        with self.assertRaises(Exception):
            item.key = "other"


class ValidateMemoryTest(unittest.TestCase):
    def test_key_and_value_are_stripped(self):
        self.assertEqual(validate_memory_key("  город  "), "город")
        self.assertEqual(validate_memory_value("  Москва  "), "Москва")

    def test_key_coerces_non_string(self):
        self.assertEqual(validate_memory_key(123), "123")

    def test_empty_key_rejected(self):
        for empty in (None, "", "   ", "\n\t"):
            with self.subTest(empty=empty):
                with self.assertRaises(ValueError):
                    validate_memory_key(empty)

    def test_empty_value_rejected(self):
        for empty in (None, "", "   ", "\n\t"):
            with self.subTest(empty=empty):
                with self.assertRaises(ValueError):
                    validate_memory_value(empty)

    def test_error_messages_are_english(self):
        with self.assertRaises(ValueError) as ctx:
            validate_memory_key("")
        self.assertIn("key", str(ctx.exception).lower())


class FormatMemoryBlockTest(unittest.TestCase):
    def test_none_for_empty_items(self):
        self.assertIsNone(format_memory_block([], "Title"))

    def test_none_when_all_excluded(self):
        items = [
            MemoryItem(id=1, key="a", value="1", included=False),
            MemoryItem(id=2, key="b", value="2", included=False),
        ]
        self.assertIsNone(format_memory_block(items, "Title"))

    def test_only_included_items_are_rendered(self):
        items = [
            MemoryItem(id=1, key="keep", value="yes"),
            MemoryItem(id=2, key="drop", value="no", included=False),
        ]
        block = format_memory_block(items, "Title")
        self.assertIn("- keep: yes", block)
        self.assertNotIn("drop", block)
        self.assertNotIn("no", block)

    def test_ordered_by_id(self):
        items = [
            MemoryItem(id=3, key="third", value="3"),
            MemoryItem(id=1, key="first", value="1"),
            MemoryItem(id=2, key="second", value="2"),
        ]
        block = format_memory_block(items, "Title")
        self.assertEqual(
            block.splitlines(),
            ["Title", "- first: 1", "- second: 2", "- third: 3"],
        )

    def test_title_is_first_line(self):
        block = format_memory_block(
            [MemoryItem(id=1, key="k", value="v")], "Заголовок"
        )
        self.assertEqual(block.splitlines()[0], "Заголовок")


class BuildMemoryBlocksTest(unittest.TestCase):
    def test_order_working_then_long_term(self):
        working = [MemoryItem(id=1, key="w", value="wv")]
        long_term = [MemoryItem(id=2, key="l", value="lv")]
        blocks = build_memory_blocks(working, long_term)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].splitlines()[0], WORKING_MEMORY_BLOCK_TITLE)
        self.assertEqual(blocks[1].splitlines()[0], LONG_TERM_MEMORY_BLOCK_TITLE)

    def test_empty_scopes_are_skipped(self):
        self.assertEqual(build_memory_blocks([], []), [])
        only_long = build_memory_blocks(
            [], [MemoryItem(id=1, key="l", value="lv")]
        )
        self.assertEqual(len(only_long), 1)
        self.assertEqual(only_long[0].splitlines()[0], LONG_TERM_MEMORY_BLOCK_TITLE)

    def test_all_excluded_scope_is_skipped(self):
        excluded = [MemoryItem(id=1, key="w", value="wv", included=False)]
        self.assertEqual(build_memory_blocks(excluded, excluded), [])


class InsertSystemBlocksTest(unittest.TestCase):
    def _payload(self):
        return [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]

    def test_empty_blocks_returns_payload_unchanged(self):
        payload = self._payload()
        result = insert_system_blocks(payload, [])
        self.assertIs(result, payload)

    def test_blocks_inserted_after_first_system_message(self):
        result = insert_system_blocks(self._payload(), ["A", "B"])
        self.assertEqual(
            [message["role"] for message in result],
            ["system", "system", "system", "user"],
        )
        self.assertEqual(result[1], {"role": "system", "content": "A"})
        self.assertEqual(result[2], {"role": "system", "content": "B"})
        self.assertEqual(result[0]["content"], "sys")
        self.assertEqual(result[3]["content"], "hi")

    def test_order_of_blocks_is_preserved(self):
        result = insert_system_blocks(self._payload(), ["first", "second", "third"])
        self.assertEqual(
            [result[1]["content"], result[2]["content"], result[3]["content"]],
            ["first", "second", "third"],
        )

    def test_input_payload_is_not_mutated(self):
        payload = self._payload()
        before = [dict(message) for message in payload]
        insert_system_blocks(payload, ["A"])
        self.assertEqual(payload, before)
        self.assertEqual(len(payload), 2)

    def test_blocks_prepended_without_system_message(self):
        payload = [{"role": "user", "content": "hi"}]
        result = insert_system_blocks(payload, ["A"])
        self.assertEqual([message["role"] for message in result], ["system", "user"])
        self.assertEqual(result[0]["content"], "A")

    def test_multiple_leading_system_messages_insert_after_first(self):
        payload = [
            {"role": "system", "content": "first"},
            {"role": "system", "content": "summary"},
            {"role": "user", "content": "hi"},
        ]
        result = insert_system_blocks(payload, ["A"])
        self.assertEqual(
            [message["content"] for message in result],
            ["first", "A", "summary", "hi"],
        )


class FormatInvariantsBlockTest(unittest.TestCase):
    """The single source of the invariants block of the chat payload."""

    def test_returns_none_for_empty_values(self):
        for value in (None, "", "   ", "\n\t "):
            with self.subTest(value=value):
                self.assertIsNone(format_invariants_block(value))

    def test_matches_the_day10_inline_string_byte_for_byte(self):
        text = "Не использовать сторонние библиотеки"
        expected = "Инварианты (соблюдай всегда):\n" + text
        self.assertEqual(format_invariants_block(text), expected)
        self.assertEqual(format_invariants_block(f"  {text}  "), expected)
        self.assertEqual(format_invariants_block(text), INVARIANTS_BLOCK_TITLE + "\n" + text)


if __name__ == "__main__":
    unittest.main()
