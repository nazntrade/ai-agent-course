"""First agent: encapsulates the LLM interaction and message history.

The module does not depend on Streamlit and can be tested in isolation.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

from dotenv import load_dotenv
from openai import OpenAI

BASE_URL = "https://api.deepseek.com"
API_KEY_ENV = "DEEPSEEK_API_KEY"

DEFAULT_SYSTEM_PROMPT = "Ты — полезный ассистент. Отвечай кратко и по делу."


def get_client():
    """Return a DeepSeek API client, or None if the key is not set."""
    load_dotenv()
    api_key = os.getenv(API_KEY_ENV)
    if not api_key:
        return None
    return OpenAI(api_key=api_key, base_url=BASE_URL)


@dataclass
class AgentConfig:
    """Agent configuration: model and generation parameters."""

    model: str = "deepseek-v4-flash"
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    temperature: float = 0.2
    max_tokens: int = 1500
    stream: bool = True


def _usage_parts(usage_obj) -> tuple[int | None, int | None]:
    """Extract (prompt_tokens, completion_tokens) from a usage-like object.

    Returns ``(None, None)`` when the object is absent or carries no token
    counts, so a provider that does not report usage is never invented.
    """
    if usage_obj is None:
        return None, None
    prompt = getattr(usage_obj, "prompt_tokens", None)
    completion = getattr(usage_obj, "completion_tokens", None)
    if prompt is None and completion is None:
        return None, None
    return prompt, completion


class ChatAgent:
    """Agent that stores the dialog history and talks to the LLM via a client.

    When ``store`` is provided the agent is bound to ``chat_id`` and persists
    history/configuration per chat. ``store`` is duck-typed and expected to
    expose::

        load_config(chat_id) -> dict
        save_config(chat_id, config) -> None
        load_messages(chat_id) -> list[{"role", "content"}]
        save_turn(chat_id, user_text, assistant_text,
                  input_tokens=None, output_tokens=None) -> None

    When ``store`` is None the agent keeps a pure in-memory history.
    """

    def __init__(self, client, store=None, chat_id=None, config=None):
        self._client = client
        self._store = store
        self._chat_id = chat_id
        if config is None and store is not None:
            config = AgentConfig(**store.load_config(chat_id))
        self._config = config if config is not None else AgentConfig()

        history = [{"role": "system", "content": self._config.system_prompt}]
        if store is not None and chat_id is not None:
            history.extend(store.load_messages(chat_id))
        self._history = history

    @property
    def history(self):
        """A copy of the history so external code cannot mutate it."""
        return [dict(message) for message in self._history]

    @property
    def config(self):
        """A copy of the config so external mutation cannot corrupt state."""
        return AgentConfig(**asdict(self._config))

    def set_config(self, config: AgentConfig):
        """Replace the configuration, persisting it when a store is bound."""
        self._config = config
        self._history[0]["content"] = config.system_prompt
        if self._store is not None:
            self._store.save_config(self._chat_id, config)

    def ask(self, user_message: str, on_chunk=None) -> str:
        """Send a message to the LLM and return the full answer.

        Supports both streaming and non-streaming modes:
        - ``stream=True``: iterate chunks, invoke ``on_chunk`` with the
          accumulated text, and capture usage from the final chunk.
        - ``stream=False``: single response; ``on_chunk`` is not called.

        On any error the added user message is rolled back, nothing is written
        to the store, and the exception is re-raised.
        """
        if not user_message.strip():
            raise ValueError("Message must not be empty")

        self._history.append({"role": "user", "content": user_message})

        payload = {
            "model": self._config.model,
            "messages": list(self._history),
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
            "stream": self._config.stream,
        }

        input_tokens = None
        output_tokens = None

        try:
            if self._config.stream:
                payload["stream_options"] = {"include_usage": True}
                response = self._client.chat.completions.create(**payload)
                collected = []
                for chunk in response:
                    usage = getattr(chunk, "usage", None)
                    if usage is not None:
                        input_tokens, output_tokens = _usage_parts(usage)
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    if choice.delta and choice.delta.content:
                        collected.append(choice.delta.content)
                        if on_chunk is not None:
                            on_chunk("".join(collected))
                answer = "".join(collected)
            else:
                response = self._client.chat.completions.create(**payload)
                answer = response.choices[0].message.content or ""
                input_tokens, output_tokens = _usage_parts(getattr(response, "usage", None))
        except Exception:
            self._history.pop()
            raise

        self._history.append({"role": "assistant", "content": answer})
        if self._store is not None:
            try:
                self._store.save_turn(
                    self._chat_id,
                    user_message,
                    answer,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
            except Exception:
                self._history.pop()
                self._history.pop()
                raise
        return answer
