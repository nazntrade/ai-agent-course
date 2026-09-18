"""Context tests for the Day 14 structural-invariant block.

The chat payload and the task packet both receive the applicable structural
invariants; without a repository (or without applicable rules) the payload is
byte-identical to the Day 10-13 output. No network and no real ``.env``.
"""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from agent import AgentConfig, ChatAgent
from invariant_storage import InvariantRepository
from invariants import (
    ENFORCEMENT_HARD,
    Invariant,
    SCOPE_TASK,
    STRUCTURAL_INVARIANTS_BLOCK_TITLE,
)
from profile import PROFILE_BLOCK_TITLE
from storage import ChatStore
from task_context import (
    BLOCK_INVARIANTS,
    BLOCK_PROFILE,
    BLOCK_STRUCTURAL_INVARIANTS,
    BLOCK_SYSTEM_PROMPT,
    build_context_packet,
)
from tasks import ACTION_RUN_PLANNING, STAGE_PLANNING, Task
from tests.test_agent import FakeClient

TASK_ID = 1


def make_task(**overrides):
    fields = dict(
        id=TASK_ID,
        chat_id=5,
        workflow_profile_id=1,
        workflow_name="default",
        title="Nightly report",
        goal="Build the nightly report",
        stage=STAGE_PLANNING,
        status="active",
        current_step="Planning",
        current_step_index=None,
        expected_action_type="run_planning",
        expected_action_text="Run planning",
        pause_reason="",
        version=2,
    )
    fields.update(overrides)
    return Task(**fields)


class ContextTestCase(unittest.TestCase):
    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ChatStore(os.path.join(self._tmp.name, "context.db"))
        self.chat_id = self.store.create_chat(AgentConfig())
        self.repo = InvariantRepository(self.store.db_path)

    def available(self):
        return self.repo.list_applicable(TASK_ID)


class ChatPayloadTest(ContextTestCase):
    def _agent(self, **kwargs):
        return ChatAgent(
            FakeClient(), store=self.store, chat_id=self.chat_id, **kwargs
        )

    def test_structural_block_is_sent_after_profile_and_free_text_invariants(self):
        config = self.store.load_config(self.chat_id)
        self.store.save_config(
            self.chat_id,
            AgentConfig(
                system_prompt=config["system_prompt"],
                invariants="Always answer in Russian",
            ),
        )
        agent = self._agent(
            invariants=self.repo,
            task_lookup=lambda chat_id: SimpleNamespace(id=TASK_ID),
        )
        payload = agent._build_payload("next question")
        contents = [message["content"] for message in payload]
        structural_index = next(
            index
            for index, text in enumerate(contents)
            if STRUCTURAL_INVARIANTS_BLOCK_TITLE in text
        )
        self.assertIn("INV-NO-FSM-BYPASS", contents[structural_index])
        self.assertIn("INV-ADV-CONFIRM-DESTRUCTIVE", contents[structural_index])
        invariants_index = next(
            index
            for index, text in enumerate(contents)
            if "Always answer in Russian" in text
        )
        self.assertLess(invariants_index, structural_index)
        self.assertTrue(all(message["role"] == "system" for message in payload[: structural_index + 1]))

    def test_no_repository_keeps_the_chat_payload_byte_identical(self):
        baseline_agent = self._agent()
        baseline = json.dumps(
            baseline_agent._build_payload("next question"), ensure_ascii=False
        )

        empty_path = os.path.join(self._tmp.name, "empty.db")
        empty_repo = InvariantRepository(empty_path, seed_defaults=False)
        agent = self._agent(
            invariants=empty_repo, task_lookup=lambda chat_id: None
        )
        self.assertEqual(
            json.dumps(agent._build_payload("next question"), ensure_ascii=False),
            baseline,
        )

    def test_task_scoped_rule_of_another_task_is_not_sent(self):
        self.repo.create_invariant(
            Invariant(
                code="INV-OTHER-TASK",
                title="Other task rule",
                text="Only for another task.",
                scope=SCOPE_TASK,
                task_id=999,
                enforcement=ENFORCEMENT_HARD,
                triggers=("other task phrase",),
            )
        )
        agent = self._agent(
            invariants=self.repo,
            task_lookup=lambda chat_id: SimpleNamespace(id=TASK_ID),
        )
        payload = agent._build_payload("next question")
        contents = "\n".join(message["content"] for message in payload)
        self.assertNotIn("INV-OTHER-TASK", contents)


class TaskPacketTest(ContextTestCase):
    def _packet(self, structural=()):
        return build_context_packet(
            stage=STAGE_PLANNING,
            action=ACTION_RUN_PLANNING,
            task=make_task(),
            invariants="Free text invariants",
            profile_block=PROFILE_BLOCK_TITLE + "\n- Profile: Engineer",
            structural_invariants=structural,
        )

    def test_structural_block_sits_between_invariants_and_profile(self):
        packet = self._packet(self.available())
        names = [block.name for block in packet.blocks]
        self.assertIn(BLOCK_STRUCTURAL_INVARIANTS, names)
        self.assertLess(
            names.index(BLOCK_INVARIANTS), names.index(BLOCK_STRUCTURAL_INVARIANTS)
        )
        self.assertLess(
            names.index(BLOCK_STRUCTURAL_INVARIANTS), names.index(BLOCK_PROFILE)
        )
        content = packet.block(BLOCK_STRUCTURAL_INVARIANTS).content
        self.assertIn("INV-NO-FSM-BYPASS", content)

    def test_no_applicable_rules_mean_no_block(self):
        packet = self._packet(())
        names = [block.name for block in packet.blocks]
        self.assertNotIn(BLOCK_STRUCTURAL_INVARIANTS, names)
        self.assertNotIn(STRUCTURAL_INVARIANTS_BLOCK_TITLE, "\n".join(
            message["content"] for message in packet.messages
        ))

    def test_repository_selection_matches_select_applicable(self):
        applicable = self.available()
        self.assertEqual(
            [rule.code for rule in applicable],
            ["INV-NO-DATA-RESET", "INV-NO-FSM-BYPASS", "INV-ADV-CONFIRM-DESTRUCTIVE"],
        )


if __name__ == "__main__":
    unittest.main()
