"""Agent-level tests for user-profile payload insertion (Day 12).

All provider calls use the ``FakeClient`` from ``tests.test_agent``: there is no
network access and the real ``.env``/API key is never read. These tests pin the
Python payload assembly only; they do not verify the real provider contract.
"""

import os
import tempfile
import unittest

from agent import AgentConfig, ChatAgent
from context import (
    build_facts_payload,
    build_payload,
    build_sliding_payload,
)
from facts import FactOperation
from memory import (
    LONG_TERM_MEMORY_BLOCK_TITLE,
    MEMORY_SCOPE_LONG_TERM,
    MEMORY_SCOPE_WORKING,
    WORKING_MEMORY_BLOCK_TITLE,
    build_memory_blocks,
    insert_system_blocks,
)
from profile import PROFILE_BLOCK_TITLE, format_profile_block
from storage import ChatStore
from strategies import (
    STRATEGY_BRANCHING,
    STRATEGY_FACTS,
    STRATEGY_FULL,
    STRATEGY_SLIDING,
    STRATEGY_SUMMARY,
    summary_compression_enabled,
)
from tests.test_agent import (
    FakeClient,
    chunks_of,
    facts_json,
    make_usage,
    summary,
    turn,
)

_ALL_STRATEGIES = (
    STRATEGY_FULL,
    STRATEGY_SUMMARY,
    STRATEGY_SLIDING,
    STRATEGY_FACTS,
    STRATEGY_BRANCHING,
)

_PROFILE_NAME = "Payload test profile"
_PROFILE_FIELDS = dict(
    addressing="direct",
    style="technical",
    format="result first",
    constraints="no filler",
    domain_context="software",
)

_INVARIANTS = "Не раскрывать секреты"
_INVARIANTS_BLOCK = "Инварианты (соблюдай всегда):\n" + _INVARIANTS


class ProfileAgentTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)

    def _chat_with_memory(self, store, strategy, profile=True):
        """Create a chat with invariants and both memory layers, optionally a profile."""
        chat_id = store.create_chat(
            AgentConfig(context_strategy=strategy, invariants=_INVARIANTS)
        )
        store.add_memory_item(
            MEMORY_SCOPE_WORKING, "city", "Москва", chat_id=chat_id
        )
        store.add_memory_item(MEMORY_SCOPE_LONG_TERM, "name", "Алексей")
        if strategy == STRATEGY_FACTS:
            store.save_turn(chat_id, "q", "a")
            anchor = store.load_branch_history(chat_id)[-1].id
            store.save_facts(
                chat_id, [FactOperation("goal", "учить Python")], anchor
            )
        profile_id = None
        if profile:
            profile_id = store.create_profile(_PROFILE_NAME, **_PROFILE_FIELDS)
            store.set_active_profile(chat_id, profile_id)
        return chat_id, profile_id

    def _day11_payload(self, agent, message):
        """Rebuild the Day 11 payload: system, invariants, working, long-term."""
        config = agent.config
        strategy = config.context_strategy
        history = agent._history[1:]
        if strategy == STRATEGY_SLIDING:
            payload = build_sliding_payload(
                config.system_prompt, history, message, config.sliding_window_messages
            )
        elif strategy == STRATEGY_FACTS:
            payload = build_facts_payload(
                config.system_prompt,
                agent._facts,
                history,
                message,
                config.facts_window_messages,
            )
        elif strategy == STRATEGY_SUMMARY:
            payload = build_payload(
                config.system_prompt,
                history,
                message,
                agent._summary_content,
                agent._covered_messages_count,
                summary_compression_enabled(strategy, config.summarize),
            )
        else:
            payload = build_payload(
                config.system_prompt, history, message, summarize_enabled=False
            )
        blocks = []
        invariants = (config.invariants or "").strip()
        if invariants:
            blocks.append("Инварианты (соблюдай всегда):\n" + invariants)
        blocks.extend(
            build_memory_blocks(agent.working_memory, agent.long_term_memory)
        )
        return insert_system_blocks(payload, blocks)

    # --- Payload order and uniqueness ----------------------------------------

    def test_profile_block_is_first_for_every_strategy(self):
        for strategy in _ALL_STRATEGIES:
            with self.subTest(strategy=strategy):
                # Long-term memory is global, so each strategy gets a fresh DB.
                with tempfile.TemporaryDirectory() as tmp:
                    store = ChatStore(os.path.join(tmp, "test.db"))
                    chat_id, _ = self._chat_with_memory(store, strategy)
                    client = FakeClient(chunks=chunks_of(["ок"]))
                    agent = ChatAgent(client, store=store, chat_id=chat_id)
                    agent.ask("вопрос")

                    messages = client.payloads[0]["messages"]
                    expected_block = format_profile_block(
                        store.get_active_profile(chat_id)
                    )
                    self.assertEqual(messages[0]["role"], "system")
                    self.assertEqual(messages[1]["role"], "system")
                    self.assertEqual(messages[1]["content"], expected_block)
                    self.assertEqual(messages[2]["content"], _INVARIANTS_BLOCK)
                    self.assertEqual(
                        messages[3]["content"],
                        WORKING_MEMORY_BLOCK_TITLE + "\n- city: Москва",
                    )
                    self.assertEqual(
                        messages[4]["content"],
                        LONG_TERM_MEMORY_BLOCK_TITLE + "\n- name: Алексей",
                    )
                    # The profile block appears exactly once.
                    joined = "\n".join(m["content"] for m in messages)
                    self.assertEqual(joined.count(PROFILE_BLOCK_TITLE), 1)

    def test_no_profile_matches_day11_payload(self):
        for strategy in _ALL_STRATEGIES:
            with self.subTest(strategy=strategy):
                with tempfile.TemporaryDirectory() as tmp:
                    store = ChatStore(os.path.join(tmp, "test.db"))
                    chat_id, _ = self._chat_with_memory(store, strategy, profile=False)
                    agent = ChatAgent(FakeClient(), store=store, chat_id=chat_id)

                    actual = agent._build_payload("вопрос")
                    expected = self._day11_payload(agent, "вопрос")
                    self.assertEqual(actual, expected)
                    joined = "\n".join(m["content"] for m in actual)
                    self.assertNotIn(PROFILE_BLOCK_TITLE, joined)

    # --- Internal calls never carry the profile -------------------------------

    def test_summary_call_has_no_profile_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(
                AgentConfig(
                    context_strategy=STRATEGY_SUMMARY,
                    keep_recent_turns=3,
                    invariants=_INVARIANTS,
                )
            )
            profile_id = store.create_profile(_PROFILE_NAME, **_PROFILE_FIELDS)
            store.set_active_profile(chat_id, profile_id)
            client = FakeClient(
                script=[
                    turn("ответ 1"),
                    turn("ответ 2"),
                    turn("ответ 3"),
                    turn("ответ 4"),
                    summary("Сводка хода 1", usage=make_usage(100, 50)),
                ]
            )
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            for index in range(1, 5):
                agent.ask(f"вопрос {index}")

            # The main request carries the profile block...
            self.assertIn(
                PROFILE_BLOCK_TITLE, client.payloads[0]["messages"][1]["content"]
            )
            # ... while the summarisation call does not.
            summary_kwargs = client.payloads[4]
            self.assertFalse(summary_kwargs["stream"])
            joined = " ".join(m["content"] for m in summary_kwargs["messages"])
            self.assertNotIn(PROFILE_BLOCK_TITLE, joined)
            self.assertNotIn(_PROFILE_NAME, joined)

    def test_facts_call_has_no_profile_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(
                AgentConfig(context_strategy=STRATEGY_FACTS, invariants=_INVARIANTS)
            )
            store.save_turn(chat_id, "q", "a")
            profile_id = store.create_profile(_PROFILE_NAME, **_PROFILE_FIELDS)
            store.set_active_profile(chat_id, profile_id)
            client = FakeClient(
                script=[turn("ответ"), facts_json('{"facts": []}')]
            )
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            agent.ask("вопрос")

            self.assertEqual(client.calls, 2)
            main_joined = " ".join(
                m["content"] for m in client.payloads[0]["messages"]
            )
            self.assertIn(PROFILE_BLOCK_TITLE, main_joined)
            facts_joined = " ".join(
                m["content"] for m in client.payloads[1]["messages"]
            )
            self.assertNotIn(PROFILE_BLOCK_TITLE, facts_joined)
            self.assertNotIn(_PROFILE_NAME, facts_joined)

    # --- Lifecycle and failures ----------------------------------------------

    def test_reload_profile_picks_up_manual_assignment_without_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig(context_strategy=STRATEGY_FULL))
            client = FakeClient()
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            self.assertIsNone(agent.active_profile)

            profile_id = store.create_profile("A")
            store.set_active_profile(chat_id, profile_id)
            # The cached value is stale until an explicit reload.
            self.assertIsNone(agent.active_profile)
            agent.reload_profile()
            self.assertEqual(agent.active_profile.id, profile_id)
            self.assertEqual(client.calls, 0)

    def test_deleting_active_profile_drops_block_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(
                AgentConfig(context_strategy=STRATEGY_FULL, invariants=_INVARIANTS)
            )
            profile_id = store.create_profile(_PROFILE_NAME, **_PROFILE_FIELDS)
            store.set_active_profile(chat_id, profile_id)
            client = FakeClient(chunks=chunks_of(["ответ 1"]))
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            agent.ask("вопрос 1")
            self.assertIn(
                PROFILE_BLOCK_TITLE, client.payloads[0]["messages"][1]["content"]
            )

            # Deleting through the store must be enough: the next payload reads
            # the assignment live instead of trusting the cached profile.
            store.delete_profile(profile_id)
            history_before = len(agent.history)
            agent.ask("вопрос 2")

            joined = "\n".join(
                m["content"] for m in client.last_kwargs["messages"]
            )
            self.assertNotIn(PROFILE_BLOCK_TITLE, joined)
            self.assertIsNone(agent.active_profile)
            self.assertEqual(len(agent.history), history_before + 2)
            self.assertEqual(len(store.load_messages(chat_id)), 4)

    def test_api_error_keeps_history_memory_and_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig(context_strategy=STRATEGY_FULL))
            store.add_memory_item(
                MEMORY_SCOPE_WORKING, "k", "v", chat_id=chat_id
            )
            store.add_memory_item(MEMORY_SCOPE_LONG_TERM, "l", "v")
            profile_id = store.create_profile(_PROFILE_NAME, **_PROFILE_FIELDS)
            store.set_active_profile(chat_id, profile_id)
            client = FakeClient(error=RuntimeError("timeout"))
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            before = agent.history

            with self.assertRaises(RuntimeError):
                agent.ask("вопрос")

            # No partial answer is stored and nothing else was mutated.
            self.assertEqual(client.calls, 1)
            self.assertEqual(agent.history, before)
            self.assertEqual(len(agent.history), 1)
            self.assertEqual(agent.active_profile.id, profile_id)
            self.assertEqual(store.get_active_profile(chat_id).id, profile_id)
            self.assertEqual([item.value for item in agent.working_memory], ["v"])
            self.assertEqual([item.value for item in agent.long_term_memory], ["v"])
            self.assertEqual(store.load_messages(chat_id), [])


if __name__ == "__main__":
    unittest.main()
