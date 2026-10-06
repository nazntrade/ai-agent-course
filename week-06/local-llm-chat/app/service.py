"""Chat service: provider switching, RAG/no-RAG, history and concurrency.

Serializes provider switches (SPEC 5.6, R4.8), stores the factual answer model
(R5.7), keeps history across switches (R5.6) and isolates delayed answers from a
different dialogue/provider (I10). No-RAG never attaches document sources (I8).
"""

from __future__ import annotations

import threading
from typing import Any, Mapping

from .config import PROVIDER_LOCAL, PROVIDER_NETWORK, Settings
from .context.builder import ContextBuilder
from .dialogues.store import SqliteDialogueStore
from .errors import (
    LocalProcessError,
    ProviderError,
    ProviderNotConfigured,
)
from .local_process.gemma_manager import GemmaProcessManager
from .providers.base import AnswerProvider, ChatMessage
from .providers.local_llama import LocalLlamaProvider
from .providers.network_deepseek import DeepSeekProvider
from .rag.embedder import OllamaEmbedder
from .rag.citations import CitationVerifier
from .rag.rewrite import ChatQueryRewriter, RewriteResult, no_rewrite
from .rag.store import RagStore

STATE_KEY_PROVIDER = "selected_provider"


class ChatService:
    def __init__(
        self,
        settings: Settings,
        *,
        local_provider: AnswerProvider | None = None,
        network_provider: AnswerProvider | None = None,
        gemma_manager: GemmaProcessManager | None = None,
        dialogue_store: SqliteDialogueStore | None = None,
        rag_store: RagStore | None = None,
        embedder: OllamaEmbedder | None = None,
        context_builder: ContextBuilder | None = None,
        query_rewriter: Any | None = None,
    ) -> None:
        self.settings = settings
        self.local_provider = local_provider or LocalLlamaProvider(
            base_url=f"http://{settings.host}:{settings.gemma_port}",
            model_id=settings.gemma_model_id,
            timeout=settings.gemma_request_timeout_seconds,
            max_output_tokens=settings.gemma_max_output_tokens,
            temperature=settings.gemma_temperature,
        )
        self.network_provider = network_provider or DeepSeekProvider(
            base_url=settings.deepseek_base_url,
            model_id=settings.deepseek_model_id,
            api_key=settings.deepseek_api_key,
            timeout=settings.deepseek_timeout_seconds,
            max_output_tokens=settings.deepseek_max_output_tokens,
            temperature=settings.deepseek_temperature,
        )
        self.gemma = gemma_manager or GemmaProcessManager(
            runtime_path=settings.gemma_runtime_path,
            gguf_path=settings.gemma_gguf_path,
            model_id=settings.gemma_model_id,
            host=settings.host,
            port=settings.gemma_port,
            context_tokens=settings.gemma_context_tokens,
            load_timeout=settings.gemma_load_timeout_seconds,
            extra_args=settings.gemma_extra_args,
        )
        self.dialogues = dialogue_store or SqliteDialogueStore(settings.dialogue_db_path)
        self.rag = rag_store
        self.embedder = embedder
        self.context = context_builder or ContextBuilder()
        # The rewriter uses whichever answer provider is selected at call time
        # unless an explicit rewriter is injected for tests.
        self._query_rewriter = query_rewriter
        self._switch_lock = threading.RLock()
        self._generation_lock = threading.RLock()
        # Bumped on every provider switch; a generation that finishes under a
        # different epoch is stale and must not be treated as an active answer.
        self._generation_epoch = 0

    # -- provider selection ----------------------------------------------
    def selected_provider(self) -> str:
        stored = self.dialogues.get_state(STATE_KEY_PROVIDER, PROVIDER_LOCAL)
        return stored if stored in (PROVIDER_LOCAL, PROVIDER_NETWORK) else PROVIDER_LOCAL

    def provider_for(self, name: str) -> AnswerProvider:
        return self.local_provider if name == PROVIDER_LOCAL else self.network_provider

    def select_provider(self, name: str, *, dialogue_id: str | None = None) -> dict[str, Any]:
        """Serialize a provider switch; switching to network stops only own Gemma."""
        if name not in (PROVIDER_LOCAL, PROVIDER_NETWORK):
            raise ProviderError("Unknown provider.", details={"provider": name})
        with self._switch_lock:
            self._generation_epoch += 1
            if name == PROVIDER_NETWORK:
                # Always stops only the process owned by this service (R4.4).
                self.gemma.stop()
            self.dialogues.set_state(STATE_KEY_PROVIDER, name)
            if dialogue_id is not None:
                self.dialogues.set_state(STATE_KEY_PROVIDER + ":active_dialogue", dialogue_id)
            return self.provider_state()

    def provider_state(self) -> dict[str, Any]:
        selected = self.selected_provider()
        local_state = self.gemma.state_snapshot()
        state = local_state
        if selected == PROVIDER_NETWORK:
            network_status = self.network_provider.status()
            return {
                "selected": selected,
                "local_state": local_state["state"],
                "local_pid": local_state["pid"],
                "local_error": local_state["error"],
                "network_reachable": network_status.reachable,
                "network_detail": network_status.detail,
                "missing_config": self.network_provider.missing_config(),
            }
        return {
            "selected": selected,
            "local_state": state["state"],
            "local_pid": state["pid"],
            "local_error": state["error"],
            "network_reachable": None,
            "network_detail": None,
            "missing_config": [],
        }

    def ensure_local_ready(self) -> dict[str, Any]:
        """Idempotent local start; declares ready only on confirmed availability."""
        with self._switch_lock:
            return self.gemma.ensure_started()

    def unload_local(self) -> dict[str, Any]:
        with self._switch_lock:
            return self.gemma.unload()

    # -- generation -------------------------------------------------------
    def ask(
        self,
        dialogue_id: str,
        question: str,
        *,
        rag_enabled: bool = False,
        provider: str | None = None,
        use_rewrite: bool = False,
        min_score: float | None = None,
        top_k: int | None = None,
    ) -> dict[str, Any]:
        name = provider or self.selected_provider()
        answer_provider = self.provider_for(name)

        # Record the user turn first so history survives any provider failure.
        self.dialogues.append_message(
            {"dialogue_id": dialogue_id, "role": "user", "text": question}
        )

        fragments: list[dict[str, Any]] = []
        rewrite: RewriteResult = no_rewrite(question)
        if rag_enabled:
            if self.rag is None or self.embedder is None:
                raise ProviderError("RAG is enabled but retrieval is not configured.")
            if use_rewrite:
                rewriter = self._query_rewriter or ChatQueryRewriter(answer_provider)
                rewrite = rewriter.rewrite(question)
            search_query = rewrite.search_query
            query_vector = self.embedder.embed_query(search_query)
            fragments = self.rag.search(
                query_vector, top_k=top_k or self.settings.rag_top_k, min_score=min_score
            )

        history = self.dialogues.list_messages(dialogue_id)[:-1]  # exclude the just-added user turn
        messages, trace = self.context.build(
            question=question,
            history=history,
            fragments=fragments if rag_enabled else [],
            rag_enabled=rag_enabled,
        )

        with self._generation_lock:
            epoch = self._generation_epoch
            if name == PROVIDER_LOCAL:
                self.gemma.ensure_started()
                self.gemma.set_generating(True)
            try:
                result = answer_provider.chat(messages)
            except ProviderNotConfigured:
                self._record_error(dialogue_id, name, "provider_not_configured")
                raise
            except Exception as exc:  # noqa: BLE001 - record and re-raise typed errors
                self._record_error(dialogue_id, name, type(exc).__name__)
                raise
            finally:
                if name == PROVIDER_LOCAL:
                    self.gemma.set_generating(False)

            # A switch happened while generating: do not write into a foreign
            # active dialogue (I10). The message is still stored on its own
            # dialogue, which is the correct owner.
            if epoch != self._generation_epoch:
                self._record_error(dialogue_id, name, "stale_generation")
                raise ProviderError("The provider changed during generation; answer discarded.")

        stored = self.dialogues.append_message(
            {
                "dialogue_id": dialogue_id,
                "role": "assistant",
                "text": result.text,
                "provider": name,
                "model": result.model,
                "usage": result.usage.to_dict() if result.usage else None,
                "finish_reason": result.finish_reason,
                "latency_ms": result.latency_ms,
                "parameters": result.parameters,
            }
        )
        citations = None
        if rag_enabled:
            # Verify that any inline citations refer to really passed fragments.
            citations = CitationVerifier(fragments).verify(result.text).to_dict()
        return {
            "dialogue_id": dialogue_id,
            "provider": name,
            "answer": result.to_dict(),
            "rag": {
                "enabled": rag_enabled,
                "sources": trace["sources"] if rag_enabled else [],
                "rewrite": rewrite.to_dict() if rag_enabled else None,
                "citations": citations,
            },
            "message_id": stored["message_id"],
        }

    def _record_error(self, dialogue_id: str, provider: str, code: str) -> None:
        self.dialogues.append_message(
            {
                "dialogue_id": dialogue_id,
                "role": "assistant",
                "text": "",
                "provider": provider,
                "model": None,
                "is_error": True,
                "parameters": {"error_code": code},
            }
        )

    # -- RAG helpers ------------------------------------------------------
    def add_document(self, label: str, text: str) -> dict[str, Any]:
        if self.rag is None or self.embedder is None:
            raise ProviderError("Retrieval is not configured.")
        from .rag.store import chunk_text

        chunks = chunk_text(text)
        vectors = self.embedder.embed_documents(chunks) if chunks else []
        return self.rag.add_document(label=label, public_uri=label, chunks=chunks, vectors=vectors)

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        min_score: float | None = None,
        use_rewrite: bool = False,
        provider: str | None = None,
    ) -> dict[str, Any]:
        if self.rag is None or self.embedder is None:
            raise ProviderError("Retrieval is not configured.")
        rewrite: RewriteResult = no_rewrite(query)
        if use_rewrite:
            name = provider or self.selected_provider()
            rewriter = self._query_rewriter or ChatQueryRewriter(self.provider_for(name))
            rewrite = rewriter.rewrite(query)
        vector = self.embedder.embed_query(rewrite.search_query)
        results = self.rag.search(vector, top_k=top_k, min_score=min_score)
        return {"query": query, "search_query": rewrite.search_query, "rewrite": rewrite.to_dict(), "results": results}

    def compare(
        self,
        query: str,
        *,
        top_k: int = 3,
        min_score: float | None = None,
        use_rewrite: bool = False,
        provider: str | None = None,
    ) -> dict[str, Any]:
        if self.rag is None or self.embedder is None:
            raise ProviderError("Retrieval is not configured.")
        rewrite: RewriteResult = no_rewrite(query)
        if use_rewrite:
            name = provider or self.selected_provider()
            rewriter = self._query_rewriter or ChatQueryRewriter(self.provider_for(name))
            rewrite = rewriter.rewrite(query)
        vector = self.embedder.embed_query(rewrite.search_query)
        comparison = self.rag.compare(vector, top_k=top_k, min_score=min_score)
        comparison["query"] = query
        comparison["search_query"] = rewrite.search_query
        comparison["rewrite"] = rewrite.to_dict()
        return comparison

    # -- health -----------------------------------------------------------
    def health(self) -> dict[str, Any]:
        return {
            "provider": self.selected_provider(),
            "local_state": self.gemma.state,
            "dialogue_store": "sqlite",
        }

    def close(self) -> None:
        try:
            self.gemma.shutdown()
        finally:
            self.dialogues.close()
            if self.rag is not None:
                self.rag.close()
