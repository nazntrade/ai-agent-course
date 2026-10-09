"""Chat service: provider switching, RAG/no-RAG, history and concurrency.

Serializes provider switches (SPEC 5.6, R4.8), stores the factual answer model
(R5.7), keeps history across switches (R5.6) and isolates delayed answers from a
different dialogue/provider (I10). No-RAG never attaches document sources (I8).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Mapping

from .config import PROVIDER_EXTERNAL, PROVIDER_LOCAL, PROVIDER_NETWORK, Settings
from .context.builder import ContextBuilder
from .dialogues.store import SqliteDialogueStore
from .errors import (
    LocalProcessError,
    ProviderError,
    ProviderNotConfigured,
)
from .local_process.gemma_manager import GemmaProcessManager
from .providers.base import AnswerProvider, ChatMessage, ChatResult
from .providers.external_http import ExternalHttpProvider
from .providers.local_llama import LocalLlamaProvider
from .providers.network_deepseek import DeepSeekProvider
from .rag.embedder import OllamaEmbedder
from .rag.citations import CitationVerifier
from .rag.grounded_answer import generate_grounded
from .rag.external_index import ExternalIndex
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
        external_provider: AnswerProvider | None = None,
        gemma_manager: GemmaProcessManager | None = None,
        dialogue_store: SqliteDialogueStore | None = None,
        rag_store: RagStore | ExternalIndex | None = None,
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
        # D28: optional external provider when AI_TEST_MODEL_* profile is set.
        self.external_provider = external_provider
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
        if isinstance(rag_store, ExternalIndex) and embedder is not None:
            rag_store.validate_embedder(embedder)
        self.context = context_builder or ContextBuilder(
            max_context_chars=settings.rag_max_context_chars
        )
        # The rewriter uses whichever answer provider is selected at call time
        # unless an explicit rewriter is injected for tests.
        self._query_rewriter = query_rewriter
        self._switch_lock = threading.RLock()
        self._generation_lock = threading.RLock()
        # Bumped on every provider switch; a generation that finishes under a
        # different epoch is stale and must not be treated as an active answer.
        self._generation_epoch = 0
        # Restore selection without loading a model during application startup.
        if not self.settings.external_profile_present():
            saved_local = self.dialogues.get_state("selected_local_model", "")
            if saved_local:
                root = Path(self.settings.gguf_dir).resolve()
                candidate = (root / saved_local).resolve()
                if candidate.is_relative_to(root) and candidate.is_file() and candidate.suffix.lower() == ".gguf" and not candidate.name.lower().startswith("mmproj"):
                    self.gemma.gguf_path = str(candidate)
                    self.gemma.model_id = saved_local
                    self.local_provider.model_id = saved_local
            saved_network = self.dialogues.get_state("selected_network_model", "")
            if saved_network in (self.settings.deepseek_models or (self.settings.deepseek_model_id,)):
                self.network_provider.model_id = saved_network

    # -- provider selection ----------------------------------------------

    @property
    def external_profile_active(self) -> bool:
        """Return ``True`` when a complete external profile is configured."""
        return self.settings.external_profile_complete()

    @property
    def external_profile_info(self) -> dict[str, Any]:
        """Return external profile metadata (kind, model, source)."""
        if not self.external_profile_active:
            return {}
        info: dict[str, Any] = {
            "kind": self.settings.test_model_kind,
            "model": self.settings.test_model_name,
            "source": "external_env",
        }
        if self.settings.test_model_path:
            info["model_file"] = Path(self.settings.test_model_path).name
        if self.settings.test_model_base_url:
            info["base_url"] = self.settings.test_model_base_url
        if self.settings.test_model_id:
            info["model_id"] = self.settings.test_model_id
        return info

    def selected_provider(self) -> str:
        stored = self.dialogues.get_state(STATE_KEY_PROVIDER, PROVIDER_LOCAL)
        if self.external_profile_active:
            return PROVIDER_EXTERNAL
        return stored if stored in (PROVIDER_LOCAL, PROVIDER_NETWORK) else PROVIDER_LOCAL

    def _validate_profile(self) -> None:
        errors = self.settings.external_profile_errors()
        if errors:
            raise ProviderError("External profile is invalid; no fallback is allowed.", details={"configuration_errors": errors})

    def provider_for(self, name: str) -> AnswerProvider:
        self._validate_profile()
        if name not in (PROVIDER_LOCAL, PROVIDER_NETWORK, PROVIDER_EXTERNAL):
            raise ProviderError("Unknown provider.")
        if name == PROVIDER_EXTERNAL:
            if self.external_provider is None:
                raise ProviderError("External profile is configured but no provider instance.")
            return self.external_provider
        return self.local_provider if name == PROVIDER_LOCAL else self.network_provider

    def select_provider(self, name: str, *, dialogue_id: str | None = None) -> dict[str, Any]:
        """Serialize a provider switch; switching to network stops only own Gemma."""
        self._check_optimization_busy()
        if self.external_profile_active:
            raise ProviderError(
                "Model selection is controlled by the external profile. "
                "Clear AI_TEST_MODEL_* variables to unlock selection.",
                details={"missing": self.settings.external_profile_missing()},
            )
        if name not in (PROVIDER_LOCAL, PROVIDER_NETWORK):
            raise ProviderError("Unknown provider.", details={"provider": name})
        # Signal cancellation before waiting for inference to finish; never stop
        # the owned descriptor until the shared lifecycle section is available.
        with self._switch_lock:
            self._check_optimization_busy()
            self._generation_epoch += 1
        with self._generation_lock, self._switch_lock:
            self._check_optimization_busy()
            if name == PROVIDER_NETWORK:
                # Always stops only the process owned by this service (R4.4).
                self.gemma.stop()
            self.dialogues.set_state(STATE_KEY_PROVIDER, name)
            if dialogue_id is not None:
                self.dialogues.set_state(STATE_KEY_PROVIDER + ":active_dialogue", dialogue_id)
            return self.provider_state()

    def select_model(self, model_id: str) -> dict[str, Any]:
        """Select a GGUF model by name (only when no external profile active).

        Only the application's own runtime is affected.
        Resolves the model name to an actual file path in the configured GGUF
        directory and restarts the local process with the new model.
        """
        self._check_optimization_busy()
        if self.external_profile_active:
            raise ProviderError(
                "Model selection is controlled by the external profile.",
            )
        self._validate_profile()
        with self._generation_lock, self._switch_lock:
            self._check_optimization_busy()
            self._generation_epoch += 1
            if model_id in (self.settings.deepseek_models or (self.settings.deepseek_model_id,)):
                if self.settings.missing_network_config():
                    raise ProviderNotConfigured("DeepSeek is not configured.", details={"missing": self.settings.missing_network_config()})
                self.gemma.stop()
                self.network_provider.model_id = model_id
                self.dialogues.set_state(STATE_KEY_PROVIDER, PROVIDER_NETWORK)
                self.dialogues.set_state("selected_network_model", model_id)
                return {"selected": model_id, "provider": PROVIDER_NETWORK}
            # Resolve model_id to an actual GGUF file path.
            model_dir = Path(self.settings.gguf_dir)
            candidate = (model_dir / model_id).resolve()
            if not candidate.is_relative_to(model_dir.resolve()) or candidate.suffix.lower() != ".gguf" or candidate.name.lower().startswith("mmproj") or not candidate.is_file():
                raise ProviderError(
                    "GGUF model file not found.",
                    details={"model": model_id, "searched_path": str(candidate)},
                )
            # Update internal gguf_path so the process uses the real file.
            self.gemma.stop()
            self.gemma.gguf_path = str(candidate)
            self.gemma.model_id = model_id
            # Also update the local provider's model_id.
            self.local_provider.model_id = model_id
            lab = getattr(self, "optimization", None)
            if lab:
                lab.active = None
                lab.last_runtime = None
                self.gemma.context_tokens = self.settings.gemma_context_tokens
                lab.restore_default()
            # Start the selected model only after the old owned process exited.
            self.dialogues.set_state(STATE_KEY_PROVIDER, PROVIDER_LOCAL)
            self.dialogues.set_state("selected_local_model", model_id)
            state = self.gemma.ensure_started()
            return {
                "selected": model_id,
                "local_state": state.get("state"),
            }

    def provider_state(self) -> dict[str, Any]:
        selected = self.selected_provider()
        local_state = self.gemma.state_snapshot()

        # D28: external profile info.
        ext_active = self.external_profile_active
        ext_info = self.external_profile_info if ext_active else None
        ext_missing = self.settings.external_profile_errors()
        if ext_missing:
            return {"selected": "external", "external_active": True, "external_missing": ext_missing, "external_info": {}, "model_name": self.settings.test_model_name, "config_source": "invalid_external_profile", "local_state": local_state["state"], "local_pid": local_state["pid"], "local_error": None}

        if selected == PROVIDER_EXTERNAL:
            ext_status = self.external_provider.status() if self.external_provider else None
            return {
                "selected": selected,
                "local_state": local_state["state"],
                "local_pid": local_state["pid"],
                "local_error": local_state["error"],
                "network_reachable": None,
                "network_detail": None,
                "missing_config": [],
                "external_active": True,
                "external_info": ext_info,
                "external_missing": ext_missing,
                "external_reachable": ext_status.reachable if ext_status else None,
                "external_detail": ext_status.detail if ext_status else None,
                "model_name": self.external_provider.identity().get("model") if self.external_provider else self.settings.test_model_name,
                "config_source": "external_env",
            }
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
                "external_active": False,
                "external_info": None,
                "external_missing": [],
                "external_reachable": None,
                "external_detail": None,
                "model_name": self.network_provider.identity().get("model"),
                "config_source": "network_deepseek",
            }
        return {
            "selected": selected,
            "local_state": local_state["state"],
            "local_pid": local_state["pid"],
            "local_error": local_state["error"],
            "network_reachable": None,
            "network_detail": None,
            "missing_config": [],
            "external_active": ext_active,
            "external_info": ext_info,
            "external_missing": ext_missing,
            "external_reachable": None,
            "external_detail": None,
            "model_name": self.local_provider.identity().get("model"),
            "config_source": "local_gguf",
        }

    def ensure_local_ready(self) -> dict[str, Any]:
        """Idempotent local start; declares ready only on confirmed availability."""
        self._check_optimization_busy()
        self._validate_profile()
        if self.external_profile_active:
            raise ProviderError("External profile owns generation; local loading is blocked.")
        with self._generation_lock, self._switch_lock:
            self._check_optimization_busy()
            return self.gemma.ensure_started()

    def unload_local(self) -> dict[str, Any]:
        self._check_optimization_busy()
        with self._generation_lock, self._switch_lock:
            self._check_optimization_busy()
            return self.gemma.unload()

    # -- generation -------------------------------------------------------
    def _check_optimization_busy(self):
        lab = getattr(self, "optimization", None)
        if lab and lab.busy:
            from .errors import InvalidRequest
            raise InvalidRequest("Optimization is busy; wait for the comparison/profile switch.")

    def ask(self, *args, **kwargs):
        # Profile changes and provider switches cannot cross prompt preparation/inference.
        self._check_optimization_busy()
        with self._generation_lock:
            self._check_optimization_busy()
            return self._ask(*args, **kwargs)

    def _ask(
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
        # C03: reject a partial external profile — never fall through to Gemma.
        if not self.external_profile_active and self.settings.external_profile_partial():
            raise ProviderError(
                "External profile is partially configured; generation is blocked.",
                details={"missing": self.settings.external_profile_missing()},
            )
        self._validate_profile()
        if self.external_profile_active and provider == PROVIDER_LOCAL:
            raise ProviderError("External profile is active; local override is blocked.")
        name = provider or self.selected_provider()
        answer_provider = self.provider_for(name)

        # Record the user turn first so history survives any provider failure.
        self.dialogues.append_message(
            {"dialogue_id": dialogue_id, "role": "user", "text": question}
        )

        retrieval_started = time.perf_counter()
        fragments: list[dict[str, Any]] = []
        rewrite: RewriteResult = no_rewrite(question)
        if rag_enabled:
            if self.rag is None or self.embedder is None:
                raise ProviderError("RAG is enabled but retrieval is not configured.")
            if use_rewrite:
                # When external profile is active, use the external provider
                # for query rewrite; otherwise fall back to the selected provider.
                rewriter_model = answer_provider
                if self.external_profile_active and self.external_provider:
                    rewriter_model = self.external_provider
                rewriter = self._query_rewriter or ChatQueryRewriter(rewriter_model)
                rewrite = rewriter.rewrite(question)
            search_query = rewrite.search_query
            from .rag.retrieval import retrieve
            fragments = retrieve(self.rag, self.embedder, search_query,
                top_k=top_k or self.settings.rag_top_k, min_score=min_score)

        retrieval_ms = round((time.perf_counter() - retrieval_started) * 1000, 3) if rag_enabled else 0.0
        history = self.dialogues.list_messages(dialogue_id)[:-1]  # exclude the just-added user turn
        lab = getattr(self, "optimization", None)
        profile = lab.active if lab and name == PROVIDER_LOCAL else None
        builder = ContextBuilder(max_context_chars=profile.max_context_chars, prompt_template=profile.prompt_template, quote_hints=profile.quote_hints) if profile else self.context
        messages, trace = builder.build(
            question=question,
            history=history,
            fragments=fragments if rag_enabled else [],
            rag_enabled=rag_enabled,
            memory=self.dialogues.get_memory(dialogue_id),
        )

        # Verify citations only against the evidence actually sent after budgeting.
        fragments = [{**source, "text": source["quote"]} for source in trace["sources"]] if rag_enabled else []
        if rag_enabled and not fragments:
            result = ChatResult(
                text="I don't know from the available documents. Please clarify the question or index a relevant document.",
                model="no-model (insufficient-context)", finish_reason="stop", usage=None,
                latency_ms=0.0, parameters={"generation_skipped": True, "reason": "insufficient_context"},
            )
        else:
            with self._generation_lock:
                epoch = self._generation_epoch
                if name == PROVIDER_LOCAL:
                    self.gemma.ensure_started()
                    self.gemma.set_generating(True)
                try:
                    options = profile.options() if profile else None
                    result = generate_grounded(answer_provider, messages, fragments, options=options) if rag_enabled else (answer_provider.chat(messages, options=options) if options else answer_provider.chat(messages))
                    if profile:
                        result.parameters.update({"profile": profile.name, "context_window": self.gemma.context_tokens, "prompt_template": profile.prompt_template})
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

        citations = CitationVerifier(fragments).verify(result.text).to_dict() if rag_enabled else None
        rag_metadata = {
            "enabled": rag_enabled, "sources": trace["sources"] if rag_enabled else [],
            "rewrite": rewrite.to_dict() if rag_enabled else None, "citations": citations,
        }
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
                "parameters": {**result.parameters, "rag": rag_metadata},
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
                "retrieval_ms": retrieval_ms,
                "context": {k: v for k, v in trace.items() if k != "sources"},
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
            name = PROVIDER_EXTERNAL if self.external_profile_active else (provider or self.selected_provider())
            rewriter = self._query_rewriter or ChatQueryRewriter(self.provider_for(name))
            rewrite = rewriter.rewrite(query)
        if isinstance(self.rag, ExternalIndex):
            self.rag.verify_runtime_embedding(self.embedder)
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
            name = PROVIDER_EXTERNAL if self.external_profile_active else (provider or self.selected_provider())
            rewriter = self._query_rewriter or ChatQueryRewriter(self.provider_for(name))
            rewrite = rewriter.rewrite(query)
        if isinstance(self.rag, ExternalIndex):
            self.rag.verify_runtime_embedding(self.embedder)
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
