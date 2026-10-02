"""Configuration loading without touching the real ``.env`` (SPEC 13)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from .chat.test_profile import TestProfile, load_test_profile

DEFAULTS = {
    "KNOWLEDGE_HOST": "127.0.0.1",
    "KNOWLEDGE_PORT": "8770",
    "KNOWLEDGE_DB_PATH": "local-data/index.db",
    "KNOWLEDGE_SOURCE_PATH": "",
    "EMBED_BASE_URL": "http://127.0.0.1:11434",
    "EMBED_MODEL": "embeddinggemma:300m",
    "EMBED_BATCH_SIZE": "16",
    "EMBED_TIMEOUT_SECONDS": "60",
    "EMBED_DOCUMENT_PREFIX": "",
    "EMBED_QUERY_PREFIX": "",
    "CHUNK_SIZE": "500",
    "CHUNK_OVERLAP": "75",
    "CHUNK_STRUCTURE_MAX_TOKENS": "800",
    "CHUNK_STRUCTURE_MIN_TOKENS": "64",
    "CHUNK_STRUCTURE_MAX_CHARS": "3200",
    "PDF_USEFUL_PAGE_MIN_CHARS": "500",
    "KNOWLEDGE_UI_TITLE": "Knowledge Agent",
    # D22 chat generation (SPEC D22 8). CHAT_MODEL is empty on purpose: the app
    # never guesses a model and never downloads one.
    "CHAT_BASE_URL": "http://127.0.0.1:11434",
    "CHAT_MODEL": "",
    "CHAT_TIMEOUT_SECONDS": "120",
    "CHAT_MAX_OUTPUT_TOKENS": "1024",
    "CHAT_CONTEXT_TOKENS": "8192",
    "CHAT_TEMPERATURE": "0",
    "CHAT_SEED": "0",
    "CHAT_TOP_K": "5",
    "CHAT_CONTEXT_CHARS_PER_TOKEN": "3",
    "CHAT_RUNS_PATH": "local-data/chat-runs",
    # Day 23 retrieval filter and query rewrite (SPEC D23 10). Defaults keep the
    # D22 behaviour: filtering and rewrite stay off unless explicitly requested.
    "RAG_FILTER_ENABLED": "0",
    "RAG_MIN_SCORE": "0.0",
    "RAG_PREFILTER_TOP_K": "20",
    "RAG_FILTER_TOP_K": "5",
    "RAG_REWRITE_ENABLED": "0",
    "RAG_REWRITE_MAX_OUTPUT_TOKENS": "1024",
    "RAG_REWRITE_TIMEOUT_SECONDS": "30",
    "RAG_REWRITE_TEMPERATURE": "0",
    "D23_DATA_PATH": "local-data/d23",
    "D23_CALIBRATION_QUESTIONS_PATH": "eval/d23/calibration-questions.json",
}


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    db_path: str
    source_path: str
    embed_base_url: str
    embed_model: str
    embed_batch_size: int
    embed_timeout_seconds: float
    document_prefix: str
    query_prefix: str
    chunk_size: int
    chunk_overlap: int
    structure_max_tokens: int
    structure_min_tokens: int
    structure_max_chars: int
    pdf_useful_page_min_chars: int
    ui_title: str
    # D22 chat generation. Defaults keep the existing explicit ``Settings(...)``
    # constructions in harness checks working without changes.
    test_profile: TestProfile | None = None
    chat_base_url: str = "http://127.0.0.1:11434"
    chat_model: str = ""
    chat_timeout_seconds: float = 120.0
    chat_max_output_tokens: int = 1024
    chat_context_tokens: int = 8192
    chat_temperature: float = 0.0
    chat_seed: int = 0
    chat_top_k: int = 5
    chat_context_chars_per_token: int = 3
    chat_runs_path: str = "local-data/chat-runs"
    # Day 23 (SPEC D23 10). Defaults are appended so existing explicit
    # ``Settings(...)`` constructions keep working without changes.
    rag_filter_enabled: bool = False
    rag_min_score: float = 0.0
    rag_prefilter_top_k: int = 20
    rag_filter_top_k: int = 5
    rag_rewrite_enabled: bool = False
    rag_rewrite_max_output_tokens: int = 1024
    rag_rewrite_timeout_seconds: float = 30.0
    rag_rewrite_temperature: float = 0.0
    d23_data_path: str = "local-data/d23"
    d23_calibration_questions_path: str = "eval/d23/calibration-questions.json"


def _value(env: Mapping[str, str], key: str) -> str:
    raw = env.get(key)
    if raw is None or raw == "":
        return DEFAULTS[key]
    return raw


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build settings from a mapping (defaults merged in, no ``.env`` read)."""

    source = env if env is not None else os.environ
    return Settings(
        host=_value(source, "KNOWLEDGE_HOST"),
        port=int(_value(source, "KNOWLEDGE_PORT")),
        db_path=_value(source, "KNOWLEDGE_DB_PATH"),
        source_path=_value(source, "KNOWLEDGE_SOURCE_PATH"),
        embed_base_url=_value(source, "EMBED_BASE_URL"),
        embed_model=_value(source, "EMBED_MODEL"),
        embed_batch_size=int(_value(source, "EMBED_BATCH_SIZE")),
        embed_timeout_seconds=float(_value(source, "EMBED_TIMEOUT_SECONDS")),
        document_prefix=_value(source, "EMBED_DOCUMENT_PREFIX"),
        query_prefix=_value(source, "EMBED_QUERY_PREFIX"),
        chunk_size=int(_value(source, "CHUNK_SIZE")),
        chunk_overlap=int(_value(source, "CHUNK_OVERLAP")),
        structure_max_tokens=int(_value(source, "CHUNK_STRUCTURE_MAX_TOKENS")),
        structure_min_tokens=int(_value(source, "CHUNK_STRUCTURE_MIN_TOKENS")),
        structure_max_chars=int(_value(source, "CHUNK_STRUCTURE_MAX_CHARS")),
        pdf_useful_page_min_chars=int(_value(source, "PDF_USEFUL_PAGE_MIN_CHARS")),
        ui_title=_value(source, "KNOWLEDGE_UI_TITLE"),
        test_profile=load_test_profile(source),
        chat_base_url=_value(source, "CHAT_BASE_URL"),
        chat_model=_value(source, "CHAT_MODEL"),
        chat_timeout_seconds=float(_value(source, "CHAT_TIMEOUT_SECONDS")),
        chat_max_output_tokens=int(_value(source, "CHAT_MAX_OUTPUT_TOKENS")),
        chat_context_tokens=int(_value(source, "CHAT_CONTEXT_TOKENS")),
        chat_temperature=float(_value(source, "CHAT_TEMPERATURE")),
        chat_seed=int(_value(source, "CHAT_SEED")),
        chat_top_k=int(_value(source, "CHAT_TOP_K")),
        chat_context_chars_per_token=int(_value(source, "CHAT_CONTEXT_CHARS_PER_TOKEN")),
        chat_runs_path=_value(source, "CHAT_RUNS_PATH"),
        rag_filter_enabled=_flag(_value(source, "RAG_FILTER_ENABLED")),
        rag_min_score=float(_value(source, "RAG_MIN_SCORE")),
        rag_prefilter_top_k=int(_value(source, "RAG_PREFILTER_TOP_K")),
        rag_filter_top_k=int(_value(source, "RAG_FILTER_TOP_K")),
        rag_rewrite_enabled=_flag(_value(source, "RAG_REWRITE_ENABLED")),
        rag_rewrite_max_output_tokens=int(_value(source, "RAG_REWRITE_MAX_OUTPUT_TOKENS")),
        rag_rewrite_timeout_seconds=float(_value(source, "RAG_REWRITE_TIMEOUT_SECONDS")),
        rag_rewrite_temperature=float(_value(source, "RAG_REWRITE_TEMPERATURE")),
        d23_data_path=_value(source, "D23_DATA_PATH"),
        d23_calibration_questions_path=_value(source, "D23_CALIBRATION_QUESTIONS_PATH"),
    )


def _flag(raw: str) -> bool:
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def load_env_file(path: str | Path) -> dict[str, str]:
    """Read a minimal ``KEY=VALUE`` file and return the parsed mapping.

    Existing process environment variables are never overwritten, so explicit
    values (for example from a test runner) always win over ``.env``.
    """

    file_path = Path(path)
    if not file_path.is_file():
        return {}
    parsed: dict[str, str] = {}
    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
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
    """Load ``.env`` values that are not already present in the environment."""

    for key, value in load_env_file(path).items():
        os.environ.setdefault(key, value)
