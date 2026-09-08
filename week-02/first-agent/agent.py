"""First agent: encapsulates the LLM interaction and message history.

The module does not depend on Streamlit and can be tested in isolation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

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


class ChatAgent:
    """Agent that stores the dialog history and talks to the LLM via a client."""

    def __init__(self, client, config: AgentConfig | None = None):
        self._client = client
        self._config = config if config is not None else AgentConfig()
        self._history = [{"role": "system", "content": self._config.system_prompt}]

    @property
    def history(self):
        """A copy of the history so external code cannot mutate it."""
        return [dict(message) for message in self._history]

    @property
    def config(self):
        return self._config

    def clear(self):
        """Reset the history to a single system message."""
        self._history = [{"role": "system", "content": self._config.system_prompt}]

    def ask(self, user_message: str, on_chunk=None) -> str:
        """Send a message to the LLM and return the full answer.

        On any error the added user message is rolled back, the partial
        assistant reply is not stored in the history, and the exception is re-raised.
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

        try:
            response = self._client.chat.completions.create(**payload)
            collected = []
            for chunk in response:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.delta and choice.delta.content:
                    collected.append(choice.delta.content)
                    if on_chunk is not None:
                        on_chunk("".join(collected))
            answer = "".join(collected)
        except Exception:
            self._history.pop()
            raise

        self._history.append({"role": "assistant", "content": answer})
        return answer
