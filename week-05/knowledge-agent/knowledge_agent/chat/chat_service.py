"""ChatService: modes, comparison and streaming over D21 retrieval.

D22 keeps its contracts: ``mode=with_rag``/``without_rag``, ``compare`` and the
flat ``usage``/``latency_ms.chat`` generation metrics. Day 23 adds an optional
relevance filter and query rewrite between ``search`` and the context budget,
four comparison modes (A-D), a full selection trace and separately recorded
rewrite metrics (SPEC D23). The service depends on the abstract
``KnowledgeService``, ``ChatModel``, ``QueryRewriter`` and ``ChatRunStore``; it
never imports HTTP, SQLite or Ollama.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Iterator, Mapping

from ..domain.contracts import (
    ChatModel,
    ChatResult,
    ChatRunStore,
    GroundingResult,
    QueryRewriter,
    RewriteResult,
)
from ..domain.errors import (
    ChatInvalidResponse,
    InvalidRequest,
    InvalidThreshold,
    KnowledgeError,
)
from .citations import GroundingVerifier, extract_citations, parse_grounded_response
from .context import BUDGET_METHOD, ContextBudget, ContextPlan
from .filtering import RelevanceFilter
from .prompts import GROUNDED_RAG, PLAIN, RAG, REWRITE, PromptTemplate

_VALID_MODES = ("with_rag", "without_rag")
_VALID_FILTER_MODES = ("with_rag", "without_rag", "compare")
_VALID_KINDS = ("single", "compare")

# ``rag_mode`` -> (use_filter, use_rewrite). A is the plain-RAG baseline.
_RAG_MODES: dict[str, tuple[bool, bool]] = {
    "A": (False, False),
    "B": (True, False),
    "C": (False, True),
    "D": (True, True),
}
_D23_FIELDS = (
    "use_filter",
    "use_rewrite",
    "prefilter_top_k",
    "postfilter_top_k",
    "min_score",
    "rag_mode",
)
INSUFFICIENT_SOURCES_MESSAGE = (
    "No relevant sources found in the selected index. "
    "Lower the relevance threshold or ask another question."
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class ChatService:
    def __init__(
        self,
        knowledge_service: Any,
        chat_model: ChatModel,
        run_store: ChatRunStore,
        *,
        top_k: int = 5,
        max_context_tokens: int = 8192,
        reserved_output_tokens: int = 1024,
        chars_per_token: int = 3,
        temperature: float = 0.0,
        seed: int = 0,
        safety_margin: int = 64,
        query_rewriter: QueryRewriter | None = None,
        rag_filter_enabled: bool = False,
        rag_rewrite_enabled: bool = False,
        rag_min_score: float = 0.0,
        rag_prefilter_top_k: int = 20,
        rag_filter_top_k: int = 5,
        grounding_enabled: bool = False,
    ) -> None:
        self.knowledge = knowledge_service
        self.chat_model = chat_model
        self.run_store = run_store
        # D24: a direct ChatService construction keeps the D22 ``rag-v1`` path
        # (default False); the application enables grounding via ``build_chat_service``.
        self.grounding_enabled = bool(grounding_enabled)
        self.top_k = max(1, int(top_k))
        self.max_context_tokens = int(max_context_tokens)
        self.reserved_output_tokens = int(reserved_output_tokens)
        self.temperature = temperature
        self.seed = seed
        self.query_rewriter = query_rewriter
        self.rag_filter_enabled = bool(rag_filter_enabled)
        self.rag_rewrite_enabled = bool(rag_rewrite_enabled)
        self.rag_min_score = float(rag_min_score)
        self.rag_prefilter_top_k = int(rag_prefilter_top_k)
        self.rag_filter_top_k = int(rag_filter_top_k)
        self.filter = RelevanceFilter()
        self.budget = ContextBudget(
            max_context_tokens=max_context_tokens,
            reserved_output_tokens=reserved_output_tokens,
            chars_per_token=chars_per_token,
            safety_margin=safety_margin,
        )

    # -- public API -------------------------------------------------------
    def chat(self, request: Mapping[str, Any]) -> dict[str, Any]:
        request = dict(request)
        plan = self._plan(request)
        result = None if plan.get("deterministic") else self.chat_model.chat(plan["messages"])
        record = self._assemble(plan, result)
        if request.get("save_run", True):
            self.run_store.create_run(record)
        return record

    def compare(self, request: Mapping[str, Any]) -> dict[str, Any]:
        request = dict(request)
        collection_id = request.get("collection_id")
        if not collection_id:
            raise InvalidRequest("collection_id is required for a comparison.")
        question = _validated_question(request)
        top_k = _validated_top_k(request, self.top_k)

        run_id = self._new_run_id()
        created_at = _utcnow()
        # Resolve the index once, without embedding, so both branches are pinned.
        version = self.knowledge.resolve_ready_index(
            collection_id, request.get("index_version_id"), request.get("strategy")
        )
        pinned = version["index_version_id"]

        rag_plan = self._plan(
            {
                "mode": "with_rag",
                "question": question,
                "collection_id": collection_id,
                "index_version_id": pinned,
                "strategy": request.get("strategy"),
                "top_k": top_k,
                "max_context_tokens": request.get("max_context_tokens"),
                "run_id": run_id,
                "created_at": created_at,
                # SPEC D24 11.5: POST /api/chat/compare is never grounded.
                "grounding": False,
            }
        )
        plain_plan = self._plan(
            {
                "mode": "without_rag",
                "question": question,
                "top_k": top_k,
                "max_context_tokens": request.get("max_context_tokens"),
                "run_id": run_id,
                "created_at": created_at,
            }
        )
        rag_result = None if rag_plan.get("deterministic") else self.chat_model.chat(rag_plan["messages"])
        plain_result = self.chat_model.chat(plain_plan["messages"])
        rag_record = self._assemble(rag_plan, rag_result)
        plain_record = self._assemble(plain_plan, plain_result)

        comparison = {
            "same_model": rag_record["model"].get("model") == plain_record["model"].get("model")
            and rag_record["model"].get("provider") == plain_record["model"].get("provider"),
            "same_settings": rag_record["model"].get("settings") == plain_record["model"].get("settings"),
            "index_version_id": pinned,
            "prompt_templates": {
                "with_rag": RAG.template_id,
                "without_rag": PLAIN.template_id,
            },
            "policy_differences": [
                "with_rag includes retrieved context and a citation instruction",
                "without_rag has no retrieval context",
            ],
        }
        record = {
            "schema_version": "chat-run-v1",
            "run_id": run_id,
            "created_at": created_at,
            "kind": "compare",
            "result_kind": "compare",
            "mode": "compare",
            "question": question,
            "model": rag_record["model"],
            "index": rag_record["index"],
            "prompt": {
                "templates": {
                    "with_rag": rag_record["prompt"],
                    "without_rag": plain_record["prompt"],
                }
            },
            "context": rag_record["context"],
            "usage": None,
            "latency_ms": {
                "retrieval": rag_record["latency_ms"]["retrieval"],
                "context": round(
                    rag_record["latency_ms"]["context"] + plain_record["latency_ms"]["context"], 3
                ),
                "chat": _sum_latency(rag_record, plain_record),
                "total": round(
                    rag_record["latency_ms"]["total"] + plain_record["latency_ms"]["total"], 3
                ),
            },
            "output_tokens_per_second": None,
            "answer": None,
            "retrieval": rag_record["retrieval"],
            "branches": {"with_rag": rag_record, "without_rag": plain_record},
            "comparison": comparison,
            "errors": [],
            "manual_evaluation": None,
        }
        if request.get("save_run", True):
            self.run_store.create_run(record)
        return record

    def compare_modes(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Run the four D23 modes (A-D) on one pinned index and settings."""

        request = dict(request)
        collection_id = request.get("collection_id")
        if not collection_id:
            raise InvalidRequest("collection_id is required for a mode comparison.")
        question = _validated_question(request)
        threshold = _validated_threshold(request, self.rag_min_score)
        prefilter = _int_field(
            request.get("prefilter_top_k"), self.rag_prefilter_top_k, "prefilter_top_k"
        )
        postfilter = _int_field(
            request.get("postfilter_top_k"), self.rag_filter_top_k, "postfilter_top_k"
        )
        _validate_top_k(prefilter, postfilter)

        run_id = self._new_run_id("d23")
        created_at = _utcnow()
        version = self.knowledge.resolve_ready_index(
            collection_id, request.get("index_version_id"), request.get("strategy")
        )
        pinned = version["index_version_id"]

        modes: list[dict[str, Any]] = []
        for mode_id, (use_filter, use_rewrite) in _RAG_MODES.items():
            plan = self._plan(
                {
                    "mode": "with_rag",
                    "question": question,
                    "collection_id": collection_id,
                    "index_version_id": pinned,
                    "strategy": request.get("strategy"),
                    "use_filter": use_filter,
                    "use_rewrite": use_rewrite,
                    "prefilter_top_k": prefilter,
                    "postfilter_top_k": postfilter,
                    "min_score": threshold,
                    "max_context_tokens": request.get("max_context_tokens"),
                    "run_id": run_id,
                    "created_at": created_at,
                }
            )
            result = None if plan.get("deterministic") else self.chat_model.chat(plan["messages"])
            record = self._assemble(plan, result)
            modes.append(
                {
                    "id": mode_id,
                    "use_filter": use_filter,
                    "use_rewrite": use_rewrite,
                    "branch": record,
                }
            )

        model_snapshot = modes[0]["branch"]["model"]
        same_model = all(
            item["branch"]["model"].get("provider") == model_snapshot.get("provider")
            and item["branch"]["model"].get("model") == model_snapshot.get("model")
            for item in modes
        )
        same_settings = all(
            item["branch"]["model"].get("settings") == model_snapshot.get("settings")
            for item in modes
        )
        comparison = {
            "same_model": same_model,
            "same_settings": same_settings,
            "index_version_id": pinned,
            "threshold": threshold,
            "prefilter_top_k": prefilter,
            "postfilter_top_k": postfilter,
            "prompt_templates": {
                "rewrite": REWRITE.template_id,
                # SPEC D24 11.2: the reported generation template is the actual one.
                "generation": (
                    GROUNDED_RAG.template_id if self.grounding_enabled else RAG.template_id
                ),
            },
            "policy_differences": [
                "B and D apply the relevance threshold",
                "C and D replace the retrieval query with a rewritten one",
                "A is plain RAG (no filter, no rewrite)",
            ],
        }
        record = {
            "schema_version": "chat-run-v1",
            "run_id": run_id,
            "created_at": created_at,
            "kind": "compare",
            "result_kind": "compare",
            "mode": "compare",
            "comparison_kind": "four_modes",
            "question": question,
            "model": model_snapshot,
            "index": modes[0]["branch"]["index"],
            "comparison": comparison,
            "modes": modes,
            "usage": None,
            "latency_ms": _aggregate_latency([item["branch"] for item in modes]),
            "answer": None,
            "retrieval": None,
            "errors": [],
            "manual_evaluation": None,
        }
        if request.get("save_run", True):
            self.run_store.create_run(record)
        return record

    def prepare(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and resolve a streaming plan; raised errors map to HTTP."""

        return self._plan(dict(request))

    def stream(self, request: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
        request = dict(request)
        return self.stream_events(self._plan(request), request.get("save_run", True))

    def stream_events(
        self, plan: Mapping[str, Any], save_run: bool = True
    ) -> Iterator[dict[str, Any]]:
        yield {
            "type": "start",
            "run_id": plan["run_id"],
            "mode": plan["mode"],
            "model": plan["model"],
            "index": plan["index"],
            "rag_mode": plan.get("rag_mode"),
        }
        if plan["mode"] == "with_rag" and plan["retrieval"] is not None:
            retrieval = plan["retrieval"]
            yield {
                "type": "sources",
                "found_count": retrieval["found_count"],
                "selected_count": retrieval["selected_count"],
                "passed_count": retrieval["passed_count"],
                "search_query": plan["search_query"],
                "passed": retrieval["passed"],
            }
        if plan.get("deterministic"):
            record = self._assemble(plan, None)
            if save_run:
                self.run_store.create_run(record)
            yield {"type": "done", "answer": record}
            return

        stream = None
        try:
            stream = self.chat_model.stream_chat(plan["messages"])
            for event in stream:
                if event.get("type") == "token":
                    yield {"type": "token", "text": event.get("text", "")}
                elif event.get("type") == "done":
                    result = event.get("result")
                    if not isinstance(result, ChatResult):
                        raise ChatInvalidResponse("The provider done event carried no result.")
                    record = self._assemble(plan, result)
                    if save_run:
                        self.run_store.create_run(record)
                    yield {"type": "done", "answer": record}
                    return
            raise ChatInvalidResponse("The chat stream ended without a done event.")
        except KnowledgeError as exc:
            self._save_partial(plan, exc, save_run)
            yield {"type": "error", "error": exc.to_dict()}
        except Exception as exc:  # noqa: BLE001 - never leak internals over SSE
            self._save_partial(plan, exc, save_run)
            yield {
                "type": "error",
                "error": {
                    "code": "internal_error",
                    "message": f"Unexpected chat error ({type(exc).__name__}).",
                },
            }
        finally:
            # A client disconnect delivers GeneratorExit (a BaseException), which
            # is never caught above; closing the provider iterator releases its
            # HTTP response/socket. A pure cancellation saves no fake record.
            _close_stream(stream)

    def _save_partial(
        self, plan: Mapping[str, Any], exc: Exception, save_run: bool
    ) -> None:
        if not save_run:
            return
        error = exc.to_dict() if isinstance(exc, KnowledgeError) else {
            "code": "internal_error",
            "message": f"Unexpected chat error ({type(exc).__name__}).",
        }
        record = self._assemble(plan, None)
        record["errors"] = [error]
        record["answer"] = None
        try:
            self.run_store.create_run(record)
        except Exception:  # noqa: BLE001 - a failed partial save must not hide the error
            pass

    # -- runs/evaluations -------------------------------------------------
    def list_runs(
        self, *, limit: int = 20, kind: str | None = None, mode: str | None = None
    ) -> dict[str, Any]:
        if kind is not None and kind not in _VALID_KINDS:
            raise InvalidRequest("kind must be 'single' or 'compare'.")
        if mode is not None and mode not in _VALID_FILTER_MODES:
            raise InvalidRequest("mode must be 'with_rag', 'without_rag' or 'compare'.")
        if kind == "single" and mode == "compare":
            raise InvalidRequest("kind=single is incompatible with mode=compare.")
        if kind == "compare" and mode in ("with_rag", "without_rag"):
            raise InvalidRequest(f"kind=compare is incompatible with mode={mode}.")
        runs = self.run_store.list_runs(limit=max(1, min(200, int(limit))), kind=kind, mode=mode)
        return {"runs": runs, "total": len(runs)}

    def get_run(self, run_id: str) -> dict[str, Any]:
        record = self.run_store.get_run(run_id)
        if record is None:
            raise KeyError(run_id)
        return record

    def save_evaluation(self, run_id: str, evaluation: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(evaluation)
        if payload.get("run_id") not in (None, run_id):
            raise InvalidRequest("The evaluation run_id does not match the path.")
        payload["run_id"] = run_id
        return self.run_store.save_evaluation(payload)

    def get_evaluation(self, run_id: str) -> dict[str, Any]:
        evaluation = self.run_store.get_evaluation(run_id)
        if evaluation is None:
            raise KeyError(run_id)
        return evaluation

    def health(self) -> dict[str, Any]:
        try:
            preflight = self.chat_model.preflight()
        except Exception:  # noqa: BLE001 - health must never raise
            preflight = {"reachable": False}
        reachable = bool(preflight.get("reachable"))
        model_present = bool(preflight.get("model_present"))
        model = self.chat_model.identity().model
        if reachable and model_present:
            hint = None
        elif self.chat_model.identity().provider != "ollama":
            hint = "Check the selected chat test profile and runtime availability"
        elif model:
            hint = f"ollama pull {model}"
        else:
            hint = "CHAT_MODEL is not set"
        return {
            "reachable": reachable,
            "model_present": model_present,
            "model": model or None,
            "digest": preflight.get("digest"),
            "context_length": preflight.get("context_length"),
            "hint": hint,
        }

    # -- planning ---------------------------------------------------------
    def _plan(self, request: Mapping[str, Any]) -> dict[str, Any]:
        mode = request.get("mode")
        if mode not in _VALID_MODES:
            raise InvalidRequest("mode must be 'with_rag' or 'without_rag'.")
        question = _validated_question(request)
        run_id = str(request.get("run_id") or self._new_run_id())
        created_at = str(request.get("created_at") or _utcnow())
        model_snapshot = self._identity_snapshot()
        model_context_length = model_snapshot.get("context_length")

        if mode == "without_rag":
            # Day 23 fields are ignored here: exactly one plain call (D22 shape).
            started = perf_counter()
            template = PLAIN
            messages = template.build(question)
            self.budget.plan(mandatory_texts=[m.content for m in messages], candidates=[],
                request_max_context_tokens=request.get("max_context_tokens"),
                model_context_length=model_context_length)
            context_ms = round((perf_counter() - started) * 1000, 3)
            context = {
                "budget_method": BUDGET_METHOD,
                "max_context_tokens": self.budget.effective_context_tokens(
                    request.get("max_context_tokens"), model_context_length
                ),
                "reserved_output_tokens": self.reserved_output_tokens,
                "prompt_tokens_estimated": sum(
                    self.budget.estimate(message.content) for message in messages
                ),
                "prompt_tokens_actual": None,
                "dropped_chunks": 0,
                "overflow": False,
            }
            return {
                "run_id": run_id,
                "created_at": created_at,
                "mode": mode,
                "question": question,
                "original_query": question,
                "search_query": question,
                "rewrite": _no_rewrite(question).to_dict(),
                "rewrite_latency": 0.0,
                "rag_mode": None,
                "use_filter": False,
                "use_rewrite": False,
                "min_score": None,
                "prefilter_top_k": None,
                "postfilter_top_k": None,
                "deterministic": False,
                "insufficient_message": None,
                "grounding": False,
                "model": model_snapshot,
                "template": template,
                "messages": messages,
                "index": None,
                "retrieval": None,
                "passed_ids": set(),
                "passed_chunks": [],
                "context": context,
                "latency_ms": {"retrieval": 0.0, "context": context_ms},
            }

        collection_id = request.get("collection_id")
        if not collection_id:
            raise InvalidRequest("collection_id is required for mode=with_rag.")
        use_filter, use_rewrite, rag_mode = self._resolve_rag_mode(request)
        threshold = _validated_threshold(request, self.rag_min_score)
        prefilter, postfilter = self._resolve_top_k(request)

        rewrite_dict, rewrite_ms = self._rewrite(question, use_rewrite)
        search_query = str(rewrite_dict.get("search_query") or question)

        started = perf_counter()
        search = self.knowledge.search(
            collection_id,
            search_query,
            top_k=prefilter,
            index_version_id=request.get("index_version_id"),
            strategy=request.get("strategy"),
        )
        retrieval_ms = round((perf_counter() - started) * 1000, 3)
        found = search.get("fragments") or []
        candidates = [
            {
                "rank": fragment.get("rank"),
                "score": fragment.get("score"),
                "chunk_id": fragment.get("chunk_id"),
                "text": fragment.get("text"),
                "metadata": fragment.get("metadata") or {},
            }
            for fragment in found
        ]
        focus = self.filter.apply(
            candidates,
            threshold=threshold if use_filter else 0.0,
            postfilter_top_k=postfilter,
        )
        selected = focus.selected
        grounding = self._resolve_grounding(request)
        template = GROUNDED_RAG if grounding else RAG
        # D24 defect fix: the budget must count the system prompt actually sent.
        # Grounding swaps RAG.system for the longer GROUNDED_RAG.system, so the
        # template is resolved before planning; without grounding this is RAG.system.
        plan: ContextPlan = self.budget.plan(
            mandatory_texts=[template.system, question],
            candidates=selected,
            request_max_context_tokens=request.get("max_context_tokens"),
            model_context_length=model_context_length,
        )
        context_ms = round((perf_counter() - started) * 1000 - retrieval_ms, 3)
        deterministic = bool(use_filter and not selected)
        messages = None if deterministic else template.build(question, plan.passed)
        version = self.knowledge.get_index_version(search["index_version_id"])
        index = {
            "collection_id": collection_id,
            "index_version_id": search["index_version_id"],
            "strategy": search.get("strategy"),
            "fingerprint": (version or {}).get("fingerprint"),
        }
        retrieval = {
            "found": [self._candidate_projection(item) for item in candidates],
            "candidates": [self._candidate_projection(item) for item in candidates],
            "selected": [self._candidate_projection(item) for item in selected],
            "passed": [
                {
                    "rank": item.get("rank"),
                    "chunk_id": item.get("chunk_id"),
                    "estimated_tokens": item.get("estimated_tokens"),
                    "metadata": item.get("metadata") or {},
                }
                for item in plan.passed
            ],
            "found_count": len(candidates),
            "selected_count": len(selected),
            "passed_count": len(plan.passed),
            "exclusion_reasons": {
                "threshold": focus.threshold_excluded,
                "top_k": focus.top_k_excluded,
                "context_budget": list(plan.dropped_ids),
            },
        }
        context = {
            "budget_method": plan.budget_method,
            "max_context_tokens": plan.max_context_tokens,
            "reserved_output_tokens": plan.reserved_output_tokens,
            "prompt_tokens_estimated": plan.prompt_tokens_estimated,
            "prompt_tokens_actual": None,
            "dropped_chunks": plan.dropped_chunks,
            "overflow": plan.overflow,
        }
        return {
            "run_id": run_id,
            "created_at": created_at,
            "mode": mode,
            "question": question,
            "original_query": question,
            "search_query": search_query,
            "rewrite": rewrite_dict,
            "rewrite_latency": rewrite_ms,
            "rag_mode": rag_mode,
            "use_filter": use_filter,
            "use_rewrite": use_rewrite,
            "min_score": threshold,
            "prefilter_top_k": prefilter,
            "postfilter_top_k": postfilter,
            "deterministic": deterministic,
            "insufficient_message": INSUFFICIENT_SOURCES_MESSAGE,
            "grounding": grounding,
            "model": model_snapshot,
            "template": template,
            "messages": messages,
            "index": index,
            "retrieval": retrieval,
            "passed_ids": {item["chunk_id"] for item in plan.passed},
            "passed_chunks": plan.passed,
            "context": context,
            "latency_ms": {"retrieval": retrieval_ms, "context": context_ms},
        }

    def _resolve_grounding(self, request: Mapping[str, Any]) -> bool:
        """Explicit ``grounding`` wins; otherwise the service setting applies."""

        raw = request.get("grounding")
        if raw is None:
            return self.grounding_enabled
        return bool(raw)

    def _resolve_rag_mode(self, request: Mapping[str, Any]) -> tuple[bool, bool, str]:
        """Resolve A-D from ``rag_mode``/booleans, with config defaults as fallback.

        When the request omits ``use_filter``/``use_rewrite``, the configured
        D23 defaults apply (both ``0`` keep the D22 behaviour, K2).
        """

        raw_mode = request.get("rag_mode")
        use_filter = request.get("use_filter")
        use_rewrite = request.get("use_rewrite")
        if raw_mode is not None:
            if raw_mode not in _RAG_MODES:
                raise InvalidRequest("rag_mode must be one of A, B, C, D.")
            expected_filter, expected_rewrite = _RAG_MODES[raw_mode]
            if use_filter is not None and bool(use_filter) != expected_filter:
                raise InvalidRequest("rag_mode conflicts with use_filter.")
            if use_rewrite is not None and bool(use_rewrite) != expected_rewrite:
                raise InvalidRequest("rag_mode conflicts with use_rewrite.")
            return expected_filter, expected_rewrite, str(raw_mode)
        resolved_filter = self.rag_filter_enabled if use_filter is None else bool(use_filter)
        resolved_rewrite = self.rag_rewrite_enabled if use_rewrite is None else bool(use_rewrite)
        for mode_id, pair in _RAG_MODES.items():
            if pair == (resolved_filter, resolved_rewrite):
                return resolved_filter, resolved_rewrite, mode_id
        raise InvalidRequest("Invalid filter/rewrite combination.")

    def _resolve_top_k(self, request: Mapping[str, Any]) -> tuple[int, int]:
        legacy = _validated_top_k(request, self.top_k)
        d23_requested = (
            self.rag_filter_enabled
            or self.rag_rewrite_enabled
            or any(request.get(field) is not None for field in _D23_FIELDS)
        )
        raw_prefilter = request.get("prefilter_top_k")
        if raw_prefilter is not None:
            prefilter = _int_field(raw_prefilter, self.rag_prefilter_top_k, "prefilter_top_k")
        elif d23_requested:
            prefilter = int(self.rag_prefilter_top_k)
        else:
            prefilter = legacy
        raw_postfilter = request.get("postfilter_top_k")
        postfilter = (
            _int_field(raw_postfilter, self.rag_filter_top_k, "postfilter_top_k")
            if raw_postfilter is not None
            else prefilter
        )
        _validate_top_k(prefilter, postfilter)
        return prefilter, postfilter

    def _rewrite(self, question: str, use_rewrite: bool) -> tuple[dict[str, Any], float]:
        if not use_rewrite or self.query_rewriter is None:
            return _no_rewrite(question).to_dict(), 0.0
        result = self.query_rewriter.rewrite(question)
        latency = float(result.latency_ms or 0.0)
        return result.to_dict(), latency

    def _assemble(self, plan: Mapping[str, Any], result: ChatResult | None) -> dict[str, Any]:
        # A provider result is only persisted when it carries real answer text;
        # this also covers stream ``done`` events before they reach the store.
        if result is not None and not result.text.strip():
            raise ChatInvalidResponse("The provider returned an empty answer.")

        retrieval_ms = plan["latency_ms"]["retrieval"]
        context_ms = plan["latency_ms"]["context"]
        rewrite_ms = float(plan.get("rewrite_latency") or 0.0)
        base = {
            "schema_version": "chat-run-v1",
            "run_id": plan["run_id"],
            "created_at": plan["created_at"],
            "result_kind": "single",
            "mode": plan["mode"],
            "question": plan["question"],
            "original_query": plan.get("original_query", plan["question"]),
            "search_query": plan.get("search_query", plan["question"]),
            "rewrite": plan.get("rewrite"),
            "rag_mode": plan.get("rag_mode"),
            "use_filter": plan.get("use_filter"),
            "use_rewrite": plan.get("use_rewrite"),
            "min_score": plan.get("min_score"),
            "prefilter_top_k": plan.get("prefilter_top_k"),
            "postfilter_top_k": plan.get("postfilter_top_k"),
            "model": plan["model"],
            "index": plan["index"],
            "prompt": {
                "template_id": plan["template"].template_id,
                "hash": plan["template"].content_hash(),
            },
            "retrieval": plan["retrieval"],
            "context": self._context_with_actual(plan, result),
            "errors": [],
            "manual_evaluation": None,
        }

        if plan.get("deterministic"):
            base["usage"] = None
            base["latency_ms"] = {
                "retrieval": retrieval_ms,
                "context": context_ms,
                "chat": None,
                "total": round(retrieval_ms + context_ms + rewrite_ms, 3),
            }
            base["output_tokens_per_second"] = None
            message = plan.get("insufficient_message") or INSUFFICIENT_SOURCES_MESSAGE
            answer = {
                "text": message,
                "finish_reason": None,
                "truncated": False,
                "citations": {"valid": [], "unsupported": []},
                "insufficient_sources": True,
            }
            if plan.get("grounding"):
                threshold = plan.get("min_score")
                answer["grounding"] = GroundingResult(
                    status="refused",
                    reason="below_threshold",
                    threshold=threshold,
                    refusal={
                        "reason": "below_threshold",
                        "message": message,
                        "threshold": threshold,
                    },
                ).to_dict()
            base["answer"] = answer
            return base

        if result is None:
            base["usage"] = None
            base["latency_ms"] = {
                "retrieval": retrieval_ms,
                "context": context_ms,
                "chat": None,
                "total": round(retrieval_ms + context_ms + rewrite_ms, 3),
            }
            base["output_tokens_per_second"] = None
            base["answer"] = None
            return base

        chat_ms = result.latency_ms
        base["usage"] = result.usage.to_dict() if result.usage else None
        base["latency_ms"] = {
            "retrieval": retrieval_ms,
            "context": context_ms,
            "chat": chat_ms,
            "total": round(retrieval_ms + context_ms + chat_ms + rewrite_ms, 3),
        }
        base["output_tokens_per_second"] = result.output_tokens_per_second
        if plan["mode"] == "with_rag" and plan.get("grounding"):
            base["answer"] = self._grounded_answer(plan, result)
        else:
            citations = (
                {"valid": [], "unsupported": []}
                if plan["mode"] == "without_rag"
                else extract_citations(result.text, plan["passed_ids"])
            )
            base["answer"] = {
                "text": result.text,
                "finish_reason": result.finish_reason,
                "truncated": result.finish_reason == "length",
                "citations": citations,
                "insufficient_sources": False,
            }
        return base

    def _grounded_answer(
        self, plan: Mapping[str, Any], result: ChatResult
    ) -> dict[str, Any]:
        """Build the grounded ``answer`` block with the D24 processing order.

        Order is format first, then ``finish_reason``: a malformed answer raises a
        typed format error regardless of ``length``; only a successfully parsed
        object can enter the truncated branch and it never becomes ``verified``.
        """

        # Raises ChatInvalidResponse(details.format="grounded_json") on any
        # deviation from the grounded-JSON contract; the text is never repaired.
        try:
            parsed = parse_grounded_response(result.text)
        except ChatInvalidResponse as exc:
            # Safe provider metadata only: never the answer text itself.
            exc.details.setdefault("finish_reason", result.finish_reason)
            exc.details.setdefault("answer_chars", len(result.text))
            raise
        verifier = GroundingVerifier(plan.get("passed_chunks") or [])
        threshold = plan.get("min_score")
        grounding = verifier.verify(parsed, threshold=threshold)
        truncated = result.finish_reason == "length"
        if truncated:
            grounding.status = "failed"
            grounding.reason = "truncated_generation"
            grounding.refusal = None
        citations = extract_citations(parsed.answer, plan["passed_ids"])
        return {
            "text": parsed.answer,
            "finish_reason": result.finish_reason,
            "truncated": truncated,
            "citations": citations,
            "insufficient_sources": grounding.status == "refused",
            "grounding": grounding.to_dict(),
        }

    @staticmethod
    def _candidate_projection(item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "rank": item.get("rank"),
            "score": item.get("score"),
            "chunk_id": item.get("chunk_id"),
            "metadata": item.get("metadata") or {},
        }

    def _context_with_actual(
        self, plan: Mapping[str, Any], result: ChatResult | None
    ) -> dict[str, Any]:
        context = dict(plan["context"])
        if result is not None and result.usage is not None:
            context["prompt_tokens_actual"] = result.usage.input_tokens
        else:
            context["prompt_tokens_actual"] = None
        return context

    def _identity_snapshot(self) -> dict[str, Any]:
        identity = self.chat_model.identity()
        settings = dict(identity.default_options)
        settings.setdefault("temperature", self.temperature)
        settings.setdefault("seed", self.seed)
        settings.setdefault("num_predict", self.reserved_output_tokens)
        settings.setdefault("num_ctx", self.max_context_tokens)
        return {
            "provider": identity.provider,
            "model": identity.model,
            "digest": identity.digest,
            "context_length": identity.context_length,
            "max_output_tokens": settings.get("num_predict"),
            "settings": settings,
        }

    @staticmethod
    def _new_run_id(prefix: str = "d22") -> str:
        return f"{prefix}-{_run_stamp()}-{secrets.token_hex(4)}"


def _no_rewrite(question: str) -> RewriteResult:
    return RewriteResult(
        original_query=question,
        search_query=question,
        attempted=False,
        used=False,
        fallback=False,
        reason=None,
        template_id="",
        template_hash="",
        finish_reason=None,
        usage=None,
        latency_ms=0.0,
    )


def _close_stream(stream: Any) -> None:
    if stream is None:
        return
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - closing must never mask the outcome
            pass


def _sum_latency(*records: Mapping[str, Any]) -> float:
    total = 0.0
    for record in records:
        value = (record.get("latency_ms") or {}).get("chat")
        total += float(value or 0.0)
    return round(total, 3)


def _aggregate_latency(branches: list[Mapping[str, Any]]) -> dict[str, Any]:
    def total(key: str) -> float:
        return round(sum(float((branch.get("latency_ms") or {}).get(key) or 0.0) for branch in branches), 3)

    return {
        "retrieval": total("retrieval"),
        "context": total("context"),
        "chat": total("chat"),
        "total": total("total"),
    }


def _validated_question(request: Mapping[str, Any]) -> str:
    question = str(request.get("question") or "")
    if not question.strip():
        raise InvalidRequest("question must not be empty.")
    if len(question) > 2000:
        raise InvalidRequest("question must be at most 2000 characters.")
    return question


def _validated_top_k(request: Mapping[str, Any], default: int) -> int:
    raw = request.get("top_k")
    try:
        top_k = int(raw) if raw is not None else int(default)
    except (TypeError, ValueError) as exc:
        raise InvalidRequest("top_k must be an integer.") from exc
    if top_k < 1 or top_k > 50:
        raise InvalidRequest("top_k must be between 1 and 50.")
    return top_k


def _int_field(raw: Any, default: int, name: str) -> int:
    if raw is None:
        return int(default)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidRequest(f"{name} must be an integer.") from exc


def _validated_threshold(request: Mapping[str, Any], default: float) -> float:
    raw = request.get("min_score")
    try:
        value = float(default if raw is None else raw)
    except (TypeError, ValueError) as exc:
        raise InvalidThreshold("min_score must be a number between 0 and 1.") from exc
    if not 0.0 <= value <= 1.0:
        raise InvalidThreshold("min_score must be between 0 and 1.")
    return value


def _validate_top_k(prefilter: int, postfilter: int) -> None:
    if prefilter < 1 or prefilter > 50:
        raise InvalidRequest("prefilter_top_k must be between 1 and 50.")
    if postfilter < 1 or postfilter > 50:
        raise InvalidRequest("postfilter_top_k must be between 1 and 50.")
    if postfilter > prefilter:
        raise InvalidRequest("postfilter_top_k must not exceed prefilter_top_k.")
