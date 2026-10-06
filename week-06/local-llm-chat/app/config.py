"""Application configuration for D26 (own settings, independent of the Center).

The application never lets the runner's ``AI_TEST_MODEL_*`` variables override
its own provider settings (SPEC R2.3, I3). Those variables are read only as a
separate test profile used by harness scenarios. A real ``.env`` is never read
in unit/integration tests; ``load_env_file``/``apply_env_file`` are explicit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

# Own D26 environment keys. They are deliberately distinct from AI_TEST_MODEL_*.
DEFAULTS = {
    "APP_HOST": "127.0.0.1",
    "APP_PORT": "8790",
    # Local Gemma runtime (SPEC R3.2).
    "GEMMA_RUNTIME_PATH": r"D:\AI\Runtimes\llama.cpp\b10809\llama-server.exe",
    "GEMMA_GGUF_PATH": r"D:\AI\Models\LLM\Gemma-4-12B-IT\gemma-4-12b-it-qat-q4_0.gguf",
    "GEMMA_MODEL_ID": "gemma-4-12b-it-qat-q4_0.gguf",
    "GEMMA_PORT": "8791",
    "GEMMA_CONTEXT_TOKENS": "8192",
    "GEMMA_MAX_OUTPUT_TOKENS": "1024",
    "GEMMA_TEMPERATURE": "0",
    "GEMMA_LOAD_TIMEOUT_SECONDS": "300",
    "GEMMA_REQUEST_TIMEOUT_SECONDS": "180",
    "GEMMA_EXTRA_ARGS": "",
    # Network DeepSeek (own server-side configuration; key only in .env).
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
    "DEEPSEEK_MODEL_ID": "deepseek-chat",
    "DEEPSEEK_API_KEY": "",
    "DEEPSEEK_TIMEOUT_SECONDS": "120",
    "DEEPSEEK_MAX_OUTPUT_TOKENS": "2048",
    "DEEPSEEK_TEMPERATURE": "0",
    # Dialogue storage.
    "DIALOGUE_DB_PATH": "local-data/conversations.db",
    # Retrieval / embedding (independent from the answer model; SPEC R5.4).
    "EMBED_BASE_URL": "http://127.0.0.1:11434",
    "EMBED_MODEL": "embeddinggemma:300m",
    "EMBED_TIMEOUT_SECONDS": "60",
    "RAG_TOP_K": "5",
    "UI_TITLE": "Local LLM Chat",
}

# The application's provider/base/model settings must never be taken from the
# test-runner profile; this tuple documents the protected names.
TEST_PROFILE_PREFIX = "AI_TEST_MODEL_"

PROVIDER_LOCAL = "local"
PROVIDER_NETWORK = "network"


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    gemma_runtime_path: str
    gemma_gguf_path: str
    gemma_model_id: str
    gemma_port: int
    gemma_context_tokens: int
    gemma_max_output_tokens: int
    gemma_temperature: float
    gemma_load_timeout_seconds: float
    gemma_request_timeout_seconds: float
    gemma_extra_args: str
    deepseek_base_url: str
    deepseek_model_id: str
    deepseek_api_key: str = field(default="", repr=False)
    deepseek_timeout_seconds: float = 120.0
    deepseek_max_output_tokens: int = 2048
    deepseek_temperature: float = 0.0
    dialogue_db_path: str = "local-data/conversations.db"
    embed_base_url: str = "http://127.0.0.1:11434"
    embed_model: str = "embeddinggemma:300m"
    embed_timeout_seconds: float = 60.0
    rag_top_k: int = 5
    ui_title: str = "Local LLM Chat"

    def missing_network_config(self) -> list[str]:
        """Names of missing network-provider settings; never their values."""
        required = {
            "DEEPSEEK_BASE_URL": self.deepseek_base_url,
            "DEEPSEEK_MODEL_ID": self.deepseek_model_id,
            "DEEPSEEK_API_KEY": self.deepseek_api_key,
        }
        return [name for name, value in required.items() if not str(value).strip()]


def _value(env: Mapping[str, str], key: str) -> str:
    raw = env.get(key)
    if raw is None or raw == "":
        return DEFAULTS[key]
    return str(raw)


def _flag(env: Mapping[str, str], key: str, default: str = "0") -> bool:
    return _value(env, key).strip().lower() in ("1", "true", "yes", "on")


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build settings from an explicit mapping (defaults merged, no ``.env`` read).

    ``AI_TEST_MODEL_*`` entries are intentionally ignored here.
    """
    source = env if env is not None else os.environ
    return Settings(
        host=_value(source, "APP_HOST"),
        port=int(_value(source, "APP_PORT")),
        gemma_runtime_path=_value(source, "GEMMA_RUNTIME_PATH"),
        gemma_gguf_path=_value(source, "GEMMA_GGUF_PATH"),
        gemma_model_id=_value(source, "GEMMA_MODEL_ID"),
        gemma_port=int(_value(source, "GEMMA_PORT")),
        gemma_context_tokens=int(_value(source, "GEMMA_CONTEXT_TOKENS")),
        gemma_max_output_tokens=int(_value(source, "GEMMA_MAX_OUTPUT_TOKENS")),
        gemma_temperature=float(_value(source, "GEMMA_TEMPERATURE")),
        gemma_load_timeout_seconds=float(_value(source, "GEMMA_LOAD_TIMEOUT_SECONDS")),
        gemma_request_timeout_seconds=float(_value(source, "GEMMA_REQUEST_TIMEOUT_SECONDS")),
        gemma_extra_args=_value(source, "GEMMA_EXTRA_ARGS"),
        deepseek_base_url=_value(source, "DEEPSEEK_BASE_URL"),
        deepseek_model_id=_value(source, "DEEPSEEK_MODEL_ID"),
        deepseek_api_key=_value(source, "DEEPSEEK_API_KEY"),
        deepseek_timeout_seconds=float(_value(source, "DEEPSEEK_TIMEOUT_SECONDS")),
        deepseek_max_output_tokens=int(_value(source, "DEEPSEEK_MAX_OUTPUT_TOKENS")),
        deepseek_temperature=float(_value(source, "DEEPSEEK_TEMPERATURE")),
        dialogue_db_path=_value(source, "DIALOGUE_DB_PATH"),
        embed_base_url=_value(source, "EMBED_BASE_URL"),
        embed_model=_value(source, "EMBED_MODEL"),
        embed_timeout_seconds=float(_value(source, "EMBED_TIMEOUT_SECONDS")),
        rag_top_k=int(_value(source, "RAG_TOP_K")),
        ui_title=_value(source, "UI_TITLE"),
    )


def load_env_file(path: str | Path) -> dict[str, str]:
    """Read a minimal ``KEY=VALUE`` file; never overrides process environment."""
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    parsed: dict[str, str] = {}
    for raw_line in file_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            parsed[key] = value
    return parsed


def apply_env_file(path: str | Path) -> None:
    """Load ``.env`` values not already present in the environment."""
    merged = {**load_env_file(Path(path).parent.parent / ".env"), **load_env_file(path)}
    for key, value in merged.items():
        if key in DEFAULTS:
            os.environ.setdefault(key, value)
