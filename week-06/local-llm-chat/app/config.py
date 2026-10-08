"""Application configuration for D26 (own settings, independent of the Center).

D28 gives a complete ``AI_TEST_MODEL_*`` profile priority over standalone
settings for generation and query rewrite. Invalid profiles block generation. A real ``.env`` is never read
in unit/integration tests; ``load_env_file``/``apply_env_file`` are explicit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
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
    "RAG_MAX_CONTEXT_CHARS": "12000",
    "UI_TITLE": "Local LLM Chat",
    "GGUF_DIR": r"D:\AI\Models\LLM",
    "DEEPSEEK_MODELS": "",
    # Week-05 knowledge-agent index path (read-only RAG source).
    "WEEK05_INDEX_PATH": str(Path(__file__).resolve().parents[3] / "week-05/knowledge-agent/local-data/index.db"),
}

# D28: defaults for the external test profile keys.  These are never written
# back to DEFAULTS; they only provide fallback values when AI_TEST_MODEL_*
# environment variables are absent.
TEST_MODEL_DEFAULTS = {
    "KIND": "",
    "BASE_URL": "",
    "NAME": "",
    "API_KEY": "",
    "ID": "",
    "PATH": "",
    "LEASE_URL": "",
    "LEASE_ID": "",
    "PARENT_READY": "0",
}

# The application's provider/base/model settings must never be taken from the
# test-runner profile; this tuple documents the protected names.
TEST_PROFILE_PREFIX = "AI_TEST_MODEL_"

PROVIDER_LOCAL = "local"
PROVIDER_NETWORK = "network"
PROVIDER_EXTERNAL = "external"


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
    rag_max_context_chars: int = 12000
    ui_title: str = "Local LLM Chat"
    week05_index_path: str = str(Path(__file__).resolve().parents[3] / "week-05/knowledge-agent/local-data/index.db")
    # D28: directory where GGUF models live (derived from GEMMA_GGUF_PATH parent).
    gguf_dir: str = ""
    deepseek_models: tuple[str, ...] = ()
    # D28: external test profile fields.
    test_model_kind: str = ""
    test_model_base_url: str = ""
    test_model_name: str = ""
    test_model_api_key: str = field(default="", repr=False)
    test_model_id: str = ""
    test_model_path: str = field(default="", repr=False)
    test_model_lease_url: str = ""
    test_model_lease_id: str = ""
    test_model_parent_ready: bool = False

    def missing_network_config(self) -> list[str]:
        """Names of missing network-provider settings; never their values."""
        required = {
            "DEEPSEEK_BASE_URL": self.deepseek_base_url,
            "DEEPSEEK_MODEL_ID": self.deepseek_model_id,
            "DEEPSEEK_API_KEY": self.deepseek_api_key,
        }
        return [name for name, value in required.items() if not str(value).strip()]

    # -- D28 external profile helpers -----------------------------------

    def external_profile_present(self) -> bool:
        return any((self.test_model_kind, self.test_model_base_url, self.test_model_name,
                    self.test_model_api_key, self.test_model_id, self.test_model_lease_url,
                    self.test_model_lease_id, self.test_model_parent_ready))

    def external_profile_errors(self) -> list[str]:
        if not self.external_profile_present():
            return []
        errors = self.external_profile_missing()
        if self.test_model_kind and self.test_model_kind not in ("local", "remote"):
            errors.append("AI_TEST_MODEL_KIND must be local or remote")
        try:
            url = urlsplit(self.test_model_base_url)
            if self.test_model_base_url and (url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment):
                errors.append("AI_TEST_MODEL_BASE_URL must be an HTTP API base URL")
        except ValueError:
            errors.append("AI_TEST_MODEL_BASE_URL is invalid")
        return errors

    def external_profile_complete(self) -> bool:
        """Return ``True`` when KIND, BASE_URL, NAME, and API_KEY are all non-empty."""
        return not self.external_profile_errors() and bool(
            self.test_model_kind.strip()
            and self.test_model_base_url.strip()
            and self.test_model_name.strip()
            and self.test_model_api_key.strip()
        )

    def external_profile_missing(self) -> list[str]:
        """Names of missing external-profile parameters (no secret values)."""
        required = {
            "AI_TEST_MODEL_KIND": self.test_model_kind,
            "AI_TEST_MODEL_BASE_URL": self.test_model_base_url,
            "AI_TEST_MODEL_NAME": self.test_model_name,
            "AI_TEST_MODEL_API_KEY": self.test_model_api_key,
        }
        return [name for name, value in required.items() if not str(value).strip()]

    def external_profile_partial(self) -> bool:
        """Return ``True`` when at least one external param is set but not all.

        Distinguishes a deliberate partial profile from a completely absent one
        so that a hidden Gemma fallback is never used (R3).
        """
        required = {
            "AI_TEST_MODEL_KIND": self.test_model_kind,
            "AI_TEST_MODEL_BASE_URL": self.test_model_base_url,
            "AI_TEST_MODEL_NAME": self.test_model_name,
            "AI_TEST_MODEL_API_KEY": self.test_model_api_key,
        }
        present = sum(1 for v in required.values() if str(v).strip())
        return self.external_profile_present() and bool(self.external_profile_errors())


def _value(env: Mapping[str, str], key: str) -> str:
    raw = env.get(key)
    if raw is None or raw == "":
        return DEFAULTS[key]
    return str(raw)


def _flag(env: Mapping[str, str], key: str, default: str = "0") -> bool:
    raw = env.get(key)
    if raw is None or raw == "":
        return default.strip().lower() in ("1", "true", "yes", "on")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _test_model_key(env_key: str) -> str:
    """Strip the ``AI_TEST_MODEL_`` prefix and return the internal Settings field name."""
    return env_key[len("AI_TEST_MODEL_"):]


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build settings from an explicit mapping (defaults merged).

    ``AI_TEST_MODEL_*`` entries are read into the external-test-profile
    fields of Settings (D28).  Own D26 keys are read as before.
    """
    source = env if env is not None else os.environ

    # Collect AI_TEST_MODEL_* values from source.
    test_model_values: dict[str, str] = {}
    for key, value in source.items():
        if key.startswith(TEST_PROFILE_PREFIX):
            internal = _test_model_key(key)
            if internal in TEST_MODEL_DEFAULTS:
                test_model_values[internal] = value

    kind = test_model_values.get("KIND", TEST_MODEL_DEFAULTS["KIND"])
    base_url = test_model_values.get("BASE_URL", TEST_MODEL_DEFAULTS["BASE_URL"])
    name = test_model_values.get("NAME", TEST_MODEL_DEFAULTS["NAME"])
    api_key = test_model_values.get("API_KEY", TEST_MODEL_DEFAULTS["API_KEY"])
    model_id = test_model_values.get("ID", TEST_MODEL_DEFAULTS["ID"])
    lease_url = test_model_values.get("LEASE_URL", TEST_MODEL_DEFAULTS["LEASE_URL"])
    lease_id = test_model_values.get("LEASE_ID", TEST_MODEL_DEFAULTS["LEASE_ID"])
    parent_ready = test_model_values.get("PARENT_READY", TEST_MODEL_DEFAULTS["PARENT_READY"])

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
        rag_max_context_chars=int(_value(source, "RAG_MAX_CONTEXT_CHARS")),
        ui_title=_value(source, "UI_TITLE"),
        week05_index_path=_value(source, "WEEK05_INDEX_PATH"),
        gguf_dir=_value(source, "GGUF_DIR"),
        deepseek_models=tuple(dict.fromkeys([_value(source, "DEEPSEEK_MODEL_ID")] + [x.strip() for x in _value(source, "DEEPSEEK_MODELS").split(",") if x.strip()])),
        test_model_kind=kind,
        test_model_base_url=base_url,
        test_model_name=name,
        test_model_api_key=api_key,
        test_model_id=model_id,
        test_model_path=test_model_values.get("PATH", ""),
        test_model_lease_url=lease_url,
        test_model_lease_id=lease_id,
        test_model_parent_ready=_flag(source, "AI_TEST_MODEL_PARENT_READY", "0"),
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
