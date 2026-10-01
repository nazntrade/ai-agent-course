"""ChatService: modes, comparison and streaming over D21 retrieval (SPEC D22).

The service depends on the abstract ``KnowledgeService`` and ``ChatModel``; it
never imports HTTP, SQLite or Ollama. Retrieval errors are propagated unchanged
and are never masked by a non-RAG answer.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Iterator, Mapping

from ..domain.contracts import ChatModel, ChatResult, ChatRunStore
from ..domain.errors import ChatInvalidResponse, InvalidRequest, KnowledgeError
from .citations import extract_citations
from .context import BUDGET_METHOD, ContextBudget, ContextPlan
from .prompts import PLAIN, RAG, PromptTemplate

_VALID_MODES = ("with_rag", "without_rag")
_VALID_FILTER_MODES = ("with_rag", "without_rag", "compare")
_VALID_KINDS = ("single", "compare")


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
    ) -> None:
        self.knowledge = knowledge_service
        self.chat_model = chat_model
        self.run_store = run_store
        self.top_k = max(1, int(top_k))
        self.max_context_tokens = int(max_context_tokens)
        self.reserved_output_tokens = int(reserved_output_tokens)
        self.temperature = temperature
        self.seed = seed
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
        result = self.chat_model.chat(plan["messages"])
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
        rag_result = self.chat_model.chat(rag_plan["messages"])
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
                "chat": round(
                    rag_record["latency_ms"]["chat"] + plain_record["latency_ms"]["chat"], 3
                ),
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
        }
        if plan["mode"] == "with_rag" and plan["retrieval"] is not None:
            retrieval = plan["retrieval"]
            yield {
                "type": "sources",
                "found_count": retrieval["found_count"],
                "passed_count": retrieval["passed_count"],
                "passed": retrieval["passed"],
            }
        try:
            for event in self.chat_model.stream_chat(plan["messages"]):
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
        top_k = _validated_top_k(request, self.top_k)
        run_id = str(request.get("run_id") or self._new_run_id())
        created_at = str(request.get("created_at") or _utcnow())
        model_snapshot = self._identity_snapshot()
        model_context_length = model_snapshot.get("context_length")

        if mode == "without_rag":
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
                "model": model_snapshot,
                "template": template,
                "messages": messages,
                "index": None,
                "retrieval": None,
                "passed_ids": set(),
                "context": context,
                "latency_ms": {"retrieval": 0.0, "context": context_ms},
            }

        collection_id = request.get("collection_id")
        if not collection_id:
            raise InvalidRequest("collection_id is required for mode=with_rag.")
        started = perf_counter()
        search = self.knowledge.search(
            collection_id,
            question,
            top_k=top_k,
            index_version_id=request.get("index_version_id"),
            strategy=request.get("strategy"),
        )
        retrieval_ms = round((perf_counter() - started) * 1000, 3)
        found = search.get("fragments") or []
        candidates = [
            {
                "rank": fragment.get("rank"),
                "chunk_id": fragment.get("chunk_id"),
                "text": fragment.get("text"),
                "metadata": fragment.get("metadata") or {},
            }
            for fragment in found
        ]
        plan: ContextPlan = self.budget.plan(
            mandatory_texts=[RAG.system, question],
            candidates=candidates,
            request_max_context_tokens=request.get("max_context_tokens"),
            model_context_length=model_context_length,
        )
        context_ms = round((perf_counter() - started) * 1000 - retrieval_ms, 3)
        messages = RAG.build(question, plan.passed)
        version = self.knowledge.get_index_version(search["index_version_id"])
        index = {
            "collection_id": collection_id,
            "index_version_id": search["index_version_id"],
            "strategy": search.get("strategy"),
            "fingerprint": (version or {}).get("fingerprint"),
        }
        retrieval = {
            "found": [
                {
                    "rank": fragment.get("rank"),
                    "score": fragment.get("score"),
                    "chunk_id": fragment.get("chunk_id"),
                    "metadata": fragment.get("metadata") or {},
                }
                for fragment in found
            ],
            "passed": [
                {
                    "rank": item.get("rank"),
                    "chunk_id": item.get("chunk_id"),
                    "estimated_tokens": item.get("estimated_tokens"),
                    "metadata": item.get("metadata") or {},
                }
                for item in plan.passed
            ],
            "found_count": len(found),
            "passed_count": len(plan.passed),
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
            "model": model_snapshot,
            "template": RAG,
            "messages": messages,
            "index": index,
            "retrieval": retrieval,
            "passed_ids": {item["chunk_id"] for item in plan.passed},
            "context": context,
            "latency_ms": {"retrieval": retrieval_ms, "context": context_ms},
        }

    def _assemble(self, plan: Mapping[str, Any], result: ChatResult | None) -> dict[str, Any]:
        if result is None:
            return {
                "schema_version": "chat-run-v1",
                "run_id": plan["run_id"],
                "created_at": plan["created_at"],
                "result_kind": "single",
                "mode": plan["mode"],
                "question": plan["question"],
                "model": plan["model"],
                "index": plan["index"],
                "prompt": {
                    "template_id": plan["template"].template_id,
                    "hash": plan["template"].content_hash(),
                },
                "retrieval": plan["retrieval"],
                "context": self._context_with_actual(plan, None),
                "usage": None,
                "latency_ms": {
                    "retrieval": plan["latency_ms"]["retrieval"],
                    "context": plan["latency_ms"]["context"],
                    "chat": None,
                    "total": round(
                        plan["latency_ms"]["retrieval"] + plan["latency_ms"]["context"], 3
                    ),
                },
                "output_tokens_per_second": None,
                "answer": None,
                "errors": [],
                "manual_evaluation": None,
            }
        chat_ms = result.latency_ms
        latency = {
            "retrieval": plan["latency_ms"]["retrieval"],
            "context": plan["latency_ms"]["context"],
            "chat": chat_ms,
            "total": round(
                plan["latency_ms"]["retrieval"] + plan["latency_ms"]["context"] + chat_ms, 3
            ),
        }
        if plan["mode"] == "without_rag":
            citations = {"valid": [], "unsupported": []}
        else:
            citations = extract_citations(result.text, plan["passed_ids"])
        answer = {
            "text": result.text,
            "finish_reason": result.finish_reason,
            "truncated": result.finish_reason == "length",
            "citations": citations,
        }
        return {
            "schema_version": "chat-run-v1",
            "run_id": plan["run_id"],
            "created_at": plan["created_at"],
            "result_kind": "single",
            "mode": plan["mode"],
            "question": plan["question"],
            "model": plan["model"],
            "index": plan["index"],
            "prompt": {
                "template_id": plan["template"].template_id,
                "hash": plan["template"].content_hash(),
            },
            "retrieval": plan["retrieval"],
            "context": self._context_with_actual(plan, result),
            "usage": result.usage.to_dict() if result.usage else None,
            "latency_ms": latency,
            "output_tokens_per_second": result.output_tokens_per_second,
            "answer": answer,
            "errors": [],
            "manual_evaluation": None,
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
    def _new_run_id() -> str:
        return f"d22-{_run_stamp()}-{secrets.token_hex(4)}"


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
