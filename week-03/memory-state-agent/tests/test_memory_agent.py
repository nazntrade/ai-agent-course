"""Agent-level tests for explicit memory payload insertion.

All provider calls use the ``FakeClient`` from ``tests.test_agent``; there is
no network access and the real ``.env``/API key is never read.
"""

import os
import tempfile
import unittest

from agent import AgentConfig, ChatAgent, ContextLimitError
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
)
from storage import ChatStore
from strategies import (
    STRATEGY_BRANCHING,
    STRATEGY_FACTS,
    STRATEGY_FULL,
    STRATEGY_SLIDING,
    STRATEGY_SUMMARY,
    summary_compression_enabled,
)
from tests.test_agent import FakeClient, chunks_of

_ALL_STRATEGIES = (
    STRATEGY_FULL,
    STRATEGY_SUMMARY,
    STRATEGY_SLIDING,
    STRATEGY_FACTS,
    STRATEGY_BRANCHING,
)

_INVARIANTS = "Не раскрывать секреты"
_INVARIANTS_BLOCK = "Инварианты (соблюдай всегда):\n" + _INVARIANTS


class MemoryAgentTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "test.db")
        self.store = ChatStore(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def _chat_with_memory(self, store, strategy):
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
        return chat_id

    def test_blocks_inserted_in_order_for_every_strategy(self):
        for strategy in _ALL_STRATEGIES:
            with self.subTest(strategy=strategy):
                # Long-term memory is global, so each strategy runs on its own
                # fresh database to stay independent.
                with tempfile.TemporaryDirectory() as tmp:
                    store = ChatStore(os.path.join(tmp, "test.db"))
                    chat_id = self._chat_with_memory(store, strategy)
                    client = FakeClient(chunks=chunks_of(["ок"]))
                    agent = ChatAgent(client, store=store, chat_id=chat_id)
                    agent.ask("вопрос")

                    # The first request is the main one; facts strategies add a
                    # second extraction call that must not be inspected here.
                    messages = client.payloads[0]["messages"]
                    self.assertEqual(messages[0]["role"], "system")
                    self.assertEqual(messages[1]["role"], "system")
                    self.assertEqual(messages[1]["content"], _INVARIANTS_BLOCK)
                    self.assertEqual(messages[2]["role"], "system")
                    self.assertEqual(
                        messages[2]["content"],
                        WORKING_MEMORY_BLOCK_TITLE + "\n- city: Москва",
                    )
                    self.assertEqual(messages[3]["role"], "system")
                    self.assertEqual(
                        messages[3]["content"],
                        LONG_TERM_MEMORY_BLOCK_TITLE + "\n- name: Алексей",
                    )

    def test_memory_precedes_facts_block(self):
        chat_id = self._chat_with_memory(self.store, STRATEGY_FACTS)
        client = FakeClient(chunks=chunks_of(["ок"]))
        agent = ChatAgent(client, store=self.store, chat_id=chat_id)
        agent.ask("вопрос")

        messages = client.payloads[0]["messages"]
        # system, invariants, working, long-term, facts block, window..., user
        self.assertTrue(messages[4]["content"].startswith("Известные факты"))
        self.assertIn("учить Python", messages[4]["content"])

    def test_excluded_items_never_reach_payload(self):
        chat_id = self.store.create_chat(AgentConfig(context_strategy=STRATEGY_FULL))
        self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "keep", "yes", chat_id=chat_id
        )
        excluded_id = self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "drop", "no", chat_id=chat_id
        )
        self.store.set_memory_item_included(
            MEMORY_SCOPE_WORKING, excluded_id, False, chat_id=chat_id
        )

        client = FakeClient(chunks=chunks_of(["ок"]))
        agent = ChatAgent(client, store=self.store, chat_id=chat_id)
        agent.ask("вопрос")

        joined = " ".join(m["content"] for m in client.last_kwargs["messages"])
        self.assertIn("- keep: yes", joined)
        self.assertNotIn("drop", joined)
        # The excluded entry stays visible to the agent/UI.
        self.assertEqual(len(agent.working_memory), 2)

    def test_empty_memory_and_invariants_match_day10_payload(self):
        for strategy in _ALL_STRATEGIES:
            with self.subTest(strategy=strategy):
                chat_id = self.store.create_chat(
                    AgentConfig(context_strategy=strategy)
                )
                agent = ChatAgent(FakeClient(), store=self.store, chat_id=chat_id)
                config = agent.config
                actual = agent._build_payload("вопрос")
                history = agent._history[1:]
                system = config.system_prompt

                if strategy == STRATEGY_SLIDING:
                    expected = build_sliding_payload(
                        system, history, "вопрос", config.sliding_window_messages
                    )
                elif strategy == STRATEGY_FACTS:
                    expected = build_facts_payload(
                        system, [], history, "вопрос", config.facts_window_messages
                    )
                elif strategy == STRATEGY_SUMMARY:
                    expected = build_payload(
                        system,
                        history,
                        "вопрос",
                        None,
                        0,
                        summary_compression_enabled(strategy, config.summarize),
                    )
                else:
                    expected = build_payload(
                        system, history, "вопрос", summarize_enabled=False
                    )

                self.assertEqual(actual, expected)
                # No extra system message was appended.
                self.assertEqual(len(actual), 2)

    def test_invariants_only_block_added_without_memory(self):
        chat_id = self.store.create_chat(
            AgentConfig(context_strategy=STRATEGY_FULL, invariants="Будь краток")
        )
        client = FakeClient(chunks=chunks_of(["ок"]))
        agent = ChatAgent(client, store=self.store, chat_id=chat_id)
        agent.ask("вопрос")
        messages = client.last_kwargs["messages"]
        self.assertEqual(messages[1]["content"], "Инварианты (соблюдай всегда):\nБудь краток")
        self.assertNotIn(WORKING_MEMORY_BLOCK_TITLE, messages[1]["content"])

    # --- Lifecycle ------------------------------------------------------------

    def test_memory_lifecycle_makes_no_api_calls(self):
        chat_id = self.store.create_chat(AgentConfig(context_strategy=STRATEGY_FULL))
        client = FakeClient()
        agent = ChatAgent(client, store=self.store, chat_id=chat_id)
        self.assertEqual(client.calls, 0)
        self.assertEqual(agent.working_memory, [])
        self.assertEqual(agent.long_term_memory, [])

        agent.set_config(AgentConfig(context_strategy=STRATEGY_FULL, system_prompt="new"))
        self.assertEqual(client.calls, 0)
        agent.reload_memory()
        self.assertEqual(client.calls, 0)

        self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "k", "v", chat_id=chat_id
        )
        # Nothing is picked up until the agent reloads the store state.
        self.assertEqual(agent.working_memory, [])
        agent.reload_memory()
        self.assertEqual([item.value for item in agent.working_memory], ["v"])
        self.assertEqual(client.calls, 0)

    def test_memory_properties_return_copies(self):
        chat_id = self.store.create_chat(AgentConfig(context_strategy=STRATEGY_FULL))
        self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "k", "v", chat_id=chat_id
        )
        agent = ChatAgent(FakeClient(), store=self.store, chat_id=chat_id)

        agent.working_memory.append("mutated")
        agent.long_term_memory.append("mutated")
        self.assertEqual([item.value for item in agent.working_memory], ["v"])
        self.assertEqual(agent.long_term_memory, [])

    def test_branch_switch_changes_working_set(self):
        chat_id = self.store.create_chat(
            AgentConfig(context_strategy=STRATEGY_BRANCHING)
        )
        self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "line", "main", chat_id=chat_id
        )
        self.store.save_turn(chat_id, "q", "a")
        checkpoint = self.store.load_branch_history(chat_id)[-1].id
        branch = self.store.create_branch(chat_id, "B", None, checkpoint)
        self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "line", "branch",
            chat_id=chat_id, branch_id=branch,
        )

        main_agent = ChatAgent(FakeClient(), store=self.store, chat_id=chat_id)
        self.assertEqual([item.value for item in main_agent.working_memory], ["main"])

        self.store.set_active_branch(chat_id, branch)
        branch_agent = ChatAgent(FakeClient(), store=self.store, chat_id=chat_id)
        self.assertEqual(
            [item.value for item in branch_agent.working_memory], ["branch"]
        )

    # --- Demo limit and failures ---------------------------------------------

    def test_demo_limit_accounts_for_memory_block(self):
        chat_id = self.store.create_chat(
            AgentConfig(context_strategy=STRATEGY_FULL, max_tokens=5)
        )
        client = FakeClient(chunks=chunks_of(["ок"]))
        agent = ChatAgent(client, store=self.store, chat_id=chat_id)
        base_est = agent._estimate_payload(agent._build_payload("вопрос"))

        long_id = self.store.add_memory_item(
            MEMORY_SCOPE_LONG_TERM, "note", "очень длинное значение " * 10
        )
        limit = base_est + 5
        agent.set_config(
            AgentConfig(
                context_strategy=STRATEGY_FULL,
                max_tokens=5,
                demo_context_limit=limit,
            )
        )

        with self.assertRaises(ContextLimitError) as ctx:
            agent.ask("вопрос")
        self.assertEqual(ctx.exception.limit_tokens, limit)
        self.assertEqual(client.calls, 0)
        self.assertEqual(len(agent.history), 1)

        # With the memory entry excluded the same limit fits.
        self.store.set_memory_item_included(
            MEMORY_SCOPE_LONG_TERM, long_id, False
        )
        agent.reload_memory()
        agent.ask("вопрос")
        self.assertEqual(client.calls, 1)

    def test_api_error_keeps_memory_unchanged(self):
        chat_id = self.store.create_chat(AgentConfig(context_strategy=STRATEGY_FULL))
        self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "k", "v", chat_id=chat_id
        )
        self.store.add_memory_item(MEMORY_SCOPE_LONG_TERM, "l", "v")
        client = FakeClient(error=RuntimeError("timeout"))
        agent = ChatAgent(client, store=self.store, chat_id=chat_id)

        with self.assertRaises(RuntimeError):
            agent.ask("вопрос")

        self.assertEqual([item.value for item in agent.working_memory], ["v"])
        self.assertEqual([item.value for item in agent.long_term_memory], ["v"])
        self.assertEqual(len(agent.history), 1)
        self.assertEqual(self.store.load_messages(chat_id), [])

    def test_empty_streamed_content_behaves_as_before(self):
        chat_id = self.store.create_chat(AgentConfig(context_strategy=STRATEGY_FULL))
        self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "k", "v", chat_id=chat_id
        )
        client = FakeClient(chunks=[])
        agent = ChatAgent(client, store=self.store, chat_id=chat_id)

        result = agent.ask("вопрос")

        self.assertEqual(result.text, "")
        self.assertEqual(client.calls, 1)
        self.assertEqual([item.value for item in agent.working_memory], ["v"])
        self.assertEqual(len(agent.history), 3)
        self.assertEqual(len(self.store.load_messages(chat_id)), 2)


if __name__ == "__main__":
    unittest.main()
