"""Configuration loading without touching the real ``.env`` (SPEC 13)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

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
    )


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
