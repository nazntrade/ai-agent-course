"""Entry point: build settings/services and run the FastAPI app (SPEC R2.7).

Never reads the Center's configuration, model catalog or process state. Uses
only the application's own environment and (optionally) a local ``.env``.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import Settings, apply_env_file, load_settings
from .context.builder import ContextBuilder
from .dialogues.store import SqliteDialogueStore
from .local_process.gemma_manager import GemmaProcessManager
from .providers.external_http import ExternalHttpProvider
from .providers.local_llama import LocalLlamaProvider
from .providers.network_deepseek import DeepSeekProvider
from .rag.embedder import OllamaEmbedder
from .rag.external_index import ExternalIndex
from .rag.store import RagStore
from .service import ChatService

MODULE_DIR = Path(__file__).resolve().parent.parent


def _build_rag_store(settings: Settings, *, with_rag: bool = True):
    """Build the RAG store: prefer week-05 ExternalIndex when the DB exists."""
    if not with_rag:
        return None
    week05_path = settings.week05_index_path
    if week05_path and Path(week05_path).is_file():
        return ExternalIndex(week05_path, MODULE_DIR / "local-data" / "day28-reading-view.json")
    # Fallback to local in-memory-capable RagStore for tests.
    return RagStore(str(Path(settings.dialogue_db_path).with_name("index.db")))


def _skip_env_file(env: dict[str, str]) -> bool:
    markers = ("APP_SKIP_ENV_FILE", "KNOWLEDGE_SKIP_ENV_FILE")
    return any(str(env.get(name, "")).strip() in ("1", "true", "yes", "on") for name in markers)


def build_service(settings: Settings, *, with_rag: bool = True) -> ChatService:
    local = LocalLlamaProvider(
        base_url=f"http://{settings.host}:{settings.gemma_port}",
        model_id=settings.gemma_model_id,
        timeout=settings.gemma_request_timeout_seconds,
        max_output_tokens=settings.gemma_max_output_tokens,
        temperature=settings.gemma_temperature,
    )
    network = DeepSeekProvider(
        base_url=settings.deepseek_base_url,
        model_id=settings.deepseek_model_id,
        api_key=settings.deepseek_api_key,
        timeout=settings.deepseek_timeout_seconds,
        max_output_tokens=settings.deepseek_max_output_tokens,
        temperature=settings.deepseek_temperature,
    )
    gemma = GemmaProcessManager(
        runtime_path=settings.gemma_runtime_path,
        gguf_path=settings.gemma_gguf_path,
        model_id=settings.gemma_model_id,
        host=settings.host,
        port=settings.gemma_port,
        context_tokens=settings.gemma_context_tokens,
        load_timeout=settings.gemma_load_timeout_seconds,
        extra_args=settings.gemma_extra_args,
    )
    # D28: create external provider when the test profile is complete.
    external: ExternalHttpProvider | None = None
    if settings.external_profile_complete():
        external = ExternalHttpProvider(
            base_url=settings.test_model_base_url,
            model_id=settings.test_model_name,
            api_key=settings.test_model_api_key,
            timeout=settings.gemma_request_timeout_seconds,
            max_output_tokens=settings.gemma_max_output_tokens,
            lease_id=settings.test_model_lease_id,
            temperature=0.0,
        )

    return ChatService(
        settings,
        local_provider=local,
        network_provider=network,
        external_provider=external,
        gemma_manager=gemma,
        dialogue_store=SqliteDialogueStore(settings.dialogue_db_path),
        rag_store=_build_rag_store(settings, with_rag=with_rag),
        embedder=OllamaEmbedder(
            base_url=settings.embed_base_url,
            model=settings.embed_model,
            timeout=settings.embed_timeout_seconds,
        ) if with_rag else None,
        context_builder=ContextBuilder(max_context_chars=settings.rag_max_context_chars),
    )


def main() -> int:
    import uvicorn

    if not _skip_env_file(dict(os.environ)):
        apply_env_file(MODULE_DIR / ".env")
    settings = load_settings(os.environ)
    service = build_service(settings)
    from .api.app import create_app

    app = create_app(service, title=settings.ui_title)
    try:
        uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
