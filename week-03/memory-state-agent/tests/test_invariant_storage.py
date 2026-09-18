"""Integration tests for the Day 14 invariant repository on a temporary SQLite.

Every test uses ``tempfile`` and a real database file; no network and no real
``.env`` are involved.
"""

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from agent import AgentConfig
from invariant_storage import (
    DEFAULT_INVARIANTS_SEED_MARKER,
    DuplicateInvariantCodeError,
    InvariantRepository,
)
from invariants import (
    ENFORCEMENT_ADVISORY,
    ENFORCEMENT_HARD,
    EVENT_ACTIVATED,
    EVENT_CONFLICT,
    EVENT_CREATED,
    EVENT_DEACTIVATED,
    EVENT_UPDATED,
    Invariant,
    KIND_DATA,
    KIND_POLICY,
    SCOPE_GLOBAL,
    SCOPE_TASK,
    SOURCE_SEED,
    SOURCE_USER,
)
from storage import ChatStore
from task_storage import TaskRepository
from tasks import EVENT_PAUSE


def custom_invariant(**overrides):
    fields = dict(
        code="INV-CUSTOM",
        title="Custom rule",
        text="Do not do the custom forbidden thing.",
        scope=SCOPE_GLOBAL,
        enforcement=ENFORCEMENT_HARD,
        kind=KIND_DATA,
        triggers=("custom forbidden",),
        alternative="Use the custom allowed path.",
    )
    fields.update(overrides)
    return Invariant(**fields)


class RepoTestCase(unittest.TestCase):
    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "invariants.db")
        self.store = ChatStore(self.path)
        self.chat_id = self.store.create_chat(AgentConfig())
        self.repo = InvariantRepository(self.store.db_path)

    def event_types(self, **filters):
        return [event.event_type for event in self.repo.list_events(**filters)]


class MigrationTest(RepoTestCase):
    def test_schema_and_seed_are_created_once(self):
        self.assertEqual(
            [invariant.code for invariant in self.repo.list_invariants()],
            ["INV-NO-FSM-BYPASS", "INV-NO-DATA-RESET", "INV-ADV-CONFIRM-DESTRUCTIVE"],
        )
        seeded = {invariant.code: invariant for invariant in self.repo.list_invariants()}
        self.assertEqual(seeded["INV-NO-FSM-BYPASS"].source, SOURCE_SEED)
        self.assertEqual(seeded["INV-NO-FSM-BYPASS"].enforcement, ENFORCEMENT_HARD)
        self.assertEqual(
            seeded["INV-ADV-CONFIRM-DESTRUCTIVE"].enforcement, ENFORCEMENT_ADVISORY
        )

    def test_reopening_is_idempotent(self):
        InvariantRepository(self.path)
        InvariantRepository(self.path)
        self.assertEqual(len(self.repo.list_invariants()), 3)

    def test_deleted_seed_is_not_resurrected(self):
        # No public delete API exists; emulate a raw removal, then reopen.
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("DELETE FROM invariants WHERE code = 'INV-NO-DATA-RESET'")
            conn.commit()
        reopened = InvariantRepository(self.path)
        codes = [invariant.code for invariant in reopened.list_invariants()]
        self.assertNotIn("INV-NO-DATA-RESET", codes)
        self.assertEqual(len(codes), 2)

    def test_day13_database_is_migrated_without_loss(self):
        # The store and task repo of Day 13 already created their tables.
        task_repo = TaskRepository(self.store.db_path)
        task = task_repo.create_task(self.chat_id, "Title", "Goal")
        self.store.save_turn(self.chat_id, "Old question", "Old answer")

        InvariantRepository(self.store.db_path)

        self.assertEqual(
            [message["content"] for message in self.store.load_messages(self.chat_id)],
            ["Old question", "Old answer"],
        )
        self.assertEqual(task_repo.get_task(task.id).title, "Title")
        self.assertEqual([event.event_type for event in task_repo.list_events(task.id)],
                         ["TASK_CREATED"])
        # The task journal trigger still rejects an update after the migration.
        with closing(sqlite3.connect(self.path)) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE task_events SET event_type = 'HACK' WHERE task_id = ?",
                    (task.id,),
                )

    def test_seed_marker_is_written(self):
        with closing(sqlite3.connect(self.path)) as conn:
            row = conn.execute(
                "SELECT value FROM app_state WHERE key = ?",
                (DEFAULT_INVARIANTS_SEED_MARKER,),
            ).fetchone()
        self.assertIsNotNone(row)


class CrudTest(RepoTestCase):
    def test_create_round_trip(self):
        created = self.repo.create_invariant(custom_invariant())
        self.assertIsNotNone(created.id)
        self.assertEqual(created.source, SOURCE_USER)
        self.assertEqual(created.version, 1)
        self.assertTrue(created.is_active)
        self.assertEqual(self.repo.get_by_code("INV-CUSTOM").title, "Custom rule")
        self.assertEqual(
            self.event_types(code="INV-CUSTOM"), [EVENT_CREATED]
        )

    def test_duplicate_code_is_rejected(self):
        self.repo.create_invariant(custom_invariant())
        with self.assertRaises(DuplicateInvariantCodeError):
            self.repo.create_invariant(custom_invariant())
        matching = [
            item for item in self.repo.list_invariants() if item.code == "INV-CUSTOM"
        ]
        self.assertEqual(len(matching), 1)

    def test_update_bumps_version_source_and_journals(self):
        created = self.repo.create_invariant(custom_invariant())
        updated = self.repo.update_invariant(
            created.id, title="Updated title", alternative="Another alternative"
        )
        self.assertEqual(updated.version, created.version + 1)
        self.assertEqual(updated.title, "Updated title")
        self.assertEqual(updated.alternative, "Another alternative")
        self.assertEqual(updated.source, SOURCE_USER)
        self.assertIn(EVENT_UPDATED, self.event_types(code="INV-CUSTOM"))

    def test_code_cannot_be_changed(self):
        created = self.repo.create_invariant(custom_invariant())
        with self.assertRaises(ValueError):
            self.repo.update_invariant(created.id, code="INV-OTHER")
        self.assertEqual(self.repo.get_invariant(created.id).code, "INV-CUSTOM")

    def test_deactivation_keeps_history_and_excludes_from_applicable(self):
        created = self.repo.create_invariant(custom_invariant())
        deactivated = self.repo.set_active(created.id, False)
        self.assertFalse(deactivated.is_active)
        self.assertEqual(deactivated.version, created.version + 1)
        self.assertIn(EVENT_DEACTIVATED, self.event_types(code="INV-CUSTOM"))
        self.assertNotIn(
            "INV-CUSTOM",
            [rule.code for rule in self.repo.list_applicable()],
        )
        # The rule itself is still readable; nothing was deleted.
        self.assertIsNotNone(self.repo.get_invariant(created.id))
        reactivated = self.repo.set_active(created.id, True)
        self.assertTrue(reactivated.is_active)
        self.assertIn(EVENT_ACTIVATED, self.event_types(code="INV-CUSTOM"))

    def test_no_public_delete_api(self):
        self.assertFalse(hasattr(self.repo, "delete_invariant"))
        self.assertFalse(hasattr(self.repo, "remove_invariant"))

    def test_task_scoped_rule_applies_only_to_its_task(self):
        created = self.repo.create_invariant(
            custom_invariant(
                code="INV-TASK",
                scope=SCOPE_TASK,
                task_id=42,
            )
        )
        self.assertIn(
            "INV-TASK",
            [rule.code for rule in self.repo.list_applicable(42)],
        )
        self.assertNotIn(
            "INV-TASK",
            [rule.code for rule in self.repo.list_applicable(7)],
        )
        self.assertEqual(created.task_id, 42)

    def test_applicable_order_places_hard_before_advisory(self):
        codes = [rule.code for rule in self.repo.list_applicable()]
        self.assertEqual(
            codes,
            [
                "INV-NO-DATA-RESET",
                "INV-NO-FSM-BYPASS",
                "INV-ADV-CONFIRM-DESTRUCTIVE",
            ],
        )


class JournalTest(RepoTestCase):
    def test_invariant_events_are_append_only(self):
        with closing(sqlite3.connect(self.path)) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE invariant_events SET event_type = 'HACK'")

    def test_record_conflict_truncates_the_request(self):
        created = self.repo.create_invariant(custom_invariant())
        long_request = "x" * 500
        event = self.repo.record_conflict(
            chat_id=self.chat_id,
            task_id=7,
            invariant_id=created.id,
            code=created.code,
            phase="request",
            request=long_request,
            trigger="custom forbidden",
            alternative="allowed",
        )
        self.assertEqual(event.event_type, EVENT_CONFLICT)
        self.assertLessEqual(len(event.details["request"]), 200)
        self.assertEqual(event.details["phase"], "request")
        self.assertEqual(event.details["decision"], "refused")

    def test_events_can_be_filtered_by_task_and_code(self):
        created = self.repo.create_invariant(custom_invariant())
        self.repo.record_conflict(
            task_id=5, invariant_id=created.id, code=created.code, phase="action"
        )
        self.repo.record_conflict(
            task_id=6, invariant_id=created.id, code=created.code, phase="commit"
        )
        by_task = self.repo.list_events(task_id=5)
        self.assertEqual(len(by_task), 1)
        self.assertEqual(by_task[0].details["phase"], "action")
        by_code = self.repo.list_events(code="INV-CUSTOM", limit=1)
        self.assertEqual(len(by_code), 1)

    def test_seed_creation_writes_created_events(self):
        events = self.repo.list_events()
        seeded = [event for event in events if event.event_type == EVENT_CREATED]
        self.assertEqual(len(seeded), 3)
        self.assertEqual(seeded[0].code, "INV-NO-FSM-BYPASS")


if __name__ == "__main__":
    unittest.main()
