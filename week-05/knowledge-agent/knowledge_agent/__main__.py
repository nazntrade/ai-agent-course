"""Application entrypoint: ``python -m knowledge_agent``."""

from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path

from .api.app import create_app
from .chat.chat_service import ChatService
from .chat.ollama_chat import OllamaChatModel
from .chat.openai_chat import OpenAIChatModel
from .chat.run_store import FileChatRunStore
from .chunking.fixed import FixedChunker
from .chunking.structure import StructureChunker
from .config import Settings, apply_env_file, load_settings
from .embedding.ollama_embedder import OllamaEmbedder
from .service.knowledge_service import KnowledgeService
from .sources import DefaultSourceResolver
from .storage.sqlite_store import SqliteIndexStore
from .text.tokenizer import LexicalTokenizer

MODULE_DIR = Path(__file__).resolve().parent.parent


def build_service(settings: Settings) -> tuple[KnowledgeService, SqliteIndexStore]:
    db_path = Path(settings.db_path)
    if not db_path.is_absolute():
        db_path = MODULE_DIR / db_path
    store = SqliteIndexStore(db_path)
    # Hanging builds from a previous process become failed; ready/active stay intact.
    store.mark_stale_builds_failed("interrupted")

    embedder = OllamaEmbedder(
        settings.embed_base_url,
        settings.embed_model,
        batch_size=settings.embed_batch_size,
        timeout=settings.embed_timeout_seconds,
        document_prefix=settings.document_prefix,
        query_prefix=settings.query_prefix,
    )
    tokenizer = LexicalTokenizer()
    chunkers = {
        "fixed": FixedChunker(tokenizer, settings.chunk_size, settings.chunk_overlap),
        "structure": StructureChunker(
            tokenizer,
            max_tokens=settings.structure_max_tokens,
            min_tokens=settings.structure_min_tokens,
            overlap=0,
            max_chars=settings.structure_max_chars,
        ),
    }
    resolver = DefaultSourceResolver(settings.pdf_useful_page_min_chars)
    service = KnowledgeService(
        store,
        embedder,
        tokenizer,
        chunkers,
        resolver,
        batch_size=settings.embed_batch_size,
        embed_timeout_seconds=settings.embed_timeout_seconds,
    )
    return service, store


def build_chat_service(settings: Settings, service: KnowledgeService) -> ChatService:
    chat_model = OllamaChatModel(
        settings.chat_base_url,
        settings.chat_model,
        timeout=settings.chat_timeout_seconds,
        max_output_tokens=settings.chat_max_output_tokens,
        context_tokens=settings.chat_context_tokens,
        temperature=settings.chat_temperature,
        seed=settings.chat_seed,
    )
    if settings.test_profile is not None:
        chat_model = OpenAIChatModel(settings.test_profile,
            timeout=settings.chat_timeout_seconds,
            max_output_tokens=settings.chat_max_output_tokens,
            context_tokens=settings.chat_context_tokens,
            temperature=settings.chat_temperature, seed=settings.chat_seed)
    runs_path = Path(settings.chat_runs_path)
    if not runs_path.is_absolute():
        runs_path = MODULE_DIR / runs_path
    run_store = FileChatRunStore(runs_path)
    return ChatService(
        service,
        chat_model,
        run_store,
        top_k=settings.chat_top_k,
        max_context_tokens=settings.chat_context_tokens,
        reserved_output_tokens=settings.chat_max_output_tokens,
        chars_per_token=settings.chat_context_chars_per_token,
        temperature=settings.chat_temperature,
        seed=settings.chat_seed,
    )


def main() -> int:
    if os.environ.get("KNOWLEDGE_SKIP_ENV_FILE") != "1":
        apply_env_file(MODULE_DIR / ".env")
    settings = load_settings()
    db_path = settings.db_path
    if not Path(db_path).is_absolute():
        settings = dataclasses.replace(settings, db_path=str(MODULE_DIR / db_path))

    service, store = build_service(settings)
    chat_service = build_chat_service(settings, service)
    app = create_app(service, chat_service=chat_service, title=settings.ui_title)
    try:
        import uvicorn

        uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        return 0
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
