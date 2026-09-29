"""load_settings and .env isolation (SPEC 13, I9)."""

from __future__ import annotations

import os

from knowledge_agent.config import apply_env_file, load_env_file, load_settings


def test_defaults_without_env():
    settings = load_settings({})
    assert settings.host == "127.0.0.1"
    assert settings.port == 8770
    assert settings.embed_model == "embeddinggemma:300m"
    assert settings.embed_batch_size == 16
    assert settings.pdf_useful_page_min_chars == 500
    assert settings.source_path == ""


def test_explicit_mapping_overrides_defaults():
    settings = load_settings(
        {
            "KNOWLEDGE_PORT": "9999",
            "EMBED_BATCH_SIZE": "4",
            "CHUNK_STRUCTURE_MAX_TOKENS": "1000",
        }
    )
    assert settings.port == 9999
    assert settings.embed_batch_size == 4
    assert settings.structure_max_tokens == 1000


def test_load_env_file_parses_without_secrets(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# comment\nKNOWLEDGE_PORT=8771\nEMBED_MODEL=embeddinggemma:300m\n\nINVALID\n",
        encoding="utf-8",
    )
    parsed = load_env_file(path)
    assert parsed == {"KNOWLEDGE_PORT": "8771", "EMBED_MODEL": "embeddinggemma:300m"}


def test_apply_env_file_does_not_override_process_env(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("KNOWLEDGE_PORT=8771\n", encoding="utf-8")
    monkeypatch.setenv("KNOWLEDGE_PORT", "8772")
    apply_env_file(path)
    assert os.environ["KNOWLEDGE_PORT"] == "8772"
