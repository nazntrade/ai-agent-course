"""Unit tests for the stage context packets of the Day 13 task state machine.

Everything here is pure: no network, no real ``.env`` and no provider call.
The only database use is a temporary SQLite file that verifies the chat
payload stays byte-identical and that building a task packet never touches the
chat state.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from agent import AgentConfig, ChatAgent
from facts import Fact, FACT_STATUS_ACTIVE
from memory import (
    INVARIANTS_BLOCK_TITLE,
    MEMORY_SCOPE_LONG_TERM,
    MEMORY_SCOPE_WORKING,
    MemoryItem,
)
from pricing import estimate_tokens_cost
from profile import PROFILE_BLOCK_TITLE
from strategies import STRATEGY_FACTS, STRATEGY_SLIDING, STRATEGY_SUMMARY
from storage import ChatStore
from task_context import (
    BLOCK_ACCEPTANCE_CRITERIA,
    BLOCK_ACTION,
    BLOCK_DEFECTS,
    BLOCK_EXECUTION_RESULTS,
    BLOCK_HISTORY,
    BLOCK_INVARIANTS,
    BLOCK_KNOWN_LIMITATIONS,
    BLOCK_LONG_TERM_MEMORY,
    BLOCK_PLAN,
    BLOCK_PROFILE,
    BLOCK_SPECIFICATION,
    BLOCK_SYSTEM_PROMPT,
    BLOCK_TASK_BRIEF,
    BLOCK_TASK_SNAPSHOT,
    BLOCK_WORKING_MEMORY,
    BLOCK_WORKFLOW,
    StageContextBuilder,
    build_context_packet,
    select_history,
)
from task_prompts import (
    TASK_EXECUTION_SYSTEM_PROMPT,
    TASK_PLANNING_SYSTEM_PROMPT,
    TASK_VALIDATION_SYSTEM_PROMPT,
)
from tasks import (
    ACTION_RUN_PLANNING,
    ACTION_RUN_STEP,
    ACTION_RUN_VALIDATION,
    ARTIFACT_EXECUTION_RESULT,
    ARTIFACT_PLAN,
    ARTIFACT_SPECIFICATION,
    ARTIFACT_TASK_BRIEF,
    ARTIFACT_VALIDATION_RESULT,
    EXPECTED_RUN_STEP,
    STAGE_EXECUTION,
    STAGE_PLANNING,
    STAGE_VALIDATION,
    STATUS_ACTIVE,
    Task,
    TaskArtifact,
    WorkflowProfile,
    compute_step_progress,
)
from tests.test_agent import FakeClient
from tokens import estimate_tokens

MODEL = "deepseek-flash"
DT = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)

PLAN = {
    "summary": "Prepare the nightly report",
    "acceptance_criteria": ["The report exists", "The totals add up"],
    "steps": [
        {"index": 1, "title": "Collect data", "description": "Gather the numbers"},
        {"index": 2, "title": "Format report", "description": "Write the report"},
    ],
}

HISTORY = [
    {"role": "user", "content": "first question"},
    {"role": "assistant", "content": "first answer"},
    {"role": "user", "content": "second question"},
    {"role": "assistant", "content": "second answer"},
    {"role": "user", "content": "third question"},
    {"role": "assistant", "content": "third answer"},
]


def make_task(**overrides):
    """A planning task by default, overridable per test."""
    fields = dict(
        id=1,
        chat_id=5,
        workflow_profile_id=1,
        workflow_name="default",
        title="Nightly report",
        goal="Build the nightly report",
        stage=STAGE_PLANNING,
        status=STATUS_ACTIVE,
        current_step="Planning",
        current_step_index=None,
        expected_action_type="run_planning",
        expected_action_text="Run planning",
        pause_reason="",
        version=2,
    )
    fields.update(overrides)
    return Task(**fields)


def make_artifact(kind, content, *, artifact_id, revision=1, stage=STAGE_PLANNING):
    return TaskArtifact(
        id=artifact_id,
        task_id=1,
        stage=stage,
        kind=kind,
        revision=revision,
        content=content,
    )


def planning_artifacts():
    return [
        make_artifact(
            ARTIFACT_TASK_BRIEF,
            {"text": "Keep it short and use the finance numbers"},
            artifact_id=1,
        ),
    ]


def execution_artifacts(*, with_rework=True):
    artifacts = [
        make_artifact(
            ARTIFACT_SPECIFICATION,
            {"markdown": "# Task specification\n\nThe specification body"},
            artifact_id=1,
        ),
        make_artifact(ARTIFACT_PLAN, PLAN, artifact_id=2),
        make_artifact(
            ARTIFACT_EXECUTION_RESULT,
            {"step_index": 1, "round": 1, "text": "STEP-ONE-R1"},
            artifact_id=10,
            stage=STAGE_EXECUTION,
        ),
    ]
    if with_rework:
        artifacts.append(
            make_artifact(
                ARTIFACT_VALIDATION_RESULT,
                {
                    "passed": False,
                    "defects": [
                        {"step_index": 1, "description": "The totals are wrong"}
                    ],
                    "notes": "Rework step 1",
                },
                artifact_id=20,
                stage=STAGE_VALIDATION,
            )
        )
    return artifacts


def execution_progress(artifacts):
    return compute_step_progress(PLAN, artifacts)


def validation_artifacts():
    return [
        make_artifact(
            ARTIFACT_SPECIFICATION,
            {"markdown": "# Task specification\n\nThe specification body"},
            artifact_id=1,
        ),
        make_artifact(ARTIFACT_PLAN, PLAN, artifact_id=2),
        make_artifact(
            ARTIFACT_EXECUTION_RESULT,
            {"step_index": 1, "round": 1, "text": "STEP-ONE-R1"},
            artifact_id=10,
            stage=STAGE_EXECUTION,
        ),
        make_artifact(
            ARTIFACT_EXECUTION_RESULT,
            {"step_index": 1, "round": 2, "text": "STEP-ONE-R2"},
            artifact_id=30,
            stage=STAGE_EXECUTION,
        ),
        make_artifact(
            ARTIFACT_EXECUTION_RESULT,
            {"step_index": 2, "round": 1, "text": "STEP-TWO-R1"},
            artifact_id=15,
            stage=STAGE_EXECUTION,
        ),
        make_artifact(
            ARTIFACT_VALIDATION_RESULT,
            {
                "passed": False,
                "defects": [
                    {"step_index": 2, "description": "The grand total is missing"}
                ],
                "notes": "Partial data only",
            },
            artifact_id=40,
            revision=2,
            stage=STAGE_VALIDATION,
        ),
    ]


def packet_for(stage, action, *, task=None, artifacts=(), progress=(), **overrides):
    arguments = dict(
        stage=stage,
        action=action,
        task=task if task is not None else make_task(),
        workflow=None,
        plan=None,
        artifacts=artifacts,
        progress=progress,
        system_prompt="You are a helpful assistant.",
        invariants="Always answer in Russian",
        profile_block=None,
        working_items=(),
        long_term_items=(),
        history_messages=HISTORY,
        summary_content=None,
        covered_messages_count=0,
        strategy=STRATEGY_SUMMARY,
        sliding_window_messages=6,
        facts_window_messages=6,
        facts=(),
        model=MODEL,
        dt=DT,
    )
    arguments.update(overrides)
    return build_context_packet(**arguments)


def block_names(packet):
    return [block.name for block in packet.blocks]


class BlockOrderTest(unittest.TestCase):
    def test_planning_block_order_and_role_of_the_action(self):
        packet = packet_for(
            STAGE_PLANNING,
            ACTION_RUN_PLANNING,
            artifacts=planning_artifacts(),
            profile_block=PROFILE_BLOCK_TITLE + "\n- Профиль: Engineer",
            working_items=[MemoryItem(id=1, key="focus", value="reporting")],
            long_term_items=[MemoryItem(id=2, key="language", value="Russian")],
        )

        self.assertEqual(
            block_names(packet),
            [
                BLOCK_SYSTEM_PROMPT,
                BLOCK_INVARIANTS,
                BLOCK_PROFILE,
                BLOCK_WORKFLOW,
                BLOCK_TASK_SNAPSHOT,
                BLOCK_TASK_BRIEF,
                BLOCK_WORKING_MEMORY,
                BLOCK_LONG_TERM_MEMORY,
                BLOCK_HISTORY,
                BLOCK_ACTION,
            ],
        )
        # Every conceptual block before the history is a system message; the
        # history keeps the roles of the strategy slice and the action is the
        # final user message.
        system_names = block_names(packet)[:6]
        for name in system_names + [BLOCK_WORKING_MEMORY, BLOCK_LONG_TERM_MEMORY]:
            self.assertEqual(packet.block(name).role, "system")
        self.assertEqual(packet.block(BLOCK_HISTORY).role, "history")
        self.assertEqual(packet.block(BLOCK_ACTION).role, "user")
        self.assertEqual(packet.messages[-1]["role"], "user")

    def test_workflow_instructions_precede_the_stage_prompt(self):
        workflow = WorkflowProfile(
            id=1,
            name="default",
            display_name="Default workflow",
            instructions={"planning": "Follow the company planning rules."},
        )
        packet = packet_for(
            STAGE_PLANNING,
            ACTION_RUN_PLANNING,
            workflow=workflow,
            artifacts=planning_artifacts(),
        )
        content = packet.block(BLOCK_WORKFLOW).content
        self.assertIn("Follow the company planning rules.", content)
        self.assertIn(TASK_PLANNING_SYSTEM_PROMPT, content)
        self.assertLess(
            content.index("Follow the company planning rules."),
            content.index(TASK_PLANNING_SYSTEM_PROMPT),
        )

    def test_invariants_are_placed_before_the_profile(self):
        packet = packet_for(
            STAGE_PLANNING,
            ACTION_RUN_PLANNING,
            artifacts=planning_artifacts(),
            profile_block=PROFILE_BLOCK_TITLE + "\n- Профиль: Engineer",
        )
        names = block_names(packet)
        self.assertLess(names.index(BLOCK_INVARIANTS), names.index(BLOCK_PROFILE))
        self.assertIn(INVARIANTS_BLOCK_TITLE, packet.block(BLOCK_INVARIANTS).content)
        self.assertIn(PROFILE_BLOCK_TITLE, packet.block(BLOCK_PROFILE).content)

    def test_empty_blocks_are_skipped(self):
        packet = packet_for(
            STAGE_PLANNING,
            ACTION_RUN_PLANNING,
            task=make_task(),
            artifacts=(),
            system_prompt="",
            invariants="",
            profile_block=None,
            working_items=(),
            long_term_items=(),
            history_messages=(),
        )
        names = block_names(packet)
        self.assertNotIn(BLOCK_SYSTEM_PROMPT, names)
        self.assertNotIn(BLOCK_INVARIANTS, names)
        self.assertNotIn(BLOCK_PROFILE, names)
        self.assertNotIn(BLOCK_TASK_BRIEF, names)
        self.assertNotIn(BLOCK_WORKING_MEMORY, names)
        self.assertNotIn(BLOCK_LONG_TERM_MEMORY, names)
        self.assertNotIn(BLOCK_HISTORY, names)
        # The stage instruction, the snapshot and the action are always there.
        self.assertEqual(
            names,
            [BLOCK_WORKFLOW, BLOCK_TASK_SNAPSHOT, BLOCK_ACTION],
        )
        self.assertFalse(any(not block.content.strip() for block in packet.blocks))
        self.assertFalse(any(not message["content"].strip() for message in packet.messages))

    def test_action_message_is_the_synthetic_stage_message(self):
        packet = packet_for(
            STAGE_PLANNING,
            ACTION_RUN_PLANNING,
            artifacts=planning_artifacts(),
        )
        action = packet.messages[-1]["content"]
        self.assertIn("run_planning", action)
        self.assertIn("Build the nightly report", action)
        self.assertIn("Keep it short and use the finance numbers", action)

    def test_custom_action_message_replaces_the_default(self):
        packet = packet_for(
            STAGE_PLANNING,
            ACTION_RUN_PLANNING,
            artifacts=planning_artifacts(),
            action_message="CUSTOM ACTION",
        )
        self.assertEqual(packet.messages[-1]["content"], "CUSTOM ACTION")

    def test_unknown_action_yields_no_action_block(self):
        packet = packet_for(STAGE_PLANNING, "none")
        self.assertNotIn(BLOCK_ACTION, block_names(packet))


class ArtifactSelectionTest(unittest.TestCase):
    def test_execution_packet_has_specification_plan_and_defects(self):
        artifacts = execution_artifacts()
        packet = packet_for(
            STAGE_EXECUTION,
            ACTION_RUN_STEP,
            task=make_task(
                stage=STAGE_EXECUTION,
                current_step="Collect data",
                current_step_index=1,
                expected_action_type=EXPECTED_RUN_STEP,
                expected_action_text="Run the current step",
            ),
            plan=PLAN,
            artifacts=artifacts,
            progress=execution_progress(artifacts),
        )
        names = block_names(packet)
        self.assertIn(BLOCK_SPECIFICATION, names)
        self.assertIn(BLOCK_PLAN, names)
        self.assertIn(BLOCK_DEFECTS, names)
        self.assertLess(names.index(BLOCK_SPECIFICATION), names.index(BLOCK_PLAN))
        self.assertLess(names.index(BLOCK_PLAN), names.index(BLOCK_DEFECTS))
        self.assertIn("The specification body", packet.block(BLOCK_SPECIFICATION).content)
        self.assertIn("Collect data", packet.block(BLOCK_PLAN).content)
        self.assertIn("The totals are wrong", packet.block(BLOCK_DEFECTS).content)
        self.assertIn(TASK_EXECUTION_SYSTEM_PROMPT, packet.block(BLOCK_WORKFLOW).content)
        self.assertIn("Collect data", packet.messages[-1]["content"])
        self.assertIn("The totals are wrong", packet.messages[-1]["content"])

    def test_execution_without_rework_has_no_defects_block(self):
        artifacts = execution_artifacts(with_rework=False)
        packet = packet_for(
            STAGE_EXECUTION,
            ACTION_RUN_STEP,
            task=make_task(
                stage=STAGE_EXECUTION,
                current_step="Collect data",
                current_step_index=1,
                expected_action_type=EXPECTED_RUN_STEP,
            ),
            plan=PLAN,
            artifacts=artifacts,
            progress=execution_progress(artifacts),
        )
        self.assertNotIn(BLOCK_DEFECTS, block_names(packet))

    def test_validation_packet_uses_the_latest_revision_of_every_artifact(self):
        artifacts = validation_artifacts()
        packet = packet_for(
            STAGE_VALIDATION,
            ACTION_RUN_VALIDATION,
            task=make_task(
                stage=STAGE_VALIDATION,
                current_step="Validation",
                current_step_index=None,
                expected_action_type="run_validation",
            ),
            plan=PLAN,
            artifacts=artifacts,
            progress=compute_step_progress(PLAN, artifacts),
        )
        names = block_names(packet)
        self.assertIn(BLOCK_SPECIFICATION, names)
        self.assertIn(BLOCK_PLAN, names)
        self.assertIn(BLOCK_ACCEPTANCE_CRITERIA, names)
        self.assertIn(BLOCK_EXECUTION_RESULTS, names)
        self.assertIn(BLOCK_KNOWN_LIMITATIONS, names)

        plan_content = packet.block(BLOCK_PLAN).content
        self.assertIn("Collect data", plan_content)
        self.assertNotIn("Критерии приёмки:", plan_content)

        criteria = packet.block(BLOCK_ACCEPTANCE_CRITERIA).content
        self.assertIn("The totals add up", criteria)

        results = packet.block(BLOCK_EXECUTION_RESULTS).content
        self.assertIn("STEP-ONE-R2", results)
        self.assertNotIn("STEP-ONE-R1", results)
        self.assertIn("STEP-TWO-R1", results)
        self.assertLess(results.index("STEP-ONE-R2"), results.index("STEP-TWO-R1"))

        limitations = packet.block(BLOCK_KNOWN_LIMITATIONS).content
        self.assertIn("Partial data only", limitations)
        self.assertIn("The grand total is missing", limitations)

        action = packet.messages[-1]["content"]
        self.assertIn(TASK_VALIDATION_SYSTEM_PROMPT, packet.block(BLOCK_WORKFLOW).content)
        self.assertIn("The totals add up", action)
        # The step results stay in their artifact block; the action message no
        # longer duplicates them.
        self.assertNotIn("STEP-ONE-R2", action)

    def test_latest_plan_revision_wins(self):
        artifacts = [
            make_artifact(ARTIFACT_PLAN, PLAN, artifact_id=1, revision=1),
            make_artifact(
                ARTIFACT_PLAN,
                {**PLAN, "summary": "REVISED SUMMARY"},
                artifact_id=2,
                revision=2,
            ),
        ]
        packet = packet_for(
            STAGE_EXECUTION,
            ACTION_RUN_STEP,
            task=make_task(
                stage=STAGE_EXECUTION,
                current_step="Collect data",
                current_step_index=1,
                expected_action_type=EXPECTED_RUN_STEP,
            ),
            plan=artifacts[-1].content,
            artifacts=artifacts,
        )
        content = packet.block(BLOCK_PLAN).content
        self.assertIn("REVISED SUMMARY", content)
        self.assertNotIn("Prepare the nightly report", content)

    def test_planning_packet_has_only_the_task_brief(self):
        artifacts = planning_artifacts() + [
            make_artifact(ARTIFACT_SPECIFICATION, {"markdown": "# spec"}, artifact_id=5)
        ]
        packet = packet_for(
            STAGE_PLANNING, ACTION_RUN_PLANNING, artifacts=artifacts
        )
        names = block_names(packet)
        self.assertIn(BLOCK_TASK_BRIEF, names)
        self.assertNotIn(BLOCK_SPECIFICATION, names)
        self.assertNotIn(BLOCK_PLAN, names)


class HistorySelectionTest(unittest.TestCase):
    def test_sliding_window_limits_the_history(self):
        selected = select_history(
            HISTORY, strategy=STRATEGY_SLIDING, sliding_window_messages=3
        )
        self.assertEqual(len(selected), 2)
        self.assertEqual(
            [message["content"] for message in selected],
            ["third question", "third answer"],
        )

    def test_sliding_window_packet_history_block_is_limited(self):
        packet = packet_for(
            STAGE_PLANNING,
            ACTION_RUN_PLANNING,
            artifacts=planning_artifacts(),
            strategy=STRATEGY_SLIDING,
            sliding_window_messages=3,
        )
        names = block_names(packet)
        self.assertIn(BLOCK_HISTORY, names)
        history_index = names.index(BLOCK_HISTORY)
        history_messages = packet.messages[history_index:-1]
        self.assertEqual(len(history_messages), 2)
        self.assertEqual(history_messages[0]["content"], "third question")
        self.assertEqual(packet.messages[-1]["role"], "user")

    def test_summary_strategy_replaces_covered_messages(self):
        selected = select_history(
            HISTORY,
            strategy=STRATEGY_SUMMARY,
            summary_content="earlier compressed talk",
            covered_messages_count=4,
        )
        self.assertEqual(selected[0]["role"], "system")
        self.assertIn("earlier compressed talk", selected[0]["content"])
        self.assertEqual(
            [message["content"] for message in selected[1:]],
            ["third question", "third answer"],
        )

    def test_facts_strategy_adds_the_facts_block(self):
        facts = [
            Fact(
                id=1,
                category="preference",
                key="language",
                value="Russian",
                status=FACT_STATUS_ACTIVE,
            )
        ]
        selected = select_history(HISTORY, strategy=STRATEGY_FACTS, facts=facts)
        self.assertEqual(selected[0]["role"], "system")
        self.assertIn("Russian", selected[0]["content"])

    def test_full_history_is_only_sent_for_the_full_strategy(self):
        selected = select_history(HISTORY, strategy="full")
        self.assertEqual(len(selected), len(HISTORY))


class DiagnosticsEstimateTest(unittest.TestCase):
    def test_tokens_and_cost_are_estimates_per_block(self):
        packet = packet_for(
            STAGE_PLANNING,
            ACTION_RUN_PLANNING,
            artifacts=planning_artifacts(),
            profile_block=PROFILE_BLOCK_TITLE + "\n- Профиль: Engineer",
            working_items=[MemoryItem(id=1, key="focus", value="reporting")],
        )
        self.assertEqual(
            packet.total_tokens, sum(block.tokens for block in packet.blocks)
        )
        for block in packet.blocks:
            expected = estimate_tokens_cost(MODEL, block.tokens, DT)
            self.assertAlmostEqual(block.cost_usd, expected, places=12)
        self.assertAlmostEqual(
            packet.total_cost_usd,
            sum(block.cost_usd for block in packet.blocks),
            places=12,
        )
        self.assertGreater(packet.total_tokens, 0)

    def test_unknown_model_has_no_cost(self):
        packet = packet_for(
            STAGE_PLANNING,
            ACTION_RUN_PLANNING,
            model="custom-model",
            artifacts=planning_artifacts(),
        )
        self.assertIsNone(packet.total_cost_usd)
        self.assertTrue(all(block.cost_usd is None for block in packet.blocks))

    def test_history_block_tokens_match_the_selected_messages(self):
        packet = packet_for(
            STAGE_PLANNING,
            ACTION_RUN_PLANNING,
            strategy=STRATEGY_SLIDING,
            sliding_window_messages=3,
        )
        expected = sum(
            estimate_tokens(message["content"]) for message in HISTORY[-2:]
        )
        self.assertEqual(packet.block(BLOCK_HISTORY).tokens, expected)


class MemoryIsolationTest(unittest.TestCase):
    def test_service_blocks_never_leak_into_memory(self):
        packet = packet_for(
            STAGE_PLANNING,
            ACTION_RUN_PLANNING,
            artifacts=planning_artifacts(),
            profile_block=PROFILE_BLOCK_TITLE + "\n- Профиль: Engineer",
            working_items=[MemoryItem(id=1, key="focus", value="reporting")],
            long_term_items=[MemoryItem(id=2, key="language", value="Russian")],
        )
        working = packet.block(BLOCK_WORKING_MEMORY).content
        long_term = packet.block(BLOCK_LONG_TERM_MEMORY).content
        self.assertIn("focus: reporting", working)
        self.assertIn("language: Russian", long_term)
        for forbidden in (
            "You are a helpful assistant.",
            INVARIANTS_BLOCK_TITLE,
            PROFILE_BLOCK_TITLE,
            TASK_PLANNING_SYSTEM_PROMPT,
        ):
            self.assertNotIn(forbidden, working)
            self.assertNotIn(forbidden, long_term)
        self.assertNotIn("- Профиль: Engineer", working)
        self.assertNotIn("- Профиль: Engineer", long_term)

    def test_excluded_memory_items_are_not_sent(self):
        packet = packet_for(
            STAGE_PLANNING,
            ACTION_RUN_PLANNING,
            artifacts=planning_artifacts(),
            working_items=[MemoryItem(id=1, key="hidden", value="x", included=False)],
        )
        self.assertNotIn(BLOCK_WORKING_MEMORY, block_names(packet))


class ChatPayloadRegressionTest(unittest.TestCase):
    """FR-28/AC-35: the Day 10-12 chat payload must stay byte-identical."""

    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ChatStore(os.path.join(self._tmp.name, "test.db"))
        config = AgentConfig(
            system_prompt="System prompt",
            context_strategy=STRATEGY_SUMMARY,
            invariants="Always answer in Russian",
        )
        self.chat_id = self.store.create_chat(config)
        profile_id = self.store.create_profile(
            "Regression profile", style="Short", constraints="No filler"
        )
        self.store.set_active_profile(self.chat_id, profile_id)
        self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "focus", "reporting", chat_id=self.chat_id
        )
        self.store.add_memory_item(MEMORY_SCOPE_LONG_TERM, "language", "Russian")

    def test_building_a_task_packet_does_not_change_the_chat_payload(self):
        agent = ChatAgent(None, store=self.store, chat_id=self.chat_id)
        before = agent._build_payload("next question")
        before_json = json.dumps(before, ensure_ascii=False)
        history_before = agent.history

        StageContextBuilder().build_context_packet(
            stage=STAGE_PLANNING,
            action=ACTION_RUN_PLANNING,
            task=make_task(chat_id=self.chat_id),
            plan=PLAN,
            artifacts=planning_artifacts(),
            system_prompt=agent.config.system_prompt,
            invariants=agent.config.invariants,
            profile_block=agent.active_profile,
            working_items=agent.working_memory,
            long_term_items=agent.long_term_memory,
            history_messages=agent.history[1:],
            strategy=agent.config.context_strategy,
            model=agent.config.model,
            dt=DT,
        )

        after = agent._build_payload("next question")
        self.assertEqual(json.dumps(after, ensure_ascii=False), before_json)
        self.assertEqual(agent.history, history_before)

    def test_chat_payload_keeps_profile_before_invariants(self):
        agent = ChatAgent(None, store=self.store, chat_id=self.chat_id)
        payload = agent._build_payload("question")
        contents = [message["content"] for message in payload]
        profile_index = next(
            index for index, text in enumerate(contents) if PROFILE_BLOCK_TITLE in text
        )
        invariants_index = next(
            index for index, text in enumerate(contents) if INVARIANTS_BLOCK_TITLE in text
        )
        self.assertLess(profile_index, invariants_index)
        self.assertEqual(
            contents[invariants_index],
            INVARIANTS_BLOCK_TITLE + "\nAlways answer in Russian",
        )

    def test_building_a_packet_issues_no_provider_call(self):
        client = FakeClient()
        agent = ChatAgent(client, store=self.store, chat_id=self.chat_id)
        StageContextBuilder().build_context_packet(
            stage=STAGE_VALIDATION,
            action=ACTION_RUN_VALIDATION,
            task=make_task(
                chat_id=self.chat_id,
                stage=STAGE_VALIDATION,
                current_step="Validation",
                expected_action_type="run_validation",
            ),
            plan=PLAN,
            artifacts=validation_artifacts(),
            history_messages=agent.history[1:],
            system_prompt=agent.config.system_prompt,
            model=agent.config.model,
            dt=DT,
        )
        self.assertEqual(client.calls, 0)
        self.assertEqual(len(agent.history), 1)


if __name__ == "__main__":
    unittest.main()
