"""Integration tests for the Day 13 task repository on a temporary SQLite.

Every test uses ``tempfile`` and a real database file; no network and no real
``.env`` are involved.
"""

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from agent import AgentConfig
from memory import MEMORY_SCOPE_LONG_TERM
from storage import ChatStore
from task_storage import (
    DEFAULT_WORKFLOW_NAME,
    TaskDuplicateEventError,
    TaskNotFoundError,
    TaskRepository,
    TaskUsage,
    TaskVersionConflictError,
)
from tasks import (
    ARTIFACT_EXECUTION_RESULT,
    ARTIFACT_FINAL_RESULT,
    ARTIFACT_PLAN,
    ARTIFACT_SPECIFICATION,
    ARTIFACT_TASK_BRIEF,
    EVENT_API_ERROR,
    EVENT_EXECUTION_FINISHED,
    EVENT_PAUSE,
    EVENT_PLAN_ACCEPTED,
    EVENT_PLAN_CREATED,
    EVENT_PLAN_REJECTED,
    EVENT_RESUME,
    EVENT_RETRY,
    EVENT_STEP_COMPLETED,
    EVENT_TASK_CREATED,
    EVENT_VALIDATION_FAILED,
    EVENT_VALIDATION_PASSED,
    EXPECTED_CONFIRM_PLAN,
    EXPECTED_FINISH_EXECUTION,
    EXPECTED_RUN_PLANNING,
    EXPECTED_RUN_STEP,
    EXPECTED_RUN_VALIDATION,
    STAGE_DONE,
    STAGE_EXECUTION,
    STAGE_PLANNING,
    STAGE_VALIDATION,
    STATUS_ACTIVE,
    STATUS_COMPLETED,
    STATUS_PAUSED,
    apply_transition,
)

PLAN_TWO_STEPS = {
    "summary": "Two step plan",
    "acceptance_criteria": ["First criterion"],
    "steps": [
        {"index": 1, "title": "Step one", "description": "Do the first thing"},
        {"index": 2, "title": "Step two", "description": "Do the second thing"},
    ],
}


def commit(repo, task, event, payload=None, progress=None):
    """Apply one event in the domain and commit it through the repository."""
    transition = apply_transition(task, event, payload=payload, progress=progress)
    return repo.commit_transition(task.id, task.version, transition)


def raw_count(path, table, where=""):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]


def raw_rows(path, query, params=()):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(query, params).fetchall()


class RepoTestCase(unittest.TestCase):
    """Common setup: a temporary database with one chat."""

    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "test.db")
        self.store = ChatStore(self.path)
        self.chat_id = self.store.create_chat(AgentConfig())
        self.repo = TaskRepository(self.store.db_path)

    def create_task(self, chat_id=None, **kwargs):
        return self.repo.create_task(
            self.chat_id if chat_id is None else chat_id,
            kwargs.pop("title", "Task title"),
            kwargs.pop("goal", "Task goal"),
            **kwargs,
        )


class TaskCreationTest(RepoTestCase):
    def test_create_task_round_trip(self):
        task = self.create_task(task_brief="The brief")

        self.assertIsNotNone(task.id)
        self.assertEqual(task.chat_id, self.chat_id)
        self.assertEqual(task.workflow_name, DEFAULT_WORKFLOW_NAME)
        self.assertIsNotNone(task.workflow_profile_id)
        self.assertEqual(task.title, "Task title")
        self.assertEqual(task.goal, "Task goal")
        self.assertEqual(task.stage, STAGE_PLANNING)
        self.assertEqual(task.status, STATUS_ACTIVE)
        self.assertEqual(task.version, 1)
        self.assertEqual(task.expected_action_type, EXPECTED_RUN_PLANNING)
        self.assertEqual(task.expected_action_text, "Run planning")
        self.assertEqual(task.current_step, "Planning")
        self.assertIsNone(task.current_step_index)
        self.assertEqual(task.pause_reason, "")
        self.assertIsNotNone(task.created_at)
        self.assertIsNotNone(task.updated_at)

        events = self.repo.list_events(task.id)
        self.assertEqual([event.event_type for event in events], [EVENT_TASK_CREATED])
        self.assertEqual(events[0].to_stage, STAGE_PLANNING)
        self.assertEqual(events[0].to_status, STATUS_ACTIVE)
        self.assertIsNone(events[0].idempotency_key)

        artifacts = self.repo.list_artifacts(task.id)
        self.assertEqual([artifact.kind for artifact in artifacts], [ARTIFACT_TASK_BRIEF])
        self.assertEqual(artifacts[0].revision, 1)
        self.assertEqual(artifacts[0].content, {"text": "The brief"})
        self.assertEqual(artifacts[0].stage, STAGE_PLANNING)

    def test_no_brief_writes_no_artifact(self):
        task = self.create_task()
        self.assertEqual(self.repo.list_artifacts(task.id), [])

    def test_no_specification_or_plan_before_planning(self):
        task = self.create_task(task_brief="Brief")
        kinds = [artifact.kind for artifact in self.repo.list_artifacts(task.id)]
        self.assertNotIn(ARTIFACT_SPECIFICATION, kinds)
        self.assertNotIn(ARTIFACT_PLAN, kinds)
        self.assertIsNone(self.repo.load_plan(task.id))
        self.assertEqual(self.repo.step_progress(task.id), [])

    def test_create_task_validates_title_and_goal(self):
        with self.assertRaises(ValueError):
            self.repo.create_task(self.chat_id, "   ", "Goal")
        with self.assertRaises(ValueError):
            self.repo.create_task(self.chat_id, "Title", "")

    def test_create_task_unknown_chat_raises(self):
        with self.assertRaises(TaskNotFoundError):
            self.repo.create_task(99999, "Title", "Goal")

    def test_unknown_workflow_falls_back_to_default(self):
        task = self.create_task(workflow_name="missing-workflow")
        self.assertEqual(task.workflow_name, DEFAULT_WORKFLOW_NAME)

    def test_get_task_and_list_tasks(self):
        first = self.create_task(title="First")
        second = self.create_task(title="Second")
        self.assertEqual(
            [task.title for task in self.repo.list_tasks(self.chat_id)],
            ["First", "Second"],
        )
        self.assertEqual(self.repo.get_task(first.id).title, "First")
        self.assertIsNone(self.repo.get_task(99999))

    def test_add_artifact_assigns_the_next_revision(self):
        task = self.create_task()
        first = self.repo.add_artifact(
            task.id, STAGE_PLANNING, ARTIFACT_TASK_BRIEF, {"text": "one"}
        )
        second = self.repo.add_artifact(
            task.id, STAGE_PLANNING, ARTIFACT_TASK_BRIEF, {"text": "two"}
        )
        self.assertEqual((first.revision, second.revision), (1, 2))
        self.assertEqual(self.repo.get_artifact(second.id).content, {"text": "two"})
        self.assertEqual(len(self.repo.list_artifacts(task.id, kind=ARTIFACT_TASK_BRIEF)), 2)

    def test_add_artifact_rejects_unknown_kind_or_task(self):
        task = self.create_task()
        with self.assertRaises(ValueError):
            self.repo.add_artifact(task.id, STAGE_PLANNING, "nope", {})
        with self.assertRaises(TaskNotFoundError):
            self.repo.add_artifact(99999, STAGE_PLANNING, ARTIFACT_TASK_BRIEF, {})
        self.assertIsNone(self.repo.get_artifact(99999))

    def test_latest_event_type(self):
        task = self.create_task()
        self.assertEqual(self.repo.latest_event_type(task.id), EVENT_TASK_CREATED)
        plan_created = commit(
            self.repo, task, EVENT_PLAN_CREATED, {"plan": PLAN_TWO_STEPS}
        )
        self.assertEqual(self.repo.latest_event_type(task.id), EVENT_PLAN_CREATED)
        self.assertEqual(plan_created.version, 2)
        self.assertIsNone(self.repo.latest_event_type(99999))


class WorkflowSeedTest(RepoTestCase):
    def test_default_workflow_is_seeded_once(self):
        workflows = self.repo.list_workflows()
        self.assertEqual(len(workflows), 1)
        profile = workflows[0]
        self.assertEqual(profile.name, DEFAULT_WORKFLOW_NAME)
        self.assertEqual(profile.display_name, "Default workflow")
        self.assertTrue(profile.is_default)
        self.assertEqual(
            list(profile.stages), [STAGE_PLANNING, STAGE_EXECUTION, STAGE_VALIDATION, STAGE_DONE]
        )
        self.assertEqual(
            sorted(profile.instructions), ["execution", "planning", "validation"]
        )
        self.assertEqual(profile.executors["planning"], "chat")

    def test_reopening_does_not_duplicate_the_seed(self):
        TaskRepository(self.path)
        TaskRepository(self.path)
        self.assertEqual(len(self.repo.list_workflows()), 1)

    def test_user_edits_survive_reopening(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                "UPDATE workflow_profiles SET display_name = ?, instructions_json = ? "
                "WHERE name = ?",
                ("My workflow", '{"planning": "custom"}', DEFAULT_WORKFLOW_NAME),
            )
            conn.commit()

        reopened = TaskRepository(self.path)
        profile = reopened.get_workflow()
        self.assertEqual(profile.display_name, "My workflow")
        self.assertEqual(profile.instructions, {"planning": "custom"})
        self.assertEqual(len(reopened.list_workflows()), 1)

    def test_broken_json_falls_back_instead_of_raising(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                "UPDATE workflow_profiles SET stages_json = 'not json', "
                "instructions_json = 'nope', executors_json = '[]' "
                "WHERE name = ?",
                (DEFAULT_WORKFLOW_NAME,),
            )
            conn.commit()

        profile = TaskRepository(self.path).get_workflow()
        self.assertEqual(profile.stages, (STAGE_PLANNING, STAGE_EXECUTION, STAGE_VALIDATION, STAGE_DONE))
        self.assertEqual(profile.instructions, {})
        self.assertEqual(profile.executors, {})

    def test_unknown_name_returns_the_default(self):
        profile = self.repo.get_workflow("does-not-exist")
        self.assertEqual(profile.name, DEFAULT_WORKFLOW_NAME)


class MigrationTest(RepoTestCase):
    def test_existing_chat_database_is_migrated_in_place(self):
        self.store.save_turn(self.chat_id, "Old question", "Old answer")

        repo = TaskRepository(self.store.db_path)

        # Chat data survived and the new tables start empty for old chats.
        self.assertEqual(
            [message["content"] for message in self.store.load_messages(self.chat_id)],
            ["Old question", "Old answer"],
        )
        self.assertEqual(len(self.store.list_chats()), 1)
        self.assertEqual(repo.list_tasks(self.chat_id), [])
        self.assertEqual(raw_count(self.path, "tasks"), 0)
        self.assertEqual(raw_count(self.path, "task_artifacts"), 0)
        self.assertEqual(raw_count(self.path, "task_events"), 0)

    def test_repeated_opening_is_idempotent(self):
        store = ChatStore(self.path)
        TaskRepository(self.path)
        TaskRepository(self.path)
        reopened_store = ChatStore(self.path)
        repo = TaskRepository(reopened_store.db_path)

        task = repo.create_task(self.chat_id, "Title", "Goal")
        TaskRepository(self.path)
        self.assertEqual(repo.get_task(task.id).title, "Title")
        self.assertEqual(len(repo.list_workflows()), 1)
        self.assertEqual(len(store.list_chats()), 1)

    def test_existing_rows_are_not_rewritten(self):
        before = raw_rows(self.path, "SELECT * FROM chats ORDER BY id")
        TaskRepository(self.path)
        TaskRepository(self.path)
        after = raw_rows(self.path, "SELECT * FROM chats ORDER BY id")
        self.assertEqual(before, after)

    def test_foreign_keys_are_enforced(self):
        # A task pointing at a missing chat must be rejected by the database.
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO tasks (chat_id, title, goal, stage, status) "
                    "VALUES (99999, 't', 'g', 'planning', 'active')"
                )


class CommitTransitionTest(RepoTestCase):
    def test_plan_transition_round_trip(self):
        task = self.create_task(task_brief="Brief")
        transition = apply_transition(
            task,
            EVENT_PLAN_CREATED,
            payload={"plan": PLAN_TWO_STEPS, "task_brief": "Brief"},
        )
        updated = self.repo.commit_transition(task.id, task.version, transition)

        self.assertEqual(updated.version, 2)
        self.assertEqual(updated.expected_action_type, EXPECTED_CONFIRM_PLAN)
        self.assertEqual(updated.current_step, "Step one")
        self.assertEqual(updated.current_step_index, 1)
        self.assertEqual(self.repo.load_plan(task.id), PLAN_TWO_STEPS)

        artifacts = self.repo.list_artifacts(task.id)
        kinds = [artifact.kind for artifact in artifacts]
        self.assertEqual(
            kinds, [ARTIFACT_TASK_BRIEF, ARTIFACT_SPECIFICATION, ARTIFACT_PLAN]
        )
        revisions = {artifact.kind: artifact.revision for artifact in artifacts}
        self.assertEqual(revisions[ARTIFACT_SPECIFICATION], 1)
        self.assertEqual(revisions[ARTIFACT_PLAN], 1)

        events = self.repo.list_events(task.id)
        self.assertEqual(
            [event.event_type for event in events], [EVENT_TASK_CREATED, EVENT_PLAN_CREATED]
        )
        self.assertEqual(events[1].idempotency_key, f"run_planning:{task.id}:1")
        self.assertEqual(events[1].payload["step_count"], 2)

    def test_replan_after_reject_writes_new_revisions(self):
        task = self.create_task()
        task = commit(self.repo, task, EVENT_PLAN_CREATED, {"plan": PLAN_TWO_STEPS})
        task = commit(self.repo, task, EVENT_PLAN_REJECTED, {})

        self.assertEqual(task.version, 3)
        self.assertEqual(task.expected_action_type, EXPECTED_RUN_PLANNING)
        self.assertEqual(self.repo.load_plan(task.id), PLAN_TWO_STEPS)

        task = commit(self.repo, task, EVENT_PLAN_CREATED, {"plan": PLAN_TWO_STEPS})
        revisions = {
            artifact.kind: artifact.revision
            for artifact in self.repo.list_artifacts(task.id)
            if artifact.kind in (ARTIFACT_PLAN, ARTIFACT_SPECIFICATION)
        }
        self.assertEqual(revisions[ARTIFACT_PLAN], 2)
        self.assertEqual(revisions[ARTIFACT_SPECIFICATION], 2)

    def test_version_conflict_keeps_everything_unchanged(self):
        task = self.create_task()
        transition = apply_transition(
            task, EVENT_PLAN_CREATED, payload={"plan": PLAN_TWO_STEPS}
        )
        self.repo.commit_transition(task.id, task.version, transition)

        # The same call with the stale version must be rejected.
        with self.assertRaises(TaskVersionConflictError):
            self.repo.commit_transition(task.id, task.version, transition)

        stored = self.repo.get_task(task.id)
        self.assertEqual(stored.version, 2)
        self.assertEqual(stored.expected_action_type, EXPECTED_CONFIRM_PLAN)
        self.assertEqual(
            [event.event_type for event in self.repo.list_events(task.id)],
            [EVENT_TASK_CREATED, EVENT_PLAN_CREATED],
        )
        self.assertEqual(
            len(self.repo.list_artifacts(task.id, kind=ARTIFACT_PLAN)), 1
        )

    def test_duplicate_idempotency_key_is_rejected_and_rolled_back(self):
        task = self.create_task()
        first = apply_transition(
            task, EVENT_PLAN_CREATED, payload={"plan": PLAN_TWO_STEPS}
        )
        task = self.repo.commit_transition(task.id, task.version, first)

        rejected = apply_transition(task, EVENT_PLAN_REJECTED, payload={})
        # Force the key of the already committed event: the partial unique index
        # rejects the second event and the whole commit rolls back.
        rejected.idempotency_key = first.idempotency_key
        rejected.event.idempotency_key = first.idempotency_key

        with self.assertRaises(TaskDuplicateEventError):
            self.repo.commit_transition(task.id, task.version, rejected)

        stored = self.repo.get_task(task.id)
        self.assertEqual(stored.version, task.version)
        self.assertEqual(stored.expected_action_type, EXPECTED_CONFIRM_PLAN)
        self.assertEqual(
            [event.event_type for event in self.repo.list_events(task.id)],
            [EVENT_TASK_CREATED, EVENT_PLAN_CREATED],
        )
        self.assertEqual(
            len(self.repo.list_artifacts(task.id, kind=ARTIFACT_PLAN)), 1
        )

    def test_commit_is_atomic_when_an_artifact_write_fails(self):
        task = self.create_task()
        transition = apply_transition(
            task, EVENT_PLAN_CREATED, payload={"plan": PLAN_TWO_STEPS}
        )
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                "CREATE TRIGGER fail_artifact BEFORE INSERT ON task_artifacts "
                "BEGIN SELECT RAISE(ABORT, 'injected failure'); END;"
            )
            conn.commit()

        with self.assertRaises(sqlite3.Error):
            self.repo.commit_transition(task.id, task.version, transition)

        stored = self.repo.get_task(task.id)
        self.assertEqual(stored.version, 1)
        self.assertEqual(stored.stage, STAGE_PLANNING)
        self.assertEqual(stored.expected_action_type, EXPECTED_RUN_PLANNING)
        self.assertEqual(
            [event.event_type for event in self.repo.list_events(task.id)],
            [EVENT_TASK_CREATED],
        )
        self.assertEqual(self.repo.list_artifacts(task.id), [])

    def test_commit_unknown_task_raises(self):
        task = self.create_task()
        transition = apply_transition(
            task, EVENT_PLAN_CREATED, payload={"plan": PLAN_TWO_STEPS}
        )
        with self.assertRaises(TaskNotFoundError):
            self.repo.commit_transition(99999, 1, transition)

    def test_rework_writes_a_new_execution_revision(self):
        task = self.create_task()
        task = commit(self.repo, task, EVENT_PLAN_CREATED, {"plan": PLAN_TWO_STEPS})
        task = commit(
            self.repo,
            task,
            EVENT_PLAN_ACCEPTED,
            progress=self.repo.step_progress(task.id),
        )
        task = commit(
            self.repo,
            task,
            EVENT_STEP_COMPLETED,
            {"text": "first attempt"},
            progress=self.repo.step_progress(task.id),
        )
        task = commit(
            self.repo,
            task,
            EVENT_STEP_COMPLETED,
            {"text": "second step"},
            progress=self.repo.step_progress(task.id),
        )
        task = commit(
            self.repo,
            task,
            EVENT_EXECUTION_FINISHED,
            progress=self.repo.step_progress(task.id),
        )
        task = commit(
            self.repo,
            task,
            EVENT_VALIDATION_FAILED,
            {
                "passed": False,
                "defects": [{"step_index": 1, "description": "first step is wrong"}],
            },
            progress=self.repo.step_progress(task.id),
        )
        task = commit(
            self.repo,
            task,
            EVENT_STEP_COMPLETED,
            {"text": "first attempt redone"},
            progress=self.repo.step_progress(task.id),
        )

        executions = self.repo.list_artifacts(task.id, kind=ARTIFACT_EXECUTION_RESULT)
        self.assertEqual([artifact.revision for artifact in executions], [1, 2, 3])
        self.assertEqual(
            [artifact.content["step_index"] for artifact in executions], [1, 2, 1]
        )
        self.assertEqual([artifact.content["round"] for artifact in executions], [1, 1, 2])
        # The earlier revision is never overwritten.
        self.assertEqual(executions[0].content["text"], "first attempt")


class AppendOnlyTest(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.task = self.create_task()
        self.task = commit(
            self.repo, self.task, EVENT_PLAN_CREATED, {"plan": PLAN_TWO_STEPS}
        )

    def test_event_update_is_rejected_by_the_trigger(self):
        event_id = self.repo.list_events(self.task.id)[0].id
        with closing(sqlite3.connect(self.path)) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE task_events SET event_type = 'TAMPERED' WHERE id = ?",
                    (event_id,),
                )
            conn.rollback()
        self.assertEqual(
            self.repo.list_events(self.task.id)[0].event_type, EVENT_TASK_CREATED
        )

    def test_duplicate_artifact_revision_is_rejected(self):
        artifact = self.repo.list_artifacts(self.task.id, kind=ARTIFACT_PLAN)[0]
        with closing(sqlite3.connect(self.path)) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO task_artifacts (task_id, stage, kind, revision, content) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (self.task.id, STAGE_PLANNING, ARTIFACT_PLAN, artifact.revision, "{}"),
                )
            conn.rollback()
        self.assertEqual(
            len(self.repo.list_artifacts(self.task.id, kind=ARTIFACT_PLAN)), 1
        )

    def test_repository_never_uses_insert_or_replace_for_task_tables(self):
        source = Path(__file__).resolve().parent.parent.joinpath("task_storage.py")
        text = source.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "INSERT OR REPLACE" in line:
                self.assertIn("app_state", line)

    def test_public_api_has_no_delete_or_update_helpers(self):
        for forbidden in ("delete_task", "delete_event", "update_event", "update_artifact"):
            self.assertFalse(hasattr(self.repo, forbidden))


class CascadeTest(RepoTestCase):
    def test_deleting_a_chat_cascades_and_keeps_long_term_memory(self):
        memory_id = self.store.add_memory_item(
            MEMORY_SCOPE_LONG_TERM, "fact", "value"
        )
        task = self.create_task(task_brief="Brief")
        task = commit(
            self.repo, task, EVENT_PLAN_CREATED, {"plan": PLAN_TWO_STEPS}
        )
        self.repo.set_active_task_id(self.chat_id, task.id)
        self.assertEqual(self.repo.get_active_task_id(self.chat_id), task.id)

        self.store.delete_chat(self.chat_id)

        self.assertEqual(raw_count(self.path, "tasks"), 0)
        self.assertEqual(raw_count(self.path, "task_artifacts"), 0)
        self.assertEqual(raw_count(self.path, "task_events"), 0)
        self.assertEqual(self.repo.get_task(task.id), None)
        self.assertEqual(self.repo.list_tasks(self.chat_id), [])

        # The dangling selection is cleared by the resolver instead of breaking.
        self.assertIsNone(self.repo.resolve_display_task(self.chat_id))
        self.assertIsNone(self.repo.get_active_task_id(self.chat_id))

        long_term = self.store.list_memory_items(MEMORY_SCOPE_LONG_TERM)
        self.assertEqual([item.id for item in long_term], [memory_id])


class SelectionTest(RepoTestCase):
    def test_set_get_clear_round_trip(self):
        task = self.create_task()
        self.repo.set_active_task_id(self.chat_id, task.id)
        self.assertEqual(self.repo.get_active_task_id(self.chat_id), task.id)
        self.repo.clear_selection(self.chat_id)
        self.assertIsNone(self.repo.get_active_task_id(self.chat_id))

    def test_set_rejects_a_foreign_task(self):
        other_chat = self.store.create_chat(AgentConfig())
        task = self.create_task(chat_id=other_chat)
        with self.assertRaises(TaskNotFoundError):
            self.repo.set_active_task_id(self.chat_id, task.id)

    def test_corrupt_selection_value_yields_none(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)",
                (f"active_task:{self.chat_id}", "not-a-number"),
            )
            conn.commit()
        self.assertIsNone(self.repo.get_active_task_id(self.chat_id))

    def test_dangling_selection_falls_back_to_the_newest_task(self):
        first = self.create_task(title="First")
        second = self.create_task(title="Second")
        self.repo.set_active_task_id(self.chat_id, first.id)

        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM tasks WHERE id = ?", (first.id,))
            conn.commit()

        resolved = self.repo.resolve_display_task(self.chat_id)
        self.assertEqual(resolved.id, second.id)
        self.assertIsNone(self.repo.get_active_task_id(self.chat_id))

    def test_resolve_returns_the_selected_task(self):
        first = self.create_task(title="First")
        second = self.create_task(title="Second")
        self.repo.set_active_task_id(self.chat_id, first.id)
        self.assertEqual(self.repo.resolve_display_task(self.chat_id).id, first.id)
        self.repo.set_active_task_id(self.chat_id, second.id)
        self.assertEqual(self.repo.resolve_display_task(self.chat_id).id, second.id)

    def test_resolve_without_tasks_is_none(self):
        self.assertIsNone(self.repo.resolve_display_task(self.chat_id))


class RestartAndProgressTest(RepoTestCase):
    def test_state_survives_a_new_repository_instance(self):
        task = self.create_task(task_brief="Brief")
        task = commit(self.repo, task, EVENT_PLAN_CREATED, {"plan": PLAN_TWO_STEPS})
        task = commit(
            self.repo,
            task,
            EVENT_PLAN_ACCEPTED,
            progress=self.repo.step_progress(task.id),
        )
        task = commit(
            self.repo,
            task,
            EVENT_STEP_COMPLETED,
            {"text": "one"},
            progress=self.repo.step_progress(task.id),
        )

        reopened = TaskRepository(self.path)
        restored = reopened.get_task(task.id)
        self.assertEqual(restored.version, task.version)
        self.assertEqual(restored.stage, STAGE_EXECUTION)
        self.assertEqual(restored.current_step_index, 2)
        self.assertEqual(
            [artifact.revision for artifact in reopened.list_artifacts(task.id, kind=ARTIFACT_EXECUTION_RESULT)],
            [1],
        )
        progress = reopened.step_progress(task.id)
        self.assertTrue(progress[0].completed)
        self.assertFalse(progress[1].completed)

    def test_pause_and_resume_survive_a_restart(self):
        task = self.create_task()
        paused = commit(self.repo, task, EVENT_PAUSE, {"reason": "break"})
        self.assertEqual(paused.status, STATUS_PAUSED)
        self.assertEqual(paused.expected_action_type, EXPECTED_RUN_PLANNING)

        reopened = TaskRepository(self.path)
        restored = reopened.get_task(task.id)
        self.assertEqual(restored.status, STATUS_PAUSED)
        self.assertEqual(restored.stage, STAGE_PLANNING)
        self.assertEqual(restored.expected_action_text, "Run planning")
        self.assertEqual(restored.expected_action_type, EXPECTED_RUN_PLANNING)

        resumed = commit(reopened, restored, EVENT_RESUME, {})
        self.assertEqual(resumed.status, STATUS_ACTIVE)
        self.assertEqual(resumed.pause_reason, "")
        self.assertEqual(resumed.version, 3)

    def test_full_loop_with_rework_is_derived_from_artifacts(self):
        task = self.create_task()
        task = commit(self.repo, task, EVENT_PLAN_CREATED, {"plan": PLAN_TWO_STEPS})
        task = commit(
            self.repo, task, EVENT_PLAN_ACCEPTED, progress=self.repo.step_progress(task.id)
        )
        self.assertEqual(task.current_step_index, 1)
        task = commit(
            self.repo,
            task,
            EVENT_STEP_COMPLETED,
            {"text": "one"},
            progress=self.repo.step_progress(task.id),
        )
        task = commit(
            self.repo,
            task,
            EVENT_STEP_COMPLETED,
            {"text": "two"},
            progress=self.repo.step_progress(task.id),
        )
        self.assertEqual(task.expected_action_type, EXPECTED_FINISH_EXECUTION)
        task = commit(
            self.repo,
            task,
            EVENT_EXECUTION_FINISHED,
            progress=self.repo.step_progress(task.id),
        )
        self.assertEqual(task.stage, STAGE_VALIDATION)
        self.assertEqual(task.expected_action_type, EXPECTED_RUN_VALIDATION)

        task = commit(
            self.repo,
            task,
            EVENT_VALIDATION_FAILED,
            {
                "passed": False,
                "defects": [{"step_index": 2, "description": "step two is wrong"}],
            },
            progress=self.repo.step_progress(task.id),
        )
        self.assertEqual(task.stage, STAGE_EXECUTION)
        self.assertEqual(task.current_step_index, 2)
        self.assertEqual(task.expected_action_type, EXPECTED_RUN_STEP)
        progress = self.repo.step_progress(task.id)
        self.assertTrue(progress[0].completed)
        self.assertTrue(progress[1].awaiting_rework)
        self.assertEqual(progress[1].defects, ("step two is wrong",))
        self.assertEqual(progress[1].round, 1)

        # Rework the defective step only.
        first_event_count = len(self.repo.list_events(task.id))
        task = commit(
            self.repo,
            task,
            EVENT_STEP_COMPLETED,
            {"text": "two again"},
            progress=progress,
        )
        progress = self.repo.step_progress(task.id)
        self.assertTrue(progress[1].completed)
        self.assertFalse(progress[1].awaiting_rework)
        self.assertEqual(progress[1].round, 2)
        self.assertEqual(
            len(self.repo.list_artifacts(task.id, kind=ARTIFACT_EXECUTION_RESULT)), 3
        )
        self.assertEqual(len(self.repo.list_events(task.id)), first_event_count + 1)

        task = commit(
            self.repo,
            task,
            EVENT_EXECUTION_FINISHED,
            progress=progress,
        )
        transition = apply_transition(
            task, EVENT_VALIDATION_PASSED, payload={"notes": "ok"}, progress=progress
        )
        task = self.repo.commit_transition(task.id, task.version, transition)
        self.assertEqual(task.stage, STAGE_DONE)
        self.assertEqual(task.status, STATUS_COMPLETED)
        final = self.repo.list_artifacts(task.id, kind=ARTIFACT_FINAL_RESULT)
        self.assertEqual(len(final), 1)
        self.assertEqual(final[0].revision, 1)
        self.assertIn("Task title", final[0].content["markdown"])


class AppendEventTest(RepoTestCase):
    def test_api_error_and_retry_do_not_touch_the_task(self):
        task = self.create_task()
        before = self.repo.get_task(task.id)

        error = self.repo.append_event(
            task.id,
            EVENT_API_ERROR,
            payload={"kind": "provider_error", "message": "boom", "attempts": 1},
        )
        self.assertEqual(error.event_type, EVENT_API_ERROR)
        self.assertEqual(error.payload["kind"], "provider_error")
        self.assertIsNone(error.idempotency_key)
        self.assertEqual(self.repo.get_task(task.id), before)

        retry = self.repo.append_event(
            task.id, EVENT_RETRY, payload={"attempts": 1}
        )
        self.assertEqual(retry.event_type, EVENT_RETRY)
        self.assertIsNone(retry.idempotency_key)
        self.assertEqual(self.repo.get_task(task.id), before)
        self.assertEqual(self.repo.latest_event_type(task.id), EVENT_RETRY)

    def test_several_api_errors_do_not_conflict(self):
        task = self.create_task()
        for kind in ("provider_error", "stream_error", "truncated"):
            self.repo.append_event(task.id, EVENT_API_ERROR, payload={"kind": kind})
        self.assertEqual(raw_count(self.path, "task_events", "WHERE idempotency_key IS NULL"), 4)
        self.assertEqual(len(self.repo.list_events(task.id)), 4)

    def test_retry_without_a_previous_api_error_is_rejected(self):
        task = self.create_task()
        from tasks import InvalidTransitionError

        with self.assertRaises(InvalidTransitionError):
            self.repo.append_event(task.id, EVENT_RETRY, payload={})

    def test_unknown_error_kind_is_rejected(self):
        from tasks import TransitionPayloadError

        task = self.create_task()
        with self.assertRaises(TransitionPayloadError):
            self.repo.append_event(
                task.id, EVENT_API_ERROR, payload={"kind": "nope"}
            )
        self.assertEqual(len(self.repo.list_events(task.id)), 1)

    def test_state_changing_event_is_rejected(self):
        task = self.create_task()
        with self.assertRaises(ValueError):
            self.repo.append_event(task.id, EVENT_PLAN_CREATED, payload={})

    def test_append_event_unknown_task_raises(self):
        with self.assertRaises(TaskNotFoundError):
            self.repo.append_event(99999, EVENT_API_ERROR, payload={"kind": "provider_error"})


class TaskUsageTest(RepoTestCase):
    def test_usage_is_aggregated_from_events(self):
        task = self.create_task()
        self.repo.append_event(
            task.id,
            EVENT_API_ERROR,
            payload={
                "kind": "provider_error",
                "message": "boom",
                "attempts": 1,
                "finish_reason": "stop",
                "model": "deepseek-flash",
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                    "cost_usd": 0.001,
                },
            },
        )
        task = commit(
            self.repo,
            task,
            EVENT_PLAN_CREATED,
            {
                "plan": PLAN_TWO_STEPS,
                "attempts": 2,
                "usage": {
                    "prompt_tokens": 200,
                    "completion_tokens": 100,
                    "prompt_cache_hit_tokens": 20,
                    "prompt_cache_miss_tokens": 180,
                    "cost_usd": 0.002,
                },
            },
        )

        usage = self.repo.get_task_usage(task.id)
        self.assertIsInstance(usage, TaskUsage)
        self.assertEqual(usage.calls, 2)
        self.assertEqual(usage.attempts, 3)
        self.assertEqual(usage.input_tokens, 300)
        self.assertEqual(usage.output_tokens, 150)
        self.assertEqual(usage.total_tokens, 150)
        self.assertAlmostEqual(usage.cost_usd, 0.003)
        self.assertEqual(usage.cache_hit_tokens, 20)
        self.assertEqual(usage.cache_miss_tokens, 180)
        self.assertEqual(usage.finish_reasons, ("stop",))
        self.assertEqual(usage.models, ("deepseek-flash",))

    def test_usage_without_calls_is_empty(self):
        task = self.create_task()
        usage = self.repo.get_task_usage(task.id)
        self.assertEqual(usage.calls, 0)
        self.assertEqual(usage.attempts, 0)
        self.assertIsNone(usage.input_tokens)
        self.assertIsNone(usage.cost_usd)
        self.assertEqual(usage.finish_reasons, ())
        self.assertEqual(usage.models, ())


if __name__ == "__main__":
    unittest.main()
