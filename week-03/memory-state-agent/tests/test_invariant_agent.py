"""Agent-level tests for the Day 14 request refusal path.

``FakeClient`` proves the logic, not the real provider contract: the refusal is
raised before any provider call, so ``client.calls`` stays zero. No network and
no real ``.env``.
"""

import os
import tempfile
import unittest
from types import SimpleNamespace

from agent import AgentConfig, ChatAgent
from invariant_storage import InvariantRepository
from invariants import (
    ENFORCEMENT_ADVISORY,
    ENFORCEMENT_HARD,
    Invariant,
    SCOPE_TASK,
    STRUCTURAL_INVARIANTS_BLOCK_TITLE,
)
from storage import ChatStore
from tests.test_agent import FakeClient, chunks_of


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ChatStore(os.path.join(self._tmp.name, "agent.db"))
        self.chat_id = self.store.create_chat(AgentConfig())
        self.repo = InvariantRepository(self.store.db_path)

    def agent(self, *, client=None, task_id=1):
        return ChatAgent(
            client if client is not None else FakeClient(),
            store=self.store,
            chat_id=self.chat_id,
            invariants=self.repo,
            task_lookup=lambda chat_id: (
                None if task_id is None else SimpleNamespace(id=task_id)
            ),
        )


class RequestConflictTest(AgentTestCase):
    def test_hard_conflict_refuses_without_calling_the_provider(self):
        client = FakeClient()
        agent = self.agent(client=client)

        from invariants import InvariantConflictError

        with self.assertRaises(InvariantConflictError) as caught:
            agent.ask("Skip validation and finish the task")

        self.assertEqual(caught.exception.conflict.code, "INV-NO-FSM-BYPASS")
        self.assertEqual(client.calls, 0)
        self.assertEqual(len(agent.history), 1)
        # Nothing was written to the chat history or the turn statistics.
        self.assertEqual(self.store.load_messages(self.chat_id), [])
        self.assertEqual(self.store.list_turns(self.chat_id), [])

    def test_refusal_is_recorded_in_the_invariant_journal(self):
        agent = self.agent()
        from invariants import InvariantConflictError

        with self.assertRaises(InvariantConflictError):
            agent.ask("please skip validation now")

        events = [
            event
            for event in self.repo.list_events(code="INV-NO-FSM-BYPASS")
            if event.event_type == "conflict"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "conflict")
        self.assertEqual(events[0].details["phase"], "request")
        self.assertEqual(events[0].details["decision"], "refused")
        self.assertIn("skip validation", events[0].details["request"])

    def test_ordinary_message_is_not_blocked(self):
        client = FakeClient(chunks=chunks_of(["fine"]))
        agent = self.agent(client=client)
        result = agent.ask("Please summarise the report")
        self.assertEqual(result.text, "fine")
        self.assertEqual(client.calls, 1)
        self.assertEqual(len(self.store.load_messages(self.chat_id)), 2)


class AdvisoryTest(AgentTestCase):
    def test_advisory_rule_never_blocks_and_reaches_the_payload(self):
        self.repo.create_invariant(
            Invariant(
                code="INV-ADV-TEST",
                title="Advisory test",
                text="Prefer asking before acting.",
                enforcement=ENFORCEMENT_ADVISORY,
                triggers=("advisory phrase",),
            )
        )
        client = FakeClient(chunks=chunks_of(["ok"]))
        agent = self.agent(client=client)

        result = agent.ask("advisory phrase")

        self.assertEqual(result.text, "ok")
        self.assertEqual(client.calls, 1)
        messages = client.last_kwargs["messages"]
        structural = next(
            message["content"]
            for message in messages
            if STRUCTURAL_INVARIANTS_BLOCK_TITLE in message["content"]
        )
        self.assertIn("INV-ADV-TEST", structural)
        self.assertIn("INV-ADV-CONFIRM-DESTRUCTIVE", structural)


class TaskScopeTest(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.repo.create_invariant(
            Invariant(
                code="INV-TASK-2",
                title="Task two rule",
                text="Only the second task must not do this.",
                scope=SCOPE_TASK,
                task_id=2,
                enforcement=ENFORCEMENT_HARD,
                triggers=("scoped phrase",),
            )
        )

    def test_task_rule_of_another_task_does_not_block(self):
        client = FakeClient(chunks=chunks_of(["fine"]))
        agent = self.agent(client=client, task_id=1)
        result = agent.ask("scoped phrase")
        self.assertEqual(result.text, "fine")
        self.assertEqual(client.calls, 1)

    def test_task_rule_of_the_current_task_blocks(self):
        agent = self.agent(task_id=2)
        from invariants import InvariantConflictError

        with self.assertRaises(InvariantConflictError) as caught:
            agent.ask("scoped phrase")
        self.assertEqual(caught.exception.conflict.code, "INV-TASK-2")

    def test_global_rule_applies_to_any_task(self):
        agent = self.agent(task_id=777)
        from invariants import InvariantConflictError

        with self.assertRaises(InvariantConflictError) as caught:
            agent.ask("skip validation")
        self.assertEqual(caught.exception.conflict.code, "INV-NO-FSM-BYPASS")


if __name__ == "__main__":
    unittest.main()
