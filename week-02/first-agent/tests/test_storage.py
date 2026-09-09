import os
import tempfile
import unittest

from agent import AgentConfig
from storage import ChatStore


class ChatStoreTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)

    def test_schema_init_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            ChatStore(path)
            ChatStore(path)  # second init on the same path must not fail

    def test_create_and_list_ordering(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            self.assertEqual(store.list_chats(), [])
            first = store.create_chat(AgentConfig())
            second = store.create_chat(AgentConfig())
            self.assertEqual([chat.id for chat in store.list_chats()], [second, first])

    def test_save_turn_writes_messages_and_derives_title_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            store.save_turn(chat_id, "Первое сообщение", "Ответ 1")
            store.save_turn(chat_id, "Второе сообщение", "Ответ 2")

            messages = store.load_messages(chat_id)
            self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user", "assistant"])
            self.assertEqual(messages[0]["content"], "Первое сообщение")
            self.assertEqual(messages[1]["content"], "Ответ 1")
            # Title is derived from the first message and never overwritten.
            self.assertEqual(store.list_chats()[0].title, "Первое сообщение")

    def test_title_truncated_at_40_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            long_text = "x" * 50
            store.save_turn(chat_id, long_text, "Ответ")
            self.assertEqual(store.list_chats()[0].title, "x" * 40 + "…")

    def test_multi_chat_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            a = store.create_chat(AgentConfig(system_prompt="Промпт A"))
            b = store.create_chat(AgentConfig(system_prompt="Промпт B"))
            store.save_turn(a, "Вопрос A", "Ответ A")
            store.save_turn(b, "Вопрос B", "Ответ B")

            self.assertEqual([m["content"] for m in store.load_messages(a)], ["Вопрос A", "Ответ A"])
            self.assertEqual([m["content"] for m in store.load_messages(b)], ["Вопрос B", "Ответ B"])
            self.assertEqual(store.load_config(a)["system_prompt"], "Промпт A")
            self.assertEqual(store.load_config(b)["system_prompt"], "Промпт B")

    def test_save_load_config_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())

            config = AgentConfig(
                system_prompt="Промпт",
                model="custom",
                temperature=0.9,
                max_tokens=256,
                stream=False,
            )
            store.save_config(chat_id, config)
            loaded = store.load_config(chat_id)
            self.assertEqual(loaded["system_prompt"], "Промпт")
            self.assertEqual(loaded["model"], "custom")
            self.assertAlmostEqual(loaded["temperature"], 0.9)
            self.assertEqual(loaded["max_tokens"], 256)
            self.assertFalse(loaded["stream"])

            config.stream = True
            store.save_config(chat_id, config)
            self.assertTrue(store.load_config(chat_id)["stream"])

    def test_delete_chat_removes_messages_and_last_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            store.save_turn(chat_id, "Вопрос", "Ответ")
            store.set_last_selected(chat_id)
            self.assertEqual(store.get_last_selected_id(), chat_id)

            store.delete_chat(chat_id)
            self.assertEqual(store.load_messages(chat_id), [])
            self.assertIsNone(store.get_last_selected_id())

    def test_last_selected_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            self.assertIsNone(store.get_last_selected_id())
            store.set_last_selected(42)
            self.assertEqual(store.get_last_selected_id(), 42)
            store.set_last_selected(43)
            self.assertEqual(store.get_last_selected_id(), 43)

    def test_usage_accumulation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            self.assertEqual(store.get_usage(chat_id), {"input_tokens": None, "output_tokens": None})

            store.save_turn(chat_id, "Вопрос", "Ответ", input_tokens=5, output_tokens=10)
            self.assertEqual(store.get_usage(chat_id), {"input_tokens": 5, "output_tokens": 10})

            # Partial update: only input reported.
            store.save_turn(chat_id, "Вопрос 2", "Ответ 2", input_tokens=3)
            self.assertEqual(store.get_usage(chat_id), {"input_tokens": 8, "output_tokens": 10})

            store.save_turn(chat_id, "Вопрос 3", "Ответ 3", input_tokens=2, output_tokens=4)
            self.assertEqual(store.get_usage(chat_id), {"input_tokens": 10, "output_tokens": 14})


if __name__ == "__main__":
    unittest.main()
