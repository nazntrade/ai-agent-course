import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from dataclasses import asdict
from types import SimpleNamespace

from agent import (
    AgentConfig,
    ApiContextOverflowError,
    ChatAgent,
    ContextLimitError,
)
from context import (
    SUMMARY_MAX_TOKENS,
    SUMMARY_RETRY_MAX_TOKENS,
    SUMMARY_TEMPERATURE,
    build_payload,
)
from stats import TurnStats
from storage import ChatStore
from tokens import estimate_tokens


def make_chunk(content, finish_reason=None):
    """Build a fake stream chunk: chunk.choices[0].delta.content."""
    delta = SimpleNamespace(content=content)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


def chunks_of(contents):
    """Return a list of chunks for a list of text fragments."""
    return [make_chunk(text) for text in contents]


def make_usage(prompt_tokens, completion_tokens):
    """Build a CompletionUsage-like object for stream/non-stream usage."""
    return SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)


def make_full_usage(prompt, completion, total=None, hit=None, miss=None):
    """Build a usage object with optional cache/total fields."""
    usage = make_usage(prompt, completion)
    if total is not None:
        usage.total_tokens = total
    if hit is not None:
        usage.prompt_cache_hit_tokens = hit
    if miss is not None:
        usage.prompt_cache_miss_tokens = miss
    return usage


def make_response(content, usage=None, finish_reason=None):
    """Build a non-stream response: choices[0].message.content (+ optional usage)."""
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    response = SimpleNamespace(choices=[choice])
    if usage is not None:
        response.usage = usage
    return response


def turn(text):
    """A script step producing a streamed turn with the given answer text."""
    return lambda kwargs: chunks_of([text])


def summary(text, usage=None, finish_reason=None):
    """A script step producing a non-stream summary response."""
    return lambda kwargs: make_response(text, usage=usage, finish_reason=finish_reason)


def raises(exc):
    """A script step that raises ``exc``."""

    def step(kwargs):
        raise exc

    return step


def reasoning_summary(text, required_tokens, usage=None):
    """A script step emulating a reasoning model's summary output budget.

    Whenever the received ``max_tokens`` budget is below ``required_tokens``,
    it returns a truncated response (``finish_reason="length"`` with
    ``completion_tokens == max_tokens`` and some ``reasoning_tokens``), which
    mirrors a reasoning model spending the budget on hidden reasoning. With a
    sufficient budget it returns the final ``text`` with ``finish_reason="stop"``.
    """

    def step(kwargs):
        budget = kwargs.get("max_tokens") or 0
        if budget < required_tokens:
            truncated_usage = make_usage(budget, budget)
            truncated_usage.reasoning_tokens = max(budget // 2, 1)
            return make_response(
                "обрезанная сводка",
                usage=truncated_usage,
                finish_reason="length",
            )
        return make_response(text, usage=usage, finish_reason="stop")

    return step


class FakeContextOverflowError(Exception):
    def __init__(self):
        self.status_code = 400
        super().__init__("This model's maximum context length is 8192 tokens")


class FakeChat:
    def __init__(self, client):
        self.completions = SimpleNamespace(create=client._create)


class FakeClient:
    """Fake client: records kwargs and returns chunks, a response, or raises.

    ``script`` is an optional list of callables, one per API call in order;
    each callable receives the request kwargs and returns an iterable of stream
    chunks, a non-stream response, or raises. When ``script`` is None the
    client behaves exactly as before, using the ``chunks``/``error``/``usage``/
    ``response`` fixtures.
    """

    def __init__(self, chunks=None, error=None, usage=None, response=None, script=None):
        self._chunks = chunks if chunks is not None else []
        self._error = error
        self._usage = usage
        self._response = response
        self._script = script
        self.payloads = []
        self.calls = 0
        self.chat = FakeChat(self)

    @property
    def last_kwargs(self):
        return self.payloads[-1] if self.payloads else None

    def _create(self, **kwargs):
        self.calls += 1
        self.payloads.append(kwargs)
        if self._script is not None:
            if self.calls - 1 < len(self._script):
                return self._script[self.calls - 1](kwargs)
            raise AssertionError(
                f"unexpected call #{self.calls}; script has {len(self._script)} steps"
            )
        if self._error is not None:
            raise self._error
        if self._response is not None:
            return self._response
        if self._usage is not None:
            return self._stream_with_usage()
        return self._chunks

    def _stream_with_usage(self):
        yield from self._chunks
        yield SimpleNamespace(choices=[], usage=self._usage)


class ChatAgentTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)

    def test_initial_history_has_single_system_message(self):
        agent = ChatAgent(FakeClient())
        history = agent.history
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["role"], "system")
        self.assertEqual(history[0]["content"], AgentConfig().system_prompt)

    def test_user_message_added_after_ask(self):
        agent = ChatAgent(FakeClient(chunks=chunks_of(["Привет, мир!"])))
        agent.ask("Привет")
        roles = [m["role"] for m in agent.history]
        self.assertEqual(roles, ["system", "user", "assistant"])
        self.assertEqual(agent.history[1]["content"], "Привет")

    def test_full_assistant_answer_joined_from_chunks(self):
        client = FakeClient(chunks=chunks_of(["При", "вет", ", ", "мир", "!"]))
        agent = ChatAgent(client)
        result = agent.ask("Привет")
        self.assertEqual(result.text, "Привет, мир!")
        self.assertEqual(agent.history[-1]["role"], "assistant")
        self.assertEqual(agent.history[-1]["content"], "Привет, мир!")

    def test_second_ask_sends_full_history(self):
        client = FakeClient(chunks=chunks_of(["Ок"]))
        agent = ChatAgent(client)
        agent.ask("Первый вопрос")
        agent.ask("Второй вопрос")

        messages = client.last_kwargs["messages"]
        self.assertEqual(len(messages), 4)
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "user"])
        self.assertEqual(messages[1]["content"], "Первый вопрос")
        self.assertEqual(messages[2]["content"], "Ок")
        self.assertEqual(messages[3]["content"], "Второй вопрос")

    def test_empty_input_raises_and_does_not_call_api(self):
        client = FakeClient()
        agent = ChatAgent(client)
        for empty in ("", "   "):
            with self.assertRaises(ValueError):
                agent.ask(empty)
            self.assertEqual(client.calls, 0)
            self.assertEqual(len(agent.history), 1)

    def test_api_error_rolls_back_history(self):
        def failing_stream():
            yield make_chunk("При")
            yield make_chunk("вет")
            raise RuntimeError("boom")

        client = FakeClient(chunks=failing_stream())
        agent = ChatAgent(client)
        before = agent.history

        with self.assertRaises(RuntimeError):
            agent.ask("Привет")

        self.assertEqual(agent.history, before)
        self.assertEqual(len(agent.history), 1)

    def test_custom_config_is_applied(self):
        config = AgentConfig(
            model="custom-model",
            system_prompt="Кастомный системный промпт",
            temperature=0.7,
            max_tokens=500,
            stream=True,
        )
        client = FakeClient(chunks=chunks_of(["Ответ"]))
        agent = ChatAgent(client, config=config)
        agent.ask("Привет")

        kwargs = client.last_kwargs
        self.assertEqual(kwargs["model"], "custom-model")
        self.assertEqual(kwargs["temperature"], 0.7)
        self.assertEqual(kwargs["max_tokens"], 500)
        self.assertEqual(kwargs["stream"], config.stream)
        self.assertEqual(kwargs["messages"][0]["content"], "Кастомный системный промпт")
        self.assertEqual(agent.history[0]["content"], "Кастомный системный промпт")

    def test_on_chunk_receives_accumulated_text(self):
        client = FakeClient(chunks=chunks_of(["a", "b", "c"]))
        agent = ChatAgent(client)
        seen = []
        agent.ask("Привет", on_chunk=seen.append)
        self.assertEqual(seen, ["a", "ab", "abc"])

    def test_non_stream_returns_full_answer(self):
        client = FakeClient(response=make_response("Полный ответ", usage=make_usage(10, 20)))
        agent = ChatAgent(client, config=AgentConfig(stream=False))
        result = agent.ask("Привет")
        self.assertEqual(result.text, "Полный ответ")
        self.assertEqual(agent.history[-1]["role"], "assistant")
        self.assertEqual(agent.history[-1]["content"], "Полный ответ")

    def test_stream_payload_contains_stream_options(self):
        client = FakeClient(chunks=chunks_of(["Ок"]))
        agent = ChatAgent(client)
        agent.ask("Привет")
        self.assertEqual(client.last_kwargs["stream_options"], {"include_usage": True})

    def test_stream_handles_final_usage_chunk_without_choices(self):
        chunks = [make_chunk("При"), make_chunk("вет")]
        usage = make_usage(7, 3)
        client = FakeClient(chunks=chunks, usage=usage)
        agent = ChatAgent(client)
        result = agent.ask("Вопрос")
        self.assertEqual(result.text, "Привет")
        self.assertEqual(result.stats.request_tokens, 7)
        self.assertEqual(result.stats.response_tokens, 3)

    def test_stream_usage_saved_to_store(self):
        client = FakeClient(chunks=chunks_of(["Привет"]), usage=make_usage(7, 3))
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            agent.ask("Привет")
            self.assertEqual(store.get_usage(chat_id), {"input_tokens": 7, "output_tokens": 3})

    def test_non_stream_usage_extracted(self):
        client = FakeClient(response=make_response("Ответ", usage=make_usage(11, 4)))
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig(stream=False))
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            agent.ask("Привет")
            self.assertEqual(store.get_usage(chat_id), {"input_tokens": 11, "output_tokens": 4})

    def test_absent_usage_stored_as_none(self):
        client = FakeClient(response=make_response("Ответ"))
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig(stream=False))
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            agent.ask("Привет")
            self.assertEqual(store.get_usage(chat_id), {"input_tokens": None, "output_tokens": None})

    def test_usage_as_dict(self):
        usage = {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "prompt_cache_hit_tokens": 4,
            "prompt_cache_miss_tokens": 6,
        }
        client = FakeClient(chunks=chunks_of(["Ответ"]), usage=usage)
        agent = ChatAgent(client)
        result = agent.ask("Вопрос")
        self.assertEqual(result.stats.request_tokens, 10)
        self.assertEqual(result.stats.response_tokens, 20)
        self.assertEqual(result.stats.total_tokens, 30)
        self.assertEqual(result.stats.prompt_cache_hit_tokens, 4)
        self.assertEqual(result.stats.prompt_cache_miss_tokens, 6)

    def test_usage_prompt_tokens_details_fallback(self):
        details = SimpleNamespace(cached_tokens=4)
        usage = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=20,
            prompt_tokens_details=details,
        )
        client = FakeClient(chunks=chunks_of(["Ответ"]), usage=usage)
        agent = ChatAgent(client)
        result = agent.ask("Вопрос")
        self.assertEqual(result.stats.prompt_cache_hit_tokens, 4)
        self.assertEqual(result.stats.prompt_cache_miss_tokens, 6)

    def test_ask_result_has_local_estimates(self):
        client = FakeClient(chunks=chunks_of(["Привет, мир!"]))
        agent = ChatAgent(client)
        result = agent.ask("Вопрос")
        self.assertIsNotNone(result.stats.user_message_tokens_est)
        self.assertIsNotNone(result.stats.assistant_tokens_est)
        self.assertGreaterEqual(result.stats.assistant_tokens_est, 1)

    def test_cache_hit_miss_passed_and_priced(self):
        usage = make_full_usage(1000, 500, total=1500, hit=400, miss=600)
        client = FakeClient(chunks=chunks_of(["Ответ"]), usage=usage)
        agent = ChatAgent(client)
        result = agent.ask("Вопрос")
        self.assertEqual(result.stats.prompt_cache_hit_tokens, 400)
        self.assertEqual(result.stats.prompt_cache_miss_tokens, 600)
        self.assertIsNotNone(result.stats.cost_usd)
        self.assertIn("cache hit/miss", result.stats.cost_assumption)

    def test_no_cache_split_uses_miss_assumption(self):
        usage = make_full_usage(1000, 500)
        client = FakeClient(chunks=chunks_of(["Ответ"]), usage=usage)
        agent = ChatAgent(client)
        result = agent.ask("Вопрос")
        self.assertIsNone(result.stats.prompt_cache_hit_tokens)
        self.assertIsNone(result.stats.prompt_cache_miss_tokens)
        self.assertIn("cache-miss", result.stats.cost_assumption)

    def test_turn_stats_persisted_and_reloaded(self):
        usage = make_full_usage(10, 20, total=30, hit=4, miss=6)
        client = FakeClient(chunks=chunks_of(["Ответ"]), usage=usage)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            store = ChatStore(path)
            chat_id = store.create_chat(AgentConfig())
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            result = agent.ask("Привет")
            self.assertEqual(result.stats.request_tokens, 10)
            self.assertEqual(result.stats.response_tokens, 20)

            reloaded = ChatStore(path)
            history = reloaded.load_history(chat_id)
            self.assertEqual([m.role for m in history], ["user", "assistant"])
            user_msg, assistant_msg = history
            self.assertIsNotNone(user_msg.turn)
            self.assertIsNotNone(assistant_msg.turn)
            self.assertEqual(user_msg.turn.request_tokens, 10)
            self.assertEqual(user_msg.turn.response_tokens, 20)
            self.assertEqual(user_msg.turn.total_tokens, 30)
            self.assertEqual(user_msg.turn.prompt_cache_hit_tokens, 4)
            self.assertEqual(user_msg.turn.prompt_cache_miss_tokens, 6)
            self.assertEqual(assistant_msg.turn.request_tokens, 10)

    def test_cumulative_tokens_and_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            client1 = FakeClient(chunks=chunks_of(["Ответ1"]), usage=make_full_usage(10, 5, total=15))
            ChatAgent(client1, store=store, chat_id=chat_id).ask("Вопрос 1")
            client2 = FakeClient(chunks=chunks_of(["Ответ2"]), usage=make_full_usage(20, 8, total=28))
            ChatAgent(client2, store=store, chat_id=chat_id).ask("Вопрос 2")

            stats = store.get_chat_stats(chat_id)
            self.assertEqual(stats.input_tokens, 30)
            self.assertEqual(stats.output_tokens, 13)
            self.assertEqual(stats.turns_count, 2)
            self.assertIsNotNone(stats.cost_usd)
            self.assertIsNotNone(stats.history_tokens_est)

    def test_finish_reason_stream_roundtrip(self):
        chunks = [make_chunk("От"), make_chunk("вет", finish_reason="length")]
        usage = make_usage(5, 6)
        client = FakeClient(chunks=chunks, usage=usage)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            store = ChatStore(path)
            chat_id = store.create_chat(AgentConfig())
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            result = agent.ask("Вопрос")
            self.assertEqual(result.stats.finish_reason, "length")

            reloaded = ChatStore(path).load_history(chat_id)
            self.assertEqual(reloaded[-1].turn.finish_reason, "length")

    def test_finish_reason_non_stream_roundtrip(self):
        response = make_response("Ответ", usage=make_usage(5, 6), finish_reason="stop")
        client = FakeClient(response=response)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            store = ChatStore(path)
            chat_id = store.create_chat(AgentConfig(stream=False))
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            result = agent.ask("Вопрос")
            self.assertEqual(result.stats.finish_reason, "stop")

            reloaded = ChatStore(path).load_history(chat_id)
            self.assertEqual(reloaded[-1].turn.finish_reason, "stop")

    def test_finish_reason_length_not_an_error(self):
        chunks = [make_chunk("Обрезанный ответ", finish_reason="length")]
        usage = make_usage(5, 6)
        client = FakeClient(chunks=chunks, usage=usage)
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            result = agent.ask("Вопрос")
            self.assertEqual(result.stats.finish_reason, "length")
            self.assertEqual(store.list_turns(chat_id)[0].stats.finish_reason, "length")
            self.assertEqual(len(store.load_messages(chat_id)), 2)

    def test_demo_context_limit_exceeded(self):
        config = AgentConfig(demo_context_limit=10, max_tokens=5)
        client = FakeClient(chunks=chunks_of(["Ок"]))
        agent = ChatAgent(client, config=config)
        with self.assertRaises(ContextLimitError) as ctx:
            agent.ask("Очень длинный вопрос, который точно превысит лимит")
        self.assertEqual(client.calls, 0)
        self.assertEqual(len(agent.history), 1)
        exc = ctx.exception
        self.assertEqual(exc.limit_tokens, 10)
        self.assertEqual(exc.requested_output_tokens, 5)
        self.assertGreaterEqual(exc.estimated_input_tokens, 1)

    def test_demo_context_limit_exceeded_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig(demo_context_limit=10, max_tokens=5))
            client = FakeClient(chunks=chunks_of(["Ок"]))
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            with self.assertRaises(ContextLimitError):
                agent.ask("Длинный текст который превысит лимит")
            self.assertEqual(client.calls, 0)
            self.assertEqual(store.load_messages(chat_id), [])
            self.assertEqual(store.get_chat_stats(chat_id).turns_count, 0)
            self.assertEqual(store.get_usage(chat_id), {"input_tokens": None, "output_tokens": None})

    def test_demo_context_limit_within(self):
        config = AgentConfig(demo_context_limit=1000, max_tokens=50)
        client = FakeClient(chunks=chunks_of(["Ок"]))
        agent = ChatAgent(client, config=config)
        result = agent.ask("Привет")
        self.assertEqual(client.calls, 1)
        self.assertEqual(result.text, "Ок")

    def test_context_overflow_error_mapped(self):
        client = FakeClient(error=FakeContextOverflowError())
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            with self.assertRaises(ApiContextOverflowError) as ctx:
                agent.ask("Длинный запрос")
            self.assertEqual(client.calls, 1)
            self.assertEqual(len(agent.history), 1)
            self.assertEqual(store.load_messages(chat_id), [])
            self.assertEqual(store.get_usage(chat_id), {"input_tokens": None, "output_tokens": None})
            self.assertIn("провайдера", str(ctx.exception))
            self.assertIn("maximum context length", ctx.exception.original_message)

    def test_other_api_error_re_raised(self):
        client = FakeClient(error=RuntimeError("boom"))
        agent = ChatAgent(client)
        with self.assertRaises(RuntimeError):
            agent.ask("Привет")
        self.assertEqual(len(agent.history), 1)

    def test_api_error_writes_nothing_to_store(self):
        def failing_stream():
            yield make_chunk("При")
            yield make_chunk("вет")
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            client = FakeClient(chunks=failing_stream())
            agent = ChatAgent(client, store=store, chat_id=chat_id)

            with self.assertRaises(RuntimeError):
                agent.ask("Привет")

            self.assertEqual(store.load_messages(chat_id), [])
            self.assertEqual(store.get_chat_stats(chat_id).turns_count, 0)
            self.assertEqual(store.get_usage(chat_id), {"input_tokens": None, "output_tokens": None})

    def test_stats_independent_across_chats(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            a = store.create_chat(AgentConfig())
            b = store.create_chat(AgentConfig())
            ChatAgent(
                FakeClient(chunks=chunks_of(["A"]), usage=make_full_usage(10, 5)),
                store=store, chat_id=a,
            ).ask("Вопрос A")
            ChatAgent(
                FakeClient(chunks=chunks_of(["B"]), usage=make_full_usage(100, 50)),
                store=store, chat_id=b,
            ).ask("Вопрос B")

            stats_a = store.get_chat_stats(a)
            stats_b = store.get_chat_stats(b)
            self.assertEqual(stats_a.input_tokens, 10)
            self.assertEqual(stats_b.input_tokens, 100)
            self.assertEqual(stats_a.turns_count, 1)
            self.assertEqual(stats_b.turns_count, 1)

    def test_restored_history_sent_in_next_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            store.save_turn(chat_id, "Вопрос", "Ответ")

            client = FakeClient(chunks=chunks_of(["Ещё ответ"]))
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            agent.ask("Ещё вопрос")

            messages = client.last_kwargs["messages"]
            self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "user"])
            self.assertEqual(messages[1]["content"], "Вопрос")
            self.assertEqual(messages[2]["content"], "Ответ")
            self.assertEqual(messages[3]["content"], "Ещё вопрос")

    def test_system_prompt_restored_and_used(self):
        config = AgentConfig(system_prompt="Сохранённый системный промпт")
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(config)

            client = FakeClient(chunks=chunks_of(["Ок"]))
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            agent.ask("Привет")

            self.assertEqual(client.last_kwargs["messages"][0]["content"], "Сохранённый системный промпт")
            self.assertEqual(agent.history[0]["content"], "Сохранённый системный промпт")

    def test_set_config_persists_and_updates_system_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            agent = ChatAgent(FakeClient(), store=store, chat_id=chat_id)

            new_config = AgentConfig(
                system_prompt="Новый промпт",
                model="other-model",
                temperature=0.9,
                max_tokens=200,
                stream=False,
                demo_context_limit=123,
            )
            agent.set_config(new_config)

            self.assertEqual(agent.history[0]["content"], "Новый промпт")
            loaded = store.load_config(chat_id)
            self.assertEqual(loaded["system_prompt"], "Новый промпт")
            self.assertEqual(loaded["model"], "other-model")
            self.assertAlmostEqual(loaded["temperature"], 0.9)
            self.assertEqual(loaded["max_tokens"], 200)
            self.assertFalse(loaded["stream"])
            self.assertEqual(loaded["demo_context_limit"], 123)

    def test_save_turn_failure_rolls_back_history(self):
        class FailingStore:
            def load_config(self, chat_id):
                return asdict(AgentConfig())

            def save_config(self, chat_id, config):
                pass

            def load_messages(self, chat_id):
                return []

            def save_turn(self, chat_id, user_text, assistant_text, stats=None):
                raise RuntimeError("save failed")

        client = FakeClient(chunks=chunks_of(["Ок"]))
        agent = ChatAgent(client, store=FailingStore(), chat_id=1)

        with self.assertRaises(RuntimeError):
            agent.ask("Привет")

        self.assertEqual(len(agent.history), 1)
        self.assertEqual(agent.history[0]["role"], "system")


class ChatAgentCompressionTest(unittest.TestCase):
    """Behaviour added in Day 9: automatic history compression via summarisation."""

    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)

    def test_summarization_triggers_and_next_payload_uses_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig(keep_recent_turns=3))
            client = FakeClient(
                script=[
                    turn("ответ 1"),
                    turn("ответ 2"),
                    turn("ответ 3"),
                    turn("ответ 4"),
                    summary("Сводка хода 1", usage=make_usage(100, 50)),
                    turn("ответ 5"),
                    summary("Сводка ходов 1-2", usage=make_usage(110, 55)),
                ]
            )
            agent = ChatAgent(client, store=store, chat_id=chat_id)

            for i in range(1, 5):
                agent.ask(f"вопрос {i}")

            # Four turns plus one summarisation call.
            self.assertEqual(client.calls, 5)
            summary_kwargs = client.payloads[4]
            self.assertFalse(summary_kwargs["stream"])
            self.assertEqual(summary_kwargs["temperature"], SUMMARY_TEMPERATURE)
            self.assertEqual(summary_kwargs["max_tokens"], SUMMARY_MAX_TOKENS)

            stored = store.load_summary(chat_id)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.content, "Сводка хода 1")
            self.assertEqual(stored.covered_messages_count, 2)

            # Next turn sends the summary plus exactly keep=3 previous turns.
            agent.ask("вопрос 5")
            turn5_kwargs = client.payloads[5]
            roles = [m["role"] for m in turn5_kwargs["messages"]]
            self.assertEqual(
                roles,
                ["system", "system", "user", "assistant", "user", "assistant", "user", "assistant", "user"],
            )
            self.assertIn("Сводка хода 1", turn5_kwargs["messages"][1]["content"])
            contents = [m["content"] for m in turn5_kwargs["messages"]]
            self.assertNotIn("вопрос 1", contents)
            self.assertNotIn("ответ 1", contents)
            self.assertIn("вопрос 2", contents)
            self.assertIn("вопрос 4", contents)

    def test_summary_tokens_accumulate_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig(keep_recent_turns=3))
            client = FakeClient(
                script=[
                    turn("ответ 1"),
                    turn("ответ 2"),
                    turn("ответ 3"),
                    turn("ответ 4"),
                    summary("Сводка", usage=make_usage(100, 50)),
                ]
            )
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            for i in range(1, 5):
                agent.ask(f"вопрос {i}")

            stats = store.get_chat_stats(chat_id)
            # Turns reported no usage, so turn counters stay None while the
            # summary counters hold the summarisation call's tokens.
            self.assertIsNone(stats.input_tokens)
            self.assertIsNone(stats.output_tokens)
            self.assertEqual(stats.summary_input_tokens, 100)
            self.assertEqual(stats.summary_output_tokens, 50)
            self.assertIsNotNone(stats.summary_cost_usd)

    def test_summarization_error_preserves_turn_and_retries_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig(keep_recent_turns=3))
            client = FakeClient(
                script=[
                    turn("ответ 1"),
                    turn("ответ 2"),
                    turn("ответ 3"),
                    turn("ответ 4"),
                    raises(RuntimeError("summary boom")),
                    turn("ответ 5"),
                    summary("Сводка", usage=make_usage(100, 50)),
                ]
            )
            agent = ChatAgent(client, store=store, chat_id=chat_id)

            result = None
            for i in range(1, 5):
                result = agent.ask(f"вопрос {i}")

            # The failing summarisation must not break the turn.
            self.assertIsNotNone(result.summary_error)
            self.assertEqual(len(store.load_messages(chat_id)), 8)
            self.assertIsNone(store.load_summary(chat_id))

            # The next ask retries the merge with no duplication: turns 1 and 2
            # are folded together (anchor did not advance) and no stale
            # "previous summary" is injected.
            agent.ask("вопрос 5")
            retry_kwargs = client.payloads[6]
            joined = " ".join(m["content"] for m in retry_kwargs["messages"])
            self.assertNotIn("Предыдущая сводка", joined)
            self.assertIn("вопрос 1", joined)
            self.assertIn("вопрос 2", joined)
            self.assertNotIn("вопрос 3", joined)
            self.assertEqual(joined.count("вопрос 1"), 1)
            self.assertIsNotNone(store.load_summary(chat_id))

    def test_compression_disabled_sends_full_history(self):
        client = FakeClient(chunks=chunks_of(["ок"]))
        agent = ChatAgent(client, config=AgentConfig(summarize=False))
        for i in range(1, 6):
            agent.ask(f"вопрос {i}")

        # No summarisation call ever happened.
        self.assertEqual(client.calls, 5)
        messages = client.last_kwargs["messages"]
        # Full history at the 5th ask: system + 4 previous turns + new user.
        self.assertEqual(len(messages), 1 + 4 * 2 + 1)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["content"], "вопрос 1")

    def test_compression_disabled_demo_limit_blocks_api_and_db(self):
        config = AgentConfig(summarize=False, demo_context_limit=10, max_tokens=5)
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(config)
            client = FakeClient(chunks=chunks_of(["ок"]))
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            with self.assertRaises(ContextLimitError):
                agent.ask("Очень длинный вопрос, который точно превысит лимит")
            self.assertEqual(client.calls, 0)
            self.assertEqual(store.load_messages(chat_id), [])

    def test_reenable_uses_existing_summary_and_compresses_only_new_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig(keep_recent_turns=3))
            client = FakeClient(
                script=[
                    turn("ответ 1"),
                    turn("ответ 2"),
                    turn("ответ 3"),
                    turn("ответ 4"),
                    summary("Сводка хода 1", usage=make_usage(100, 50)),
                    turn("ответ 5"),
                    turn("ответ 6"),
                    turn("ответ 7"),
                    summary("Сводка ходов 1-4", usage=make_usage(120, 60)),
                ]
            )
            agent = ChatAgent(client, store=store, chat_id=chat_id)

            # Phase 1: four turns, summarisation covers turn 1.
            for i in range(1, 5):
                agent.ask(f"вопрос {i}")
            self.assertEqual(store.load_summary(chat_id).covered_messages_count, 2)

            # Phase 2: compression disabled, two turns, no further summarisation.
            agent.set_config(AgentConfig(summarize=False))
            for i in (5, 6):
                agent.ask(f"вопрос {i}")
            self.assertEqual(store.load_summary(chat_id).covered_messages_count, 2)

            # Phase 3: re-enable; the new tail (turns 2-4) is compressed using
            # the existing summary as "previous summary".
            agent.set_config(AgentConfig(summarize=True, keep_recent_turns=3))
            agent.ask("вопрос 7")
            reenable_kwargs = client.payloads[8]
            joined = " ".join(m["content"] for m in reenable_kwargs["messages"])
            self.assertIn("Предыдущая сводка", joined)
            self.assertIn("Сводка хода 1", joined)
            self.assertIn("вопрос 2", joined)
            self.assertIn("вопрос 3", joined)
            self.assertIn("вопрос 4", joined)
            self.assertNotIn("вопрос 5", joined)
            self.assertNotIn("вопрос 1", joined)
            self.assertEqual(store.load_summary(chat_id).covered_messages_count, 8)

    def test_open_chat_and_set_config_do_not_call_api(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            agent.set_config(AgentConfig(system_prompt="новый промпт"))
            self.assertEqual(client.calls, 0)

    def test_summarization_with_no_store(self):
        client = FakeClient(
            script=[
                turn("ответ 1"),
                turn("ответ 2"),
                turn("ответ 3"),
                turn("ответ 4"),
                summary("Сводка", usage=make_usage(100, 50)),
                turn("ответ 5"),
                summary("Сводка 2", usage=make_usage(110, 55)),
            ]
        )
        agent = ChatAgent(client, config=AgentConfig(keep_recent_turns=3))
        for i in range(1, 5):
            agent.ask(f"вопрос {i}")
        self.assertEqual(client.calls, 5)
        agent.ask("вопрос 5")
        self.assertIn("Сводка", client.payloads[5]["messages"][1]["content"])

    def test_store_without_summary_methods_works(self):
        class BasicStore:
            def load_config(self, chat_id):
                return asdict(AgentConfig())

            def load_messages(self, chat_id):
                return []

            def save_turn(self, chat_id, user_text, assistant_text, stats=None):
                pass

        client = FakeClient(
            script=[
                turn("1"),
                turn("2"),
                turn("3"),
                turn("4"),
                summary("Сводка", usage=make_usage(100, 50)),
            ]
        )
        agent = ChatAgent(client, store=BasicStore(), chat_id=1)
        result = None
        for i in range(1, 5):
            result = agent.ask(f"вопрос {i}")
        self.assertEqual(client.calls, 5)
        self.assertIsNone(result.summary_error)

    def test_on_summarizing_enters_and_exits(self):
        class RecordingCM:
            def __init__(self, log):
                self.log = log

            def __enter__(self):
                self.log.append("enter")
                return self

            def __exit__(self, *exc):
                self.log.append("exit")
                return False

        log = []
        client = FakeClient(
            script=[
                turn("1"),
                turn("2"),
                turn("3"),
                turn("4"),
                summary("Сводка", usage=make_usage(100, 50)),
            ]
        )
        agent = ChatAgent(client, config=AgentConfig(keep_recent_turns=3))
        for i in range(1, 5):
            agent.ask(f"вопрос {i}", on_summarizing=lambda: RecordingCM(log))
        self.assertEqual(log, ["enter", "exit"])

    def test_empty_summary_yields_error_and_db_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig(keep_recent_turns=3))
            client = FakeClient(
                script=[
                    turn("1"),
                    turn("2"),
                    turn("3"),
                    turn("4"),
                    summary("", usage=make_usage(100, 50)),
                ]
            )
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            result = None
            for i in range(1, 5):
                result = agent.ask(f"вопрос {i}")

            self.assertIsNotNone(result.summary_error)
            self.assertIsNone(store.load_summary(chat_id))
            self.assertEqual(len(store.load_messages(chat_id)), 8)
            # The executed attempt is billed even though no summary was created.
            stats = store.get_chat_stats(chat_id)
            self.assertEqual(stats.summary_input_tokens, 100)
            self.assertEqual(stats.summary_output_tokens, 50)

    def test_summary_isolated_between_chats(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_a = store.create_chat(AgentConfig(keep_recent_turns=3))
            chat_b = store.create_chat(AgentConfig(keep_recent_turns=3))
            chat_c = store.create_chat(AgentConfig(keep_recent_turns=3))

            # Chat A produces its own summary with distinct content and tokens.
            client_a = FakeClient(
                script=[
                    turn("A1"),
                    turn("A2"),
                    turn("A3"),
                    turn("A4"),
                    summary("Сводка A", usage=make_usage(100, 50)),
                ]
            )
            agent_a = ChatAgent(client_a, store=store, chat_id=chat_a)
            for i in range(1, 5):
                agent_a.ask(f"вопрос A{i}")

            # Chat B produces a different summary.
            client_b = FakeClient(
                script=[
                    turn("B1"),
                    turn("B2"),
                    turn("B3"),
                    turn("B4"),
                    summary("Сводка B", usage=make_usage(7, 3)),
                ]
            )
            agent_b = ChatAgent(client_b, store=store, chat_id=chat_b)
            for i in range(1, 5):
                agent_b.ask(f"вопрос B{i}")

            # load_summary returns distinct values per chat.
            summary_a = store.load_summary(chat_a)
            summary_b = store.load_summary(chat_b)
            self.assertEqual(summary_a.content, "Сводка A")
            self.assertEqual(summary_b.content, "Сводка B")

            # Cumulative summary counters are not mixed across chats.
            stats_a = store.get_chat_stats(chat_a)
            stats_b = store.get_chat_stats(chat_b)
            self.assertEqual(stats_a.summary_input_tokens, 100)
            self.assertEqual(stats_a.summary_output_tokens, 50)
            self.assertEqual(stats_b.summary_input_tokens, 7)
            self.assertEqual(stats_b.summary_output_tokens, 3)

            # A chat without a summary returns None and keeps null counters.
            self.assertIsNone(store.load_summary(chat_c))
            stats_c = store.get_chat_stats(chat_c)
            self.assertIsNone(stats_c.summary_input_tokens)
            self.assertIsNone(stats_c.summary_output_tokens)
            self.assertIsNone(stats_c.summary_cost_usd)

            # Compressing chat A again must not touch chat B's summary. With
            # batching the second summary needs three more turns (A5-A7).
            client_a2 = FakeClient(
                script=[
                    turn("A5"),
                    turn("A6"),
                    turn("A7"),
                    summary("Сводка A2", usage=make_usage(120, 60)),
                ]
            )
            agent_a2 = ChatAgent(client_a2, store=store, chat_id=chat_a)
            for i in (5, 6, 7):
                agent_a2.ask(f"вопрос A{i}")

            self.assertEqual(store.load_summary(chat_a).content, "Сводка A2")
            self.assertEqual(store.load_summary(chat_b).content, "Сводка B")
            self.assertEqual(
                store.get_chat_stats(chat_b).summary_input_tokens, 7
            )
            self.assertEqual(
                store.get_chat_stats(chat_b).summary_output_tokens, 3
            )

    def test_summary_restored_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            store = ChatStore(path)
            chat_id = store.create_chat(AgentConfig(keep_recent_turns=3))

            # Phase 1: four turns produce a summary covering turn 1.
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
            for i in range(1, 5):
                agent.ask(f"вопрос {i}")
            self.assertEqual(store.load_summary(chat_id).covered_messages_count, 2)

            # "Restart": a fresh store and agent for the same chat_id.
            restarted_store = ChatStore(path)
            restarted_client = FakeClient(
                script=[
                    turn("ответ 5"),
                    turn("ответ 6"),
                    turn("ответ 7"),
                    summary("Сводка ходов 1-4", usage=make_usage(110, 55)),
                ]
            )
            restarted_agent = ChatAgent(
                restarted_client, store=restarted_store, chat_id=chat_id
            )
            # Opening the chat must not trigger a paid summarisation call.
            self.assertEqual(restarted_client.calls, 0)

            restarted_agent.ask("вопрос 5")

            # The new turn carries the restored summary as a system message and
            # does not re-send the covered turn 1.
            turn5_kwargs = restarted_client.payloads[0]
            self.assertEqual(turn5_kwargs["messages"][1]["role"], "system")
            self.assertIn("Сводка хода 1", turn5_kwargs["messages"][1]["content"])
            contents = [m["content"] for m in turn5_kwargs["messages"]]
            self.assertNotIn("вопрос 1", contents)
            self.assertNotIn("ответ 1", contents)
            self.assertIn("вопрос 2", contents)

            # Batching: the second summary fires only on the 7th turn, folding
            # the continuous tail (turns 2-4) onto the restored summary.
            restarted_agent.ask("вопрос 6")
            restarted_agent.ask("вопрос 7")

            summary_call = restarted_client.payloads[3]
            joined = " ".join(m["content"] for m in summary_call["messages"])
            self.assertIn("Предыдущая сводка", joined)
            self.assertIn("Сводка хода 1", joined)
            self.assertIn("вопрос 2", joined)
            self.assertIn("вопрос 3", joined)
            self.assertIn("вопрос 4", joined)
            self.assertNotIn("вопрос 1", joined)
            self.assertNotIn("вопрос 5", joined)
            self.assertEqual(joined.count("вопрос 2"), 1)
            self.assertEqual(
                restarted_store.load_summary(chat_id).covered_messages_count, 8
            )

    def test_compression_runs_in_batches_not_every_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig(keep_recent_turns=3))
            client = FakeClient(
                script=[
                    turn("1"),
                    turn("2"),
                    turn("3"),
                    turn("4"),
                    summary("S1", usage=make_usage(100, 50)),
                    turn("5"),
                    turn("6"),
                    turn("7"),
                    summary("S2", usage=make_usage(110, 55)),
                ]
            )
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            for i in range(1, 8):
                agent.ask(f"вопрос {i}")

            # Seven turns plus exactly two summarisation calls (after turns 4 and 7).
            self.assertEqual(client.calls, 9)
            stream_flags = [kwargs.get("stream") for kwargs in client.payloads]
            non_stream_indices = [i for i, s in enumerate(stream_flags) if s is False]
            self.assertEqual(non_stream_indices, [4, 8])
            self.assertEqual(store.load_summary(chat_id).covered_messages_count, 8)

    def test_pre_request_compression_before_main_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            config = AgentConfig(keep_recent_turns=3, max_tokens=100)
            chat_id = store.create_chat(config)

            long_text = "достаточно длинный текст сообщения " * 10
            for i in range(1, 9):
                store.save_turn(chat_id, f"{long_text} {i}", f"{long_text} ответ {i}")

            pairs = store.load_messages(chat_id)
            system_prompt = config.system_prompt
            new_message = f"{long_text} новый вопрос"

            full_payload = build_payload(
                system_prompt, pairs, new_message, summarize_enabled=False
            )
            full_est = sum(estimate_tokens(m["content"]) for m in full_payload)
            compressed_payload = build_payload(
                system_prompt,
                pairs,
                new_message,
                summary_content="Сводка",
                covered_messages_count=10,
            )
            compressed_est = sum(
                estimate_tokens(m["content"]) for m in compressed_payload
            )

            # Limit fits the compressed payload but not the full one.
            limit = compressed_est + config.max_tokens
            store.save_config(
                chat_id,
                AgentConfig(
                    keep_recent_turns=3,
                    max_tokens=100,
                    summarize=True,
                    demo_context_limit=limit,
                ),
            )

            client = FakeClient(
                script=[
                    summary("Сводка", usage=make_usage(120, 40)),
                    turn("Ответ"),
                ]
            )
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            result = agent.ask(new_message)

            # Summarisation first, then the main request with the compressed payload.
            self.assertEqual(client.calls, 2)
            self.assertFalse(client.payloads[0]["stream"])
            main_kwargs = client.payloads[1]
            self.assertTrue(main_kwargs["stream"])
            self.assertEqual(main_kwargs["messages"][0]["role"], "system")
            self.assertEqual(main_kwargs["messages"][1]["role"], "system")
            self.assertIn("Сводка", main_kwargs["messages"][1]["content"])
            self.assertIsNone(result.summary_error)
            # Old history is intact and the summary was persisted.
            self.assertEqual(len(store.load_messages(chat_id)), 18)
            self.assertEqual(store.load_summary(chat_id).covered_messages_count, 10)

    def test_pre_request_compression_disabled_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            config = AgentConfig(keep_recent_turns=3, summarize=False, max_tokens=100)
            chat_id = store.create_chat(config)

            long_text = "достаточно длинный текст сообщения " * 10
            for i in range(1, 9):
                store.save_turn(chat_id, f"{long_text} {i}", f"{long_text} ответ {i}")

            pairs = store.load_messages(chat_id)
            new_message = f"{long_text} новый вопрос"
            full_payload = build_payload(
                config.system_prompt, pairs, new_message, summarize_enabled=False
            )
            full_est = sum(estimate_tokens(m["content"]) for m in full_payload)
            limit = full_est + config.max_tokens - 1
            store.save_config(
                chat_id,
                AgentConfig(
                    keep_recent_turns=3,
                    summarize=False,
                    max_tokens=100,
                    demo_context_limit=limit,
                ),
            )

            client = FakeClient(chunks=chunks_of(["ок"]))
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            with self.assertRaises(ContextLimitError):
                agent.ask(new_message)
            self.assertEqual(client.calls, 0)
            self.assertEqual(len(store.load_messages(chat_id)), 16)

    def test_truncated_summary_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig(keep_recent_turns=3))
            # Both the first attempt and the retry are truncated.
            client = FakeClient(
                script=[
                    turn("1"),
                    turn("2"),
                    turn("3"),
                    turn("4"),
                    summary(
                        "Обрезанная сводка",
                        usage=make_usage(100, 50),
                        finish_reason="length",
                    ),
                    summary(
                        "Обрезанная сводка ещё раз",
                        usage=make_usage(100, 50),
                        finish_reason="length",
                    ),
                ]
            )
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            result = None
            for i in range(1, 5):
                result = agent.ask(f"вопрос {i}")

            self.assertIsNotNone(result.summary_error)
            self.assertIsNone(store.load_summary(chat_id))
            self.assertEqual(len(store.load_messages(chat_id)), 8)
            # Both truncated attempts are billed, but no summary is created.
            stats = store.get_chat_stats(chat_id)
            self.assertEqual(stats.summary_input_tokens, 200)
            self.assertEqual(stats.summary_output_tokens, 100)

    def test_truncated_summary_preserves_previous_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig(keep_recent_turns=3))
            client = FakeClient(
                script=[
                    turn("1"),
                    turn("2"),
                    turn("3"),
                    turn("4"),
                    summary("S1", usage=make_usage(100, 50)),
                    turn("5"),
                    turn("6"),
                    turn("7"),
                    summary(
                        "обрыв",
                        usage=make_usage(110, 55),
                        finish_reason="length",
                    ),
                    summary(
                        "обрыв 2",
                        usage=make_usage(110, 55),
                        finish_reason="length",
                    ),
                ]
            )
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            for i in range(1, 5):
                agent.ask(f"вопрос {i}")

            before = store.load_summary(chat_id)
            stats_before = store.get_chat_stats(chat_id)

            for i in (5, 6, 7):
                last_result = agent.ask(f"вопрос {i}")

            # Both attempts of the 7th-turn summary were truncated: the previous
            # summary, its boundary and its stored tokens stay intact, while both
            # executed attempts are still billed.
            self.assertIsNotNone(last_result.summary_error)
            after = store.load_summary(chat_id)
            self.assertEqual(after.content, "S1")
            self.assertEqual(after.covered_messages_count, 2)
            self.assertEqual(after.updated_at, before.updated_at)
            self.assertEqual(after.prompt_tokens, before.prompt_tokens)
            self.assertEqual(after.response_tokens, before.response_tokens)
            stats_after = store.get_chat_stats(chat_id)
            self.assertEqual(
                stats_after.summary_input_tokens,
                stats_before.summary_input_tokens + 220,
            )
            self.assertEqual(
                stats_after.summary_output_tokens,
                stats_before.summary_output_tokens + 110,
            )
            self.assertEqual(len(store.load_messages(chat_id)), 14)

    def test_reasoning_summary_retries_with_larger_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig(keep_recent_turns=3))

            # The model needs more than the base budget but fits the retry.
            required = SUMMARY_MAX_TOKENS + 100
            first = reasoning_summary("Сводка A", required, usage=make_usage(100, 50))
            second = reasoning_summary("Сводка B", required, usage=make_usage(110, 55))
            client = FakeClient(
                script=[
                    turn("1"),
                    turn("2"),
                    turn("3"),
                    turn("4"),
                    first,  # turn 4: truncated
                    first,  # turn 4 retry: succeeds
                    turn("5"),
                    turn("6"),
                    turn("7"),
                    second,  # turn 7: truncated
                    second,  # turn 7 retry: succeeds
                ]
            )
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            for i in range(1, 5):
                agent.ask(f"вопрос {i}")

            after_first = store.load_summary(chat_id)
            self.assertEqual(after_first.content, "Сводка A")
            self.assertEqual(after_first.covered_messages_count, 2)
            stats_first = store.get_chat_stats(chat_id)

            # Ensure the second save lands on a later updated_at second.
            time.sleep(1.1)
            for i in (5, 6, 7):
                agent.ask(f"вопрос {i}")

            after_second = store.load_summary(chat_id)
            self.assertEqual(after_second.content, "Сводка B")
            self.assertEqual(after_second.covered_messages_count, 8)
            self.assertNotEqual(after_second.updated_at, after_first.updated_at)
            stats_second = store.get_chat_stats(chat_id)
            self.assertGreater(
                stats_second.summary_input_tokens, stats_first.summary_input_tokens
            )
            self.assertGreater(
                stats_second.summary_output_tokens, stats_first.summary_output_tokens
            )
            # Seven turns plus two summarisation attempts each (base + retry).
            self.assertEqual(client.calls, 11)

    def test_reasoning_double_truncation_keeps_previous_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig(keep_recent_turns=3))
            # Even the retry budget is below what the fake model needs.
            failing = reasoning_summary(
                "никогда", SUMMARY_RETRY_MAX_TOKENS + 100
            )
            client = FakeClient(
                script=[
                    turn("1"),
                    turn("2"),
                    turn("3"),
                    turn("4"),
                    summary("S1", usage=make_usage(100, 50)),
                    turn("5"),
                    turn("6"),
                    turn("7"),
                    failing,  # turn 7: truncated
                    failing,  # turn 7 retry: truncated
                ]
            )
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            last = None
            for i in range(1, 8):
                last = agent.ask(f"вопрос {i}")

            self.assertIsNotNone(last.summary_error)
            stored = store.load_summary(chat_id)
            self.assertEqual(stored.content, "S1")
            self.assertEqual(stored.covered_messages_count, 2)

    def test_keep_recent_turns_change_keeps_messages_consistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig(keep_recent_turns=3))
            client = FakeClient(
                script=[
                    turn("1"),
                    turn("2"),
                    turn("3"),
                    turn("4"),
                    summary("S1", usage=make_usage(100, 50)),
                    turn("5"),
                    turn("6"),
                    turn("7"),
                    summary("S2", usage=make_usage(110, 55)),
                ]
            )
            agent = ChatAgent(client, store=store, chat_id=chat_id)

            # First summary at keep=3 covers turn 1.
            for i in range(1, 5):
                agent.ask(f"вопрос {i}")
            self.assertEqual(store.load_summary(chat_id).covered_messages_count, 2)

            # Increasing keep sends the full uncovered tail, with no summary.
            agent.set_config(AgentConfig(keep_recent_turns=10))
            agent.ask("вопрос 5")
            turn5_payload = client.payloads[5]
            non_system = [
                m["content"] for m in turn5_payload["messages"] if m["role"] != "system"
            ]
            self.assertEqual(
                non_system,
                ["вопрос 2", "2", "вопрос 3", "3", "вопрос 4", "4", "вопрос 5"],
            )
            self.assertEqual(store.load_summary(chat_id).covered_messages_count, 2)

            # Restoring keep=3 reaches the next batch: turns 2-4 fold in a
            # continuous range, each message exactly once.
            agent.set_config(AgentConfig(keep_recent_turns=3))
            agent.ask("вопрос 6")
            agent.ask("вопрос 7")
            summary_call = client.payloads[8]
            joined = " ".join(m["content"] for m in summary_call["messages"])
            for i in range(2, 5):
                self.assertEqual(joined.count(f"[user]: вопрос {i}"), 1)
                self.assertEqual(joined.count(f"[assistant]: {i}"), 1)
            self.assertNotIn("вопрос 1", joined)
            self.assertNotIn("вопрос 5", joined)
            self.assertEqual(store.load_summary(chat_id).covered_messages_count, 8)

    def test_retry_bills_both_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig(keep_recent_turns=3))
            client = FakeClient(
                script=[
                    turn("1"),
                    turn("2"),
                    turn("3"),
                    turn("4"),
                    # First attempt truncated, then the retry succeeds.
                    summary(
                        "обрезано",
                        usage=make_full_usage(100, 50, total=150, hit=10, miss=90),
                        finish_reason="length",
                    ),
                    summary(
                        "Сводка",
                        usage=make_full_usage(200, 60, total=260, hit=20, miss=180),
                    ),
                ]
            )
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            result = None
            for i in range(1, 5):
                result = agent.ask(f"вопрос {i}")

            self.assertIsNone(result.summary_error)
            stored = store.load_summary(chat_id)
            self.assertEqual(stored.content, "Сводка")
            self.assertEqual(stored.covered_messages_count, 2)
            # The summaries row stores the aggregate of both attempts.
            self.assertEqual(stored.prompt_tokens, 300)
            self.assertEqual(stored.response_tokens, 110)
            self.assertEqual(stored.total_tokens, 410)
            stats = store.get_chat_stats(chat_id)
            self.assertEqual(stats.summary_input_tokens, 300)
            self.assertEqual(stats.summary_output_tokens, 110)
            self.assertEqual(stats.summary_cache_hit_tokens, 30)
            self.assertEqual(stats.summary_cache_miss_tokens, 270)
            self.assertIsNotNone(stored.cost_usd)
            self.assertAlmostEqual(stats.summary_cost_usd, stored.cost_usd)
            # Four turns plus the base attempt and the retry.
            self.assertEqual(client.calls, 6)

    def test_success_billed_once_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig(keep_recent_turns=3))
            client = FakeClient(
                script=[
                    turn("1"),
                    turn("2"),
                    turn("3"),
                    turn("4"),
                    summary("Сводка", usage=make_usage(100, 50)),
                ]
            )
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            result = None
            for i in range(1, 5):
                result = agent.ask(f"вопрос {i}")

            self.assertIsNone(result.summary_error)
            stats = store.get_chat_stats(chat_id)
            self.assertEqual(stats.summary_input_tokens, 100)
            self.assertEqual(stats.summary_output_tokens, 50)
            stored = store.load_summary(chat_id)
            self.assertEqual(stored.prompt_tokens, 100)
            self.assertEqual(stored.response_tokens, 50)
            # Four turns plus exactly one summarisation call, no duplication.
            self.assertEqual(client.calls, 5)

    def test_save_summary_failure_keeps_boundary(self):
        class FailingSaveStore:
            """Delegates to a real store but fails the first ``save_summary``."""

            def __init__(self, inner):
                self._inner = inner
                self.fail_once = True

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def save_summary(self, *args, **kwargs):
                if self.fail_once:
                    self.fail_once = False
                    raise RuntimeError("save failed")
                return self._inner.save_summary(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig(keep_recent_turns=3))
            client = FakeClient(
                script=[
                    turn("1"),
                    turn("2"),
                    turn("3"),
                    turn("4"),
                    summary("Сводка", usage=make_usage(100, 50)),
                    turn("5"),
                    summary("Сводка 2", usage=make_usage(110, 55)),
                ]
            )
            agent = ChatAgent(
                client, store=FailingSaveStore(store), chat_id=chat_id
            )
            result = None
            for i in range(1, 5):
                result = agent.ask(f"вопрос {i}")

            # The failed save leaves the summary and its boundary unchanged.
            self.assertIsNotNone(result.summary_error)
            self.assertIsNone(store.load_summary(chat_id))
            self.assertEqual(len(store.load_messages(chat_id)), 8)

            # The next ask still plans from the old boundary (0) and retries the
            # whole merge instead of resuming past turn 1.
            agent.ask("вопрос 5")
            stored = store.load_summary(chat_id)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.covered_messages_count, 4)
            summary_call = client.payloads[6]
            joined = " ".join(m["content"] for m in summary_call["messages"])
            self.assertIn("вопрос 1", joined)
            self.assertIn("вопрос 2", joined)
            self.assertNotIn("Предыдущая сводка", joined)

    def test_missing_usage_keeps_summary_counters_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig(keep_recent_turns=3))
            client = FakeClient(
                script=[
                    turn("1"),
                    turn("2"),
                    turn("3"),
                    turn("4"),
                    summary("Сводка"),  # no usage reported by the provider
                ]
            )
            agent = ChatAgent(client, store=store, chat_id=chat_id)
            result = None
            for i in range(1, 5):
                result = agent.ask(f"вопрос {i}")

            self.assertIsNone(result.summary_error)
            stored = store.load_summary(chat_id)
            self.assertEqual(stored.content, "Сводка")
            self.assertIsNone(stored.prompt_tokens)
            self.assertIsNone(stored.response_tokens)
            self.assertIsNone(stored.cost_usd)
            # Missing usage stays "no data" instead of being coerced to 0.
            stats = store.get_chat_stats(chat_id)
            self.assertIsNone(stats.summary_input_tokens)
            self.assertIsNone(stats.summary_output_tokens)
            self.assertIsNone(stats.summary_cache_hit_tokens)
            self.assertIsNone(stats.summary_cache_miss_tokens)
            self.assertIsNone(stats.summary_cost_usd)


class ModelNormalizationTest(unittest.TestCase):
    """The canonical model ID must survive config, storage and API payloads."""

    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)

    def test_default_config_model_is_canonical(self):
        self.assertEqual(AgentConfig().model, "deepseek-flash")

    def test_legacy_model_in_agent_config_is_normalized(self):
        config = AgentConfig(model="deepseek-v4-flash")
        self.assertEqual(config.model, "deepseek-flash")

    def test_custom_model_is_not_rewritten(self):
        self.assertEqual(AgentConfig(model="custom-model").model, "custom-model")

    def test_default_ask_sends_canonical_model(self):
        client = FakeClient(chunks=chunks_of(["Ок"]))
        agent = ChatAgent(client)
        agent.ask("Привет")
        self.assertEqual(client.last_kwargs["model"], "deepseek-flash")

    def test_legacy_model_in_store_is_migrated_before_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            store = ChatStore(path)
            chat_id = store.create_chat(AgentConfig())
            store.save_turn(
                chat_id,
                "Старый вопрос",
                "Старый ответ",
                stats=TurnStats(request_tokens=11, response_tokens=5),
            )
            # Simulate a row written by a previous app version.
            with closing(sqlite3.connect(path)) as conn:
                conn.execute(
                    "UPDATE chats SET model = 'deepseek-v4-flash' WHERE id = ?",
                    (chat_id,),
                )
                conn.commit()

            migrated = ChatStore(path)
            client = FakeClient(chunks=chunks_of(["Новый ответ"]))
            agent = ChatAgent(client, store=migrated, chat_id=chat_id)
            agent.ask("Новый вопрос")

            kwargs = client.last_kwargs
            self.assertEqual(kwargs["model"], "deepseek-flash")
            # The displayed name is presentation-only and must never reach the API.
            self.assertNotIn("DeepSeek V4.1 Flash", repr(kwargs))
            # History and counters from the legacy row are preserved.
            self.assertEqual(
                [m["content"] for m in migrated.load_messages(chat_id)][:2],
                ["Старый вопрос", "Старый ответ"],
            )
            usage = migrated.get_usage(chat_id)
            self.assertEqual(usage["input_tokens"], 11)
            self.assertEqual(usage["output_tokens"], 5)
            self.assertIsNotNone(migrated.load_history(chat_id)[0].turn)


if __name__ == "__main__":
    unittest.main()
