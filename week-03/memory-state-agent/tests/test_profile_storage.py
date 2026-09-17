"""Integration tests for user-profile persistence in ``ChatStore``.

Every test runs against a real temporary SQLite file (no mocks, no network).
The migration test seeds a synthetic Day 10/11 database so the idempotent
Day 12 migration and the one-time demo seeds are exercised on legacy data.
"""

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from agent import AgentConfig
from storage import ChatStore, DuplicateProfileNameError
from tests.test_memory_storage import _seed_day10_db


class ProfileStorageTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "test.db")
        self.store = ChatStore(self.path)
        # Two chats keep a swapped chat id from passing by coincidence.
        self.chat_id = self.store.create_chat(AgentConfig())
        self.other_chat = self.store.create_chat(AgentConfig())

    def tearDown(self):
        self._tmp.cleanup()

    def _count(self, table, where="", params=()):
        with closing(sqlite3.connect(self.path)) as conn:
            sql = f"SELECT COUNT(*) FROM {table}"
            if where:
                sql += f" WHERE {where}"
            return conn.execute(sql, params).fetchone()[0]

    # --- CRUD -----------------------------------------------------------------

    def test_create_read_update_roundtrip(self):
        profile_id = self.store.create_profile(
            "  My profile  ", "direct", "plain", "lists", "no jargon", "software"
        )
        profile = self.store.get_profile(profile_id)
        self.assertEqual(profile.name, "My profile")
        self.assertEqual(profile.addressing, "direct")
        self.assertEqual(profile.style, "plain")
        self.assertEqual(profile.format, "lists")
        self.assertEqual(profile.constraints, "no jargon")
        self.assertEqual(profile.domain_context, "software")
        self.assertIsNotNone(profile.created_at)
        self.assertIsNotNone(profile.updated_at)

        self.store.update_profile(
            profile_id, "Renamed", "short", "technical", "bullets", "be concise", "infra"
        )
        updated = self.store.get_profile(profile_id)
        self.assertEqual(updated.name, "Renamed")
        self.assertEqual(updated.style, "technical")
        self.assertEqual(updated.created_at, profile.created_at)

    def test_blank_optional_fields_are_stored_as_empty(self):
        profile_id = self.store.create_profile("  Only name  ", None, "  ", None)
        profile = self.store.get_profile(profile_id)
        self.assertEqual(profile.name, "Only name")
        self.assertEqual(profile.addressing, "")
        self.assertEqual(profile.style, "")
        self.assertEqual(profile.format, "")

    def test_empty_name_rejected(self):
        for empty in (None, "", "   "):
            with self.subTest(empty=empty):
                with self.assertRaises(ValueError):
                    self.store.create_profile(empty)
        self.assertEqual(self._count("user_profiles"), 2)

    def test_list_is_ordered_by_id(self):
        first = self.store.create_profile("Zeta")
        second = self.store.create_profile("Alpha")
        ids = [profile.id for profile in self.store.list_profiles()]
        self.assertEqual(ids, sorted(ids))
        self.assertGreater(second, first)

    # --- Duplicate names ------------------------------------------------------

    def test_duplicate_name_on_create_is_case_insensitive(self):
        with self.assertRaises(DuplicateProfileNameError) as ctx:
            self.store.create_profile("  concise   ENGINEER ")
        self.assertEqual(ctx.exception.name, "concise   ENGINEER")
        # Nothing was inserted; the demo profile is untouched.
        self.assertEqual(self._count("user_profiles"), 2)
        self.assertEqual(
            self.store.list_profiles()[0].name, "Concise engineer"
        )

    def test_duplicate_name_on_update_is_rejected(self):
        first = self.store.create_profile("Alpha")
        second = self.store.create_profile("Beta")
        with self.assertRaises(DuplicateProfileNameError):
            self.store.update_profile(second, "  alpha  ")
        self.assertEqual(self.store.get_profile(second).name, "Beta")
        self.assertEqual(self.store.get_profile(first).name, "Alpha")

    def test_update_to_own_name_is_allowed(self):
        profile_id = self.store.create_profile("Alpha")
        self.store.update_profile(profile_id, "  Alpha  ")
        self.assertEqual(self.store.get_profile(profile_id).name, "Alpha")

    def test_duplicate_name_on_clone_is_rejected(self):
        source = self.store.create_profile("Alpha")
        with self.assertRaises(DuplicateProfileNameError):
            self.store.clone_profile(source, " ALPHA ")
        self.assertEqual(self._count("user_profiles"), 3)

    # --- Missing ids ----------------------------------------------------------

    def test_unknown_ids(self):
        self.assertIsNone(self.store.get_profile(99999))
        with self.assertRaises(KeyError):
            self.store.update_profile(99999, "x")
        with self.assertRaises(KeyError):
            self.store.delete_profile(99999)
        with self.assertRaises(KeyError):
            self.store.clone_profile(99999, "x")

    def test_delete_missing_profile_changes_nothing(self):
        before = self._count("user_profiles")
        with self.assertRaises(KeyError):
            self.store.delete_profile(99999)
        self.assertEqual(self._count("user_profiles"), before)

    # --- Clone ----------------------------------------------------------------

    def test_clone_is_an_independent_copy(self):
        source = self.store.create_profile(
            "Original", "addr", "style", "fmt", "con", "dom"
        )
        clone = self.store.clone_profile(source, "Copy")
        self.store.update_profile(clone, "Copy", "changed")

        original = self.store.get_profile(source)
        cloned = self.store.get_profile(clone)
        self.assertEqual(original.addressing, "addr")
        self.assertEqual(cloned.addressing, "changed")
        self.assertEqual(original.name, "Original")
        self.assertEqual(cloned.name, "Copy")
        self.assertIsNotNone(cloned.created_at)

    # --- Active profile per chat ----------------------------------------------

    def test_active_profile_is_per_chat(self):
        first = self.store.create_profile("A")
        second = self.store.create_profile("B")
        self.assertIsNone(self.store.get_active_profile(self.chat_id))

        self.store.set_active_profile(self.chat_id, first)
        self.store.set_active_profile(self.other_chat, second)

        self.assertEqual(self.store.get_active_profile(self.chat_id).id, first)
        self.assertEqual(self.store.get_active_profile(self.other_chat).id, second)

    def test_set_active_none_selects_no_profile(self):
        profile_id = self.store.create_profile("A")
        self.store.set_active_profile(self.chat_id, profile_id)
        self.store.set_active_profile(self.chat_id, None)
        self.assertIsNone(self.store.get_active_profile(self.chat_id))

    def test_set_active_unknown_ids_raise_and_change_nothing(self):
        with self.assertRaises(KeyError):
            self.store.set_active_profile(99999, None)
        with self.assertRaises(KeyError):
            self.store.set_active_profile(self.chat_id, 99999)
        self.assertIsNone(self.store.get_active_profile(self.chat_id))

    def test_get_active_profile_for_unknown_chat_is_none(self):
        self.assertIsNone(self.store.get_active_profile(99999))

    def test_delete_active_profile_falls_back_in_all_chats(self):
        profile_id = self.store.create_profile("A")
        self.store.set_active_profile(self.chat_id, profile_id)
        self.store.set_active_profile(self.other_chat, profile_id)
        self.store.save_turn(self.chat_id, "вопрос", "ответ")

        self.store.delete_profile(profile_id)

        self.assertIsNone(self.store.get_active_profile(self.chat_id))
        self.assertIsNone(self.store.get_active_profile(self.other_chat))
        self.assertIsNone(self.store.get_profile(profile_id))
        self.assertEqual(
            self._count("chats", "active_profile_id IS NOT NULL"), 0
        )
        # The chat is untouched and still reads normally.
        self.assertEqual(
            [message["content"] for message in self.store.load_messages(self.chat_id)],
            ["вопрос", "ответ"],
        )

    # --- Persistence and seeds ------------------------------------------------

    def test_profiles_and_assignment_survive_reopen(self):
        profile_id = self.store.create_profile("Persisted", "addr")
        self.store.set_active_profile(self.chat_id, profile_id)

        reopened = ChatStore(self.path)

        self.assertEqual(reopened.get_profile(profile_id).name, "Persisted")
        self.assertEqual(reopened.get_profile(profile_id).addressing, "addr")
        self.assertEqual(reopened.get_active_profile(self.chat_id).id, profile_id)

    def test_fresh_db_seeds_two_contrastive_profiles(self):
        profiles = self.store.list_profiles()
        self.assertEqual(
            [profile.name for profile in profiles],
            ["Concise engineer", "Detailed tutor"],
        )
        concise, detailed = profiles
        self.assertNotEqual(concise.style, detailed.style)
        self.assertNotEqual(concise.format, detailed.format)
        self.assertNotEqual(concise.addressing, detailed.addressing)

    def test_deleted_demo_profiles_do_not_return(self):
        for profile in self.store.list_profiles():
            self.store.delete_profile(profile.id)
        self.assertEqual(self.store.list_profiles(), [])

        reopened = ChatStore(self.path)
        self.assertEqual(reopened.list_profiles(), [])


class ProfileMigrationTest(unittest.TestCase):
    """Opening a Day 10/11 database adds ``active_profile_id`` and seeds once."""

    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)

    def test_migration_adds_column_seeds_once_and_keeps_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "legacy.db")
            _seed_day10_db(path)

            store = ChatStore(path)

            with closing(sqlite3.connect(path)) as conn:
                row = conn.execute(
                    "SELECT active_profile_id FROM chats WHERE id = 1"
                ).fetchone()
            # A legacy chat reads as No profile.
            self.assertIsNone(row[0])
            self.assertIsNone(store.get_active_profile(1))
            self.assertEqual(len(store.list_profiles()), 2)

            # Reopening is idempotent: no duplicate seeds, data intact.
            reopened = ChatStore(path)
            self.assertEqual(len(reopened.list_profiles()), 2)
            self.assertEqual(
                [message["content"] for message in reopened.load_messages(1)],
                ["Привет мир", "Здравствуйте"],
            )

            # CRUD works on the migrated database.
            profile_id = reopened.create_profile("Custom")
            reopened.set_active_profile(1, profile_id)
            self.assertEqual(reopened.get_active_profile(1).id, profile_id)


if __name__ == "__main__":
    unittest.main()
