import os
import unittest
from types import SimpleNamespace

from agent import AgentConfig, ChatAgent


def make_chunk(content, finish_reason=None):
    """Build a fake stream chunk: chunk.choices[0].delta.content."""
    delta = SimpleNamespace(content=content)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


def chunks_of(contents):
    """Return a list of chunks for a list of text fragments."""
    return [make_chunk(text) for text in contents]


class FakeChat:
    def __init__(self, client):
        self.completions = SimpleNamespace(create=client._create)


class FakeClient:
    """Fake client: records kwargs and returns the given stream."""

    def __init__(self, chunks=None, error=None):
        self._chunks = chunks if chunks is not None else []
        self._error = error
        self.last_kwargs = None
        self.calls = 0
        self.chat = FakeChat(self)

    def _create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if self._error is not None:
            raise self._error
        return self._chunks


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

    def test_clear_resets_history_to_system_message(self):
        agent = ChatAgent(FakeClient(chunks=chunks_of(["Ок"])))
        agent.ask("Привет")
        agent.clear()
        history = agent.history
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["role"], "system")
        self.assertEqual(history[0]["content"], AgentConfig().system_prompt)

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
        agent = ChatAgent(client, config)
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


if __name__ == "__main__":
    unittest.main()
