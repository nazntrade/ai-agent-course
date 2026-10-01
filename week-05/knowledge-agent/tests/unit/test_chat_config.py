"""D22 ``CHAT_*`` settings load without touching ``.env``."""

from __future__ import annotations

from knowledge_agent.config import load_settings


def test_chat_defaults_do_not_guess_a_model():
    settings = load_settings({})
    assert settings.chat_base_url == "http://127.0.0.1:11434"
    assert settings.chat_model == ""
    assert settings.chat_timeout_seconds == 120.0
    assert settings.chat_max_output_tokens == 1024
    assert settings.chat_context_tokens == 8192
    assert settings.chat_top_k == 5
    assert settings.chat_context_chars_per_token == 3
    assert settings.chat_runs_path == "local-data/chat-runs"


def test_chat_explicit_mapping_overrides_defaults():
    settings = load_settings(
        {
            "CHAT_MODEL": "llama3.2:3b",
            "CHAT_BASE_URL": "http://127.0.0.1:11435",
            "CHAT_CONTEXT_TOKENS": "4096",
            "CHAT_MAX_OUTPUT_TOKENS": "256",
            "CHAT_TOP_K": "3",
        }
    )
    assert settings.chat_model == "llama3.2:3b"
    assert settings.chat_base_url == "http://127.0.0.1:11435"
    assert settings.chat_context_tokens == 4096
    assert settings.chat_max_output_tokens == 256
    assert settings.chat_top_k == 3
