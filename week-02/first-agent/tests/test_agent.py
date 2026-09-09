import os
import tempfile
import unittest
from dataclasses import asdict
from types import SimpleNamespace

from agent import AgentConfig, ChatAgent
from storage import ChatStore


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


def make_response(content, usage=None):
    """Build a non-stream response: choices[0].message.content (+ optional usage)."""
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    response = SimpleNamespace(choices=[choice])
    if usage is not None:
        response.usage = usage
    return response


class FakeChat:
    def __init__(self, client):
        self.completions = SimpleNamespace(create=client._create)


class FakeClient:
    """Fake client: records kwargs and returns chunks, a response, or raises."""

    def __init__(self, chunks=None, error=None, usage=None, response=None):
        self._chunks = chunks if chunks is not None else []
        self._error = error
        self._usage = usage
        self._response = response
        self.last_kwargs = None
        self.calls = 0
        self.chat = FakeChat(self)

    def _create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
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
        answer = agent.ask("Привет")
        self.assertEqual(answer, "Привет, мир!")
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
        answer = agent.ask("Привет")
        self.assertEqual(answer, "Полный ответ")
        self.assertEqual(agent.history[-1]["role"], "assistant")
        self.assertEqual(agent.history[-1]["content"], "Полный ответ")

    def test_stream_payload_contains_stream_options(self):
        client = FakeClient(chunks=chunks_of(["Ок"]))
        agent = ChatAgent(client)
        agent.ask("Привет")
        self.assertEqual(client.last_kwargs["stream_options"], {"include_usage": True})

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
            )
            agent.set_config(new_config)

            self.assertEqual(agent.history[0]["content"], "Новый промпт")
            loaded = store.load_config(chat_id)
            self.assertEqual(loaded["system_prompt"], "Новый промпт")
            self.assertEqual(loaded["model"], "other-model")
            self.assertAlmostEqual(loaded["temperature"], 0.9)
            self.assertEqual(loaded["max_tokens"], 200)
            self.assertFalse(loaded["stream"])

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
            self.assertEqual(store.get_usage(chat_id), {"input_tokens": None, "output_tokens": None})

    def test_save_turn_failure_rolls_back_history(self):
        class FailingStore:
            def load_config(self, chat_id):
                return asdict(AgentConfig())

            def save_config(self, chat_id, config):
                pass

            def load_messages(self, chat_id):
                return []

            def save_turn(self, chat_id, user_text, assistant_text,
                          input_tokens=None, output_tokens=None):
                raise RuntimeError("save failed")

        client = FakeClient(chunks=chunks_of(["Ок"]))
        agent = ChatAgent(client, store=FailingStore(), chat_id=1)

        with self.assertRaises(RuntimeError):
            agent.ask("Привет")

        self.assertEqual(len(agent.history), 1)
        self.assertEqual(agent.history[0]["role"], "system")


if __name__ == "__main__":
    unittest.main()
