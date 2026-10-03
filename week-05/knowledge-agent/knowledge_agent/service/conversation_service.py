"""ConversationService: orchestrate one multi-turn dialogue turn (SPEC D25 5.3).

Order per user message: reference resolution -> task-memory update -> fresh
retrieval (via the existing D21-D24 ``ChatService`` flow) -> budget with history
and memory -> generation -> persist the turn. The service never imports SQLite
or HTTP; it depends on the ``ConversationStore`` contract and ``ChatService``.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ..chat.chat_service import INSUFFICIENT_SOURCES_MESSAGE
from ..chat.memory import extract, task_memory, validate_patch
from ..chat.prompts import build_memory_block, history_messages
from ..chat.references import ReferenceResolver
from ..domain.contracts import (
    ConversationStore,
    MemoryOperation,
    ReferenceResolution,
    TaskMemory,
)
from ..domain.errors import (
    ContextOverflow,
    DeletionRequiresConfirmation,
    DialogueNotFound,
    InvalidMemoryOperation,
    InvalidRequest,
    MemoryConflict,
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# Correction defects 4/5: a refusal must be a clear user-facing sentence in the
# language of the user's question, and a citation failure is a distinct outcome.
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")

_REFUSAL_MESSAGE_RU = (
    "В переданных документах нет ответа на этот вопрос. "
    "Переформулируйте вопрос, понизьте порог релевантности или выберите другой индекс."
)
_REFUSAL_MESSAGE_EN = INSUFFICIENT_SOURCES_MESSAGE
_CITATION_FAILURE_MESSAGE_RU = (
    "Ответ не удалось подтвердить по переданным фрагментам. "
    "Переформулируйте вопрос или выберите другой индекс."
)
_CITATION_FAILURE_MESSAGE_EN = (
    "The answer could not be confirmed against the passed excerpts. "
    "Rephrase the question or choose another index."
)


def _is_russian(text: str) -> bool:
    return bool(_CYRILLIC_RE.search(text or ""))


def _refusal_message(question: str) -> str:
    return _REFUSAL_MESSAGE_RU if _is_russian(question) else _REFUSAL_MESSAGE_EN


def _citation_failure_message(question: str) -> str:
    return _CITATION_FAILURE_MESSAGE_RU if _is_russian(question) else _CITATION_FAILURE_MESSAGE_EN


# Correction (A12): a request that explicitly asks to summarize or list the
# user's own task state (goal, active conditions, clarifications, terms) is a
# task-state turn, not a documentary question. Documents cannot contain the
# user's conditions, so such a turn must be answered from <task_memory> and must
# not be suppressed into a documentary refusal when the grounded model returns
# ``insufficient=true`` or its citations do not verify.
_TASK_STATE_TERM_RE = re.compile(
    r"услов|цел[ьяи]|задач|уточнен|термин|goal|condition|constraint|objective|clarification",
    re.IGNORECASE,
)
_TASK_STATE_SUMMARY_INTENT_RE = re.compile(
    r"подвед\w*\s+(?:общ\w+\s+)?итог"
    r"|итогов\w*\s+(?:сводк|список)"
    r"|сводк"
    r"|суммир"
    r"|перечисл"
    r"|напомни"
    r"|назов\w+"
    r"|список"
    r"|summari[sz]"
    r"|list\b"
    r"|remind",
    re.IGNORECASE,
)


def is_task_state_summary_request(question: str, memory: Any) -> bool:
    """True when the user explicitly asks to summarize/list their task state."""

    text = str(question or "").strip()
    if not text:
        return False
    state = task_memory(memory)
    has_state = (
        bool((state.goal or {}).get("text"))
        or bool(state.active_constraints())
        or bool(state.clarifications)
        or bool(state.terms)
    )
    if not has_state:
        return False
    if not _TASK_STATE_TERM_RE.search(text) or not _TASK_STATE_SUMMARY_INTENT_RE.search(text):
        return False
    # Documentary questions must never turn into a memory-only success, even
    # when they contain a word such as "conditions", "goal" or "terms".
    if re.search(r"документ|обзор|источник|раздел|стать[яие]|по\s+материал|обучени|агент|rag|document|source|paper|article|according", text, re.IGNORECASE):
        return False
    return bool(re.search(
        r"сохран[её]н|(?:мо[ияеи]|наш[аиу]|текущ[иеая])\s+(?:цел|услов|задач|уточнен|термин)"
        r"|с\s+уч[её]том\s+(?:всех\s+)?условий"
        r"|(?:my|our|saved|current)\s+(?:task|goal|condition|constraint|objective)",
        text, re.IGNORECASE,
    ))


def build_task_state_summary(memory: Any, question: str) -> str:
    """Render a substantive summary from the confirmed task memory.

    The summary contains the stored goal and active conditions plus any
    clarifications/terms; it never invents documentary facts and needs no
    citations. It is the answer for a task-state summary turn.
    """

    state = task_memory(memory)
    goal = str((state.goal or {}).get("text") or "").strip()
    active = [str(item.get("text") or "").strip() for item in state.active_constraints()]
    active = [item for item in active if item]
    clarifications = [
        (str(item.get("question") or "").strip(), str(item.get("answer") or "").strip())
        for item in state.clarifications
    ]
    terms = [
        (str(item.get("term") or "").strip(), str(item.get("definition") or "").strip())
        for item in state.terms
    ]
    if _is_russian(question):
        lines = ["Итог с учётом всех сохранённых условий задачи."]
        if goal:
            lines.append(f"Цель: {goal}.")
        if active:
            lines.append("Активные условия: " + "; ".join(active) + ".")
        else:
            lines.append("Активных условий не задано.")
        explained = [f"{item} — {answer}" if answer else item for item, answer in clarifications if item]
        if explained:
            lines.append("Уточнения: " + "; ".join(explained) + ".")
        term_lines = [f"{term}: {definition}" if definition else term for term, definition in terms if term]
        if term_lines:
            lines.append("Термины: " + "; ".join(term_lines) + ".")
        return "\n".join(lines)

    lines = ["Summary of the task state."]
    if goal:
        lines.append(f"Goal: {goal}.")
    if active:
        lines.append("Active conditions: " + "; ".join(active) + ".")
    else:
        lines.append("No active conditions.")
    explained = [f"{item} — {answer}" if answer else item for item, answer in clarifications if item]
    if explained:
        lines.append("Clarifications: " + "; ".join(explained) + ".")
    term_lines = [f"{term}: {definition}" if definition else term for term, definition in terms if term]
    if term_lines:
        lines.append("Terms: " + "; ".join(term_lines) + ".")
    return "\n".join(lines)


class ConversationService:
    def __init__(
        self,
        store: ConversationStore,
        chat_service: Any,
        *,
        history_max_turns: int = 6,
        history_max_tokens: int = 1200,
        resolver: ReferenceResolver | None = None,
    ) -> None:
        self.store = store
        self.chat = chat_service
        self.history_max_turns = max(1, int(history_max_turns))
        self.history_max_tokens = max(1, int(history_max_tokens))
        self.resolver = resolver or ReferenceResolver()

    # -- dialogue CRUD ----------------------------------------------------
    def create_dialogue(self, name: str | None = None) -> dict[str, Any]:
        return self.store.create_dialogue(name)

    def list_dialogues(self) -> list[dict[str, Any]]:
        return self.store.list_dialogues()

    def get_dialogue(self, dialogue_id: str) -> dict[str, Any]:
        dialogue = self._require_dialogue(dialogue_id)
        return {**dialogue, "memory": self.store.get_memory(dialogue_id)}

    def rename_dialogue(self, dialogue_id: str, name: str) -> dict[str, Any]:
        if not str(name or "").strip():
            raise InvalidRequest("name must not be empty.")
        self._require_dialogue(dialogue_id)
        return self.store.rename_dialogue(dialogue_id, name)

    def delete_dialogue(self, dialogue_id: str, *, confirm: bool) -> dict[str, Any]:
        if not confirm:
            raise DeletionRequiresConfirmation(
                "Deleting a dialogue requires confirm=true."
            )
        self._require_dialogue(dialogue_id)
        self.store.delete_dialogue(dialogue_id)
        return {"dialogue_id": dialogue_id, "deleted": True}

    # -- turns ------------------------------------------------------------
    def list_turns(
        self, dialogue_id: str, *, limit: int = 50, before: str | None = None
    ) -> dict[str, Any]:
        self._require_dialogue(dialogue_id)
        turns = self.store.list_turns(dialogue_id, limit=limit, before=before, ascending=False)
        return {"turns": list(reversed(turns)), "total": self.store.count_turns(dialogue_id)}

    def get_turn(self, dialogue_id: str, turn_id: str) -> dict[str, Any]:
        self._require_dialogue(dialogue_id)
        turn = self.store.get_turn(dialogue_id, turn_id)
        if turn is None:
            raise KeyError(turn_id)
        return turn

    # -- task memory ------------------------------------------------------
    def get_memory(self, dialogue_id: str) -> dict[str, Any]:
        self._require_dialogue(dialogue_id)
        return self.store.get_memory(dialogue_id)

    def patch_memory(
        self,
        dialogue_id: str,
        *,
        expected_version: Any,
        operations: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        self._require_dialogue(dialogue_id)
        if expected_version is None:
            raise InvalidRequest("expected_version is required.")
        current = task_memory(self.store.get_memory(dialogue_id))
        if current.version != int(expected_version):
            raise MemoryConflict(
                "Task memory changed since it was read; reload and retry.",
                details={"expected_version": int(expected_version), "current_version": current.version},
            )
        typed = [MemoryOperation.from_dict(item) for item in operations]
        if not typed:
            raise InvalidRequest("operations must not be empty.")
        known = self._known_user_turn_ids(dialogue_id)
        updated, report = validate_patch(
            current, typed, known_user_turn_ids=known, now=_utcnow()
        )
        if not report["applied"]:
            reason = (
                (report.get("clarification") or {}).get("reason")
                or (report["rejected"][0].get("reason") if report["rejected"] else "not_confirmed")
            )
            raise InvalidMemoryOperation(
                "No memory operation could be confirmed.", details={"reason": reason, **report}
            )
        if report["rejected"]:
            raise InvalidMemoryOperation(
                "Some memory operations were rejected.",
                details=report,
            )
        saved = self.store.save_memory(
            dialogue_id, updated.to_dict(), expected_version=current.version
        )
        saved = dict(saved)
        saved["applied"] = report["applied"]
        return saved

    # -- one dialogue turn ------------------------------------------------
    def ask(self, dialogue_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        request = dict(request)
        dialogue = self._require_dialogue(dialogue_id)
        client_turn_id = str(request.get("client_turn_id") or "").strip()
        if not client_turn_id:
            raise InvalidRequest("client_turn_id is required.")
        question = str(request.get("question") or "").strip()
        if not question:
            raise InvalidRequest("question must not be empty.")
        if len(question) > 2000:
            raise InvalidRequest("question must be at most 2000 characters.")

        existing = self.store.get_turn_by_client_id(dialogue_id, client_turn_id)
        if existing is not None:
            return existing

        turn_id = "turn-" + uuid.uuid4().hex
        created_at = _utcnow()
        history = list(reversed(self.store.list_turns(dialogue_id, limit=500, ascending=False)))
        memory_before = task_memory(self.store.get_memory(dialogue_id))

        resolution = self.resolver.resolve(question, history, memory_before.to_dict())
        known_user_ids = self._known_user_turn_ids(dialogue_id)
        known_user_ids.add(turn_id)

        if resolution.ambiguous:
            turn = self._clarification_turn(
                dialogue, turn_id, client_turn_id, question, resolution, memory_before, created_at
            )
            return self.store.append_turn(turn)

        proposal = extract(
            memory_before,
            question,
            ground_turn_id=turn_id,
            known_user_turn_ids=known_user_ids,
            now=created_at,
        )
        memory_after, report = validate_patch(
            memory_before,
            proposal["operations"],
            known_user_turn_ids=known_user_ids,
            now=created_at,
        )
        if not report["applied"] and proposal.get("clarification"):
            clarification = proposal["clarification"]
            memory_resolution = ReferenceResolution(
                original_query=question,
                search_query=question,
                ambiguous=True,
                clarification_question=clarification.get("question"),
                reason=clarification.get("reason"),
            )
            turn = self._clarification_turn(
                dialogue, turn_id, client_turn_id, question, memory_resolution, memory_before, created_at
            )
            return self.store.append_turn(turn)

        # Correction (A12): detect a task-state summary turn before generation so
        # a documentary refusal or citation failure on it can be replaced by a
        # substantive summary built from the confirmed task memory.
        task_state_summary = is_task_state_summary_request(question, memory_after)
        # A pure explicit memory directive only needs an acknowledgement, not
        # an ungrounded essay that could contaminate subsequent factual turns.
        pure_memory_directive = bool(report["applied"]) and bool(re.fullmatch(
            r"(?:цель:|моя цель:|условие:|термин:|уточнение:|goal:|constraint:|term:|clarification:"
            r"|измени условие|замени условие|отмени условие|убери условие|теперь)\s*[^?!;.\n]+[.!]?",
            question, re.IGNORECASE,
        ))
        selected = self._select_history(history)
        chat_request = self._chat_request(request, resolution, memory_after, selected)
        if task_state_summary or pure_memory_directive:
            chat_request["_task_state_answer"] = build_task_state_summary(memory_after, question)
        error: dict[str, Any] | None = None
        record: dict[str, Any] | None = None
        try:
            record = self.chat.conversation_turn(chat_request)
        except ContextOverflow:
            # SPEC D25 6.5/9.2: the mandatory part (goal and active constraints)
            # does not fit before the model call; surface the typed 422 instead
            # of persisting a turn that was never generated.
            raise
        except Exception as exc:  # noqa: BLE001 - the turn is persisted with a typed error
            from ..domain.errors import KnowledgeError

            if isinstance(exc, KnowledgeError):
                error = exc.to_dict()
                record = getattr(exc, "conversation_observation", None)
            else:
                error = {
                    "code": "internal_error",
                    "message": f"Unexpected dialogue error ({type(exc).__name__}).",
                }

        turn = self._assemble_turn(
            dialogue=dialogue,
            turn_id=turn_id,
            client_turn_id=client_turn_id,
            question=question,
            resolution=resolution,
            memory_before=memory_before,
            memory_after=memory_after,
            selected_history=selected,
            record=record,
            error=error,
            request=request,
            created_at=created_at,
            task_state_summary=task_state_summary,
        )
        # A confirmed memory item and its user-turn grounds are committed
        # together. Duplicates remain idempotent even under concurrency.
        changed = memory_after.to_dict() if memory_after.version != memory_before.version else None
        return self.store.commit_turn(turn, changed, expected_version=memory_before.version)

    # -- helpers ----------------------------------------------------------
    def _require_dialogue(self, dialogue_id: str) -> dict[str, Any]:
        dialogue = self.store.get_dialogue(dialogue_id)
        if dialogue is None:
            raise DialogueNotFound(
                "The requested dialogue was not found.", details={"dialogue_id": dialogue_id}
            )
        return dialogue

    def _known_user_turn_ids(self, dialogue_id: str) -> set[str]:
        return self.store.user_turn_ids(dialogue_id)

    def _select_history(self, turns: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        estimate = self.chat.budget.estimate
        selected: list[dict[str, Any]] = []
        tokens = 0
        for turn in reversed(list(turns)):
            messages = history_messages([turn])
            cost = sum(estimate(message.content) for message in messages)
            if selected and (len(selected) >= self.history_max_turns or tokens + cost > self.history_max_tokens):
                break
            selected.insert(0, dict(turn))
            tokens += cost
            if len(selected) >= self.history_max_turns:
                break
        return selected

    def _chat_request(
        self,
        request: Mapping[str, Any],
        resolution: ReferenceResolution,
        memory: TaskMemory,
        history: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        mode = request.get("mode") or "with_rag"
        if mode not in ("with_rag", "without_rag"):
            raise InvalidRequest("mode must be 'with_rag' or 'without_rag'.")
        breadth = request.get("top_k") if request.get("top_k") is not None else 10
        payload: dict[str, Any] = {
            "mode": mode,
            "question": resolution.search_query,
            "original_query": resolution.original_query,
            "top_k": breadth,
            "prefilter_top_k": request.get("prefilter_top_k") if request.get("prefilter_top_k") is not None else breadth,
            "postfilter_top_k": request.get("postfilter_top_k") if request.get("postfilter_top_k") is not None else breadth,
            "use_filter": request.get("use_filter"),
            "use_rewrite": request.get("use_rewrite"),
            "min_score": request.get("min_score"),
            "grounding": request.get("grounding"),
            "max_context_tokens": request.get("max_context_tokens"),
            "memory_text": build_memory_block(memory.to_dict()),
            "history_messages": history_messages(history),
            "save_run": False,
        }
        if mode == "with_rag":
            payload["collection_id"] = request.get("collection_id")
            payload["index_version_id"] = request.get("index_version_id")
            payload["strategy"] = request.get("strategy")
            if not payload["collection_id"]:
                raise InvalidRequest("collection_id is required for mode=with_rag.")
        return payload

    def _clarification_turn(
        self,
        dialogue: Mapping[str, Any],
        turn_id: str,
        client_turn_id: str,
        question: str,
        resolution: ReferenceResolution,
        memory: TaskMemory,
        created_at: str,
    ) -> dict[str, Any]:
        settings = self._base_settings(dialogue)
        return {
            "schema_version": "dialogue-turn-v1",
            "turn_id": turn_id,
            "dialogue_id": dialogue["dialogue_id"],
            "ordinal": None,
            "client_turn_id": client_turn_id,
            "created_at": created_at,
            "user_message": question,
            "original_query": question,
            "search_query": resolution.search_query,
            "reference_resolution": {
                "used_history": resolution.used_history,
                "used_memory": resolution.used_memory,
                "ambiguous": True,
                "clarification_requested": True,
                # SPEC D25 11.2: an ambiguous reference exposes the clarifying
                # question, not only the boolean flag.
                "clarification_question": resolution.clarification_question,
                "reason": resolution.reason,
            },
            "memory_before": {"version": memory.version},
            "memory_after": {"version": memory.version},
            "settings": settings,
            "retrieval_performed": False,
            "retrieval": {
                "found_count": 0,
                "selected_count": 0,
                "passed_count": 0,
                "passed_chunk_ids": [],
                "exclusion_reasons": {},
            },
            "context": self._context_projection(
                memory, history_turns=0, parts=None, reduction=False
            ),
            "sources": [],
            "citations": [],
            "answer": {
                "text": resolution.clarification_question or "Could you clarify your question?",
                "finish_reason": None,
                "truncated": False,
                "insufficient_sources": False,
                "grounding_status": None,
            },
            "status": "clarification",
            "error": None,
            "usage": None,
            "latency_ms": {"retrieval": 0.0, "context": 0.0, "chat": 0.0, "total": 0.0},
        }

    def _assemble_turn(
        self,
        *,
        dialogue: Mapping[str, Any],
        turn_id: str,
        client_turn_id: str,
        question: str,
        resolution: ReferenceResolution,
        memory_before: TaskMemory,
        memory_after: TaskMemory,
        selected_history: Sequence[Mapping[str, Any]],
        record: Mapping[str, Any] | None,
        error: Mapping[str, Any] | None,
        request: Mapping[str, Any],
        created_at: str,
        task_state_summary: bool = False,
    ) -> dict[str, Any]:
        settings = self._base_settings(dialogue)
        if record is not None:
            settings = self._record_settings(record, request)
        else:
            settings.update(self._request_settings(request))
        if record is None:
            answer = {
                "text": "",
                "finish_reason": None,
                "truncated": False,
                "insufficient_sources": False,
                "grounding_status": None,
            }
            status = "error"
            retrieval = {
                "found_count": 0,
                "selected_count": 0,
                "passed_count": 0,
                "passed_chunk_ids": [],
                "exclusion_reasons": {},
            }
            context = self._context_projection(
                memory_after, history_turns=len(selected_history), parts=None, reduction=False
            )
            sources: list[dict[str, Any]] = []
            citations: list[dict[str, Any]] = []
            usage = None
            latency = {"retrieval": None, "context": None, "chat": None, "total": None}
        else:
            raw_answer = record.get("answer") or {}
            retrieval = self._retrieval_projection(record.get("retrieval") or {})
            sources = self._sources(record.get("retrieval") or {})
            citations = self._citations(raw_answer)
            grounding = raw_answer.get("grounding") or {}
            status = self._answer_status(raw_answer, grounding, record)
            # Correction defects 4/5: never surface a bare service marker as the
            # answer. A legitimate insufficient-context refusal and a citation
            # failure both get a clear, understandable user-facing message.
            display_text = raw_answer.get("text") or ""
            if status == "refused":
                display_text = _refusal_message(question)
                # A refusal is a non-answer: never attach citations that would
                # contradict the refusal (consistency C07).
                citations = []
            elif status == "citation_failed":
                display_text = _citation_failure_message(question)
            # Correction (A12): a task-state summary is answered from the
            # confirmed task memory. A documentary insufficiency or citation
            # failure on such a turn is not a refusal; replace it with a
            # substantive summary and keep the turn internally consistent.
            task_state = bool(raw_answer.get("task_state_summary"))
            if task_state:
                display_text = build_task_state_summary(memory_after, question)
                status = "ok"
                citations = []
            answer = {
                "text": display_text,
                "finish_reason": raw_answer.get("finish_reason"),
                "truncated": bool(raw_answer.get("truncated")),
                "insufficient_sources": bool(raw_answer.get("insufficient_sources")) and not task_state,
                "grounding_status": None if task_state else grounding.get("status"),
            }
            if raw_answer.get("grounding") is not None and not task_state:
                answer["grounding"] = dict(raw_answer["grounding"])
            if task_state:
                answer["task_state_summary"] = True
                answer["origin"] = "confirmed_task_memory"
                answer["generation_performed"] = False
            if raw_answer.get("generation_diagnostics"):
                answer["generation_diagnostics"] = raw_answer["generation_diagnostics"]
            context = self._turn_context(record.get("context") or {}, memory_after, len(selected_history))
            usage = record.get("usage")
            latency = record.get("latency_ms") or {"retrieval": 0.0, "context": 0.0, "chat": 0.0, "total": 0.0}
        return {
            "schema_version": "dialogue-turn-v1",
            "turn_id": turn_id,
            "dialogue_id": dialogue["dialogue_id"],
            "ordinal": None,
            "client_turn_id": client_turn_id,
            "created_at": created_at,
            "user_message": question,
            "original_query": resolution.original_query,
            "search_query": resolution.search_query,
            "reference_resolution": {
                "used_history": resolution.used_history,
                "used_memory": resolution.used_memory,
                "ambiguous": resolution.ambiguous,
                "clarification_requested": bool(resolution.clarification_question),
                "clarification_question": resolution.clarification_question,
                "reason": resolution.reason,
            },
            "memory_before": {"version": memory_before.version},
            "memory_after": memory_after.to_dict(),
            "settings": settings,
            "retrieval_performed": record is not None and record.get("mode") == "with_rag",
            "retrieval": retrieval,
            "context": context,
            "sources": sources,
            "citations": citations,
            "answer": answer,
            "status": status,
            "error": dict(error) if error else None,
            "usage": usage,
            "latency_ms": latency,
        }

    def _base_settings(self, dialogue: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "mode": "with_rag",
            "index_version_id": None,
            "collection_id": None,
            "strategy": None,
            "top_k": None,
            "prefilter_top_k": None,
            "postfilter_top_k": None,
            "use_filter": None,
            "use_rewrite": None,
            "min_score": None,
            "grounding": True,
            "model": None,
        }

    def _request_settings(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Safe intended configuration when failure precedes a completed plan."""
        breadth = request.get("top_k") if request.get("top_k") is not None else 10
        model = self.chat.chat_model
        mode = request.get("mode") or "with_rag"
        return {
            "mode": mode,
            "collection_id": request.get("collection_id"),
            "index_version_id": request.get("index_version_id"),
            "strategy": request.get("strategy"),
            "top_k": breadth,
            "prefilter_top_k": request.get("prefilter_top_k") if request.get("prefilter_top_k") is not None else breadth,
            "postfilter_top_k": request.get("postfilter_top_k") if request.get("postfilter_top_k") is not None else breadth,
            "use_filter": request.get("use_filter") if request.get("use_filter") is not None else self.chat.rag_filter_enabled,
            "use_rewrite": request.get("use_rewrite") if request.get("use_rewrite") is not None else self.chat.rag_rewrite_enabled,
            "min_score": request.get("min_score") if request.get("min_score") is not None else self.chat.rag_min_score,
            "grounding": bool(request.get("grounding") if request.get("grounding") is not None else self.chat.grounding_enabled) and mode == "with_rag",
            "model": {"provider": getattr(model, "provider", None), "model": getattr(model, "model", None)},
        }

    def _record_settings(
        self, record: Mapping[str, Any], request: Mapping[str, Any]
    ) -> dict[str, Any]:
        index = record.get("index") or {}
        breadth = request.get("top_k") if request.get("top_k") is not None else 10
        return {
            "mode": record.get("mode"),
            "index_version_id": index.get("index_version_id"),
            "collection_id": index.get("collection_id") or request.get("collection_id"),
            "strategy": index.get("strategy"),
            "top_k": breadth,
            "prefilter_top_k": request.get("prefilter_top_k") if request.get("prefilter_top_k") is not None else breadth,
            "postfilter_top_k": request.get("postfilter_top_k") if request.get("postfilter_top_k") is not None else breadth,
            "use_filter": record.get("use_filter"),
            "use_rewrite": record.get("use_rewrite"),
            "min_score": record.get("min_score"),
            "grounding": bool(request.get("grounding") if request.get("grounding") is not None else self.chat.grounding_enabled) and record.get("mode") == "with_rag",
            "model": record.get("model"),
        }

    def _retrieval_projection(self, retrieval: Mapping[str, Any]) -> dict[str, Any]:
        passed = retrieval.get("passed") or []
        return {
            "found_count": retrieval.get("found_count", 0),
            "selected_count": retrieval.get("selected_count", 0),
            "passed_count": retrieval.get("passed_count", 0),
            "passed_chunk_ids": [item.get("chunk_id") for item in passed],
            "exclusion_reasons": retrieval.get("exclusion_reasons") or {},
        }

    def _sources(self, retrieval: Mapping[str, Any]) -> list[dict[str, Any]]:
        # SPEC D22 6.2 freezes the ``passed`` projection without ``score``; the
        # real cosine score lives on the found/selected candidate projection
        # (chat_service._candidate_projection). Join by chunk_id so the D25
        # sources keep the score required by SPEC D25 6.2 without changing D22.
        scores: dict[Any, Any] = {}
        for item in retrieval.get("candidates") or retrieval.get("selected") or []:
            chunk_id = item.get("chunk_id")
            if chunk_id is not None and item.get("score") is not None:
                scores[chunk_id] = item.get("score")
        sources: list[dict[str, Any]] = []
        for item in retrieval.get("passed") or []:
            metadata = item.get("metadata") or {}
            chunk_id = item.get("chunk_id")
            score = item.get("score")
            if score is None:
                score = scores.get(chunk_id)
            if score is None:
                score = metadata.get("score")
            sources.append(
                {
                    "chunk_id": chunk_id,
                    "source": metadata.get("source_label") or metadata.get("source_uri"),
                    "section": metadata.get("section_path"),
                    "page_start": metadata.get("page_start"),
                    "page_end": metadata.get("page_end"),
                    "rank": item.get("rank"),
                    "score": score,
                }
            )
        return sources

    def _citations(self, answer: Mapping[str, Any]) -> list[dict[str, Any]]:
        grounding = answer.get("grounding") or {}
        citations = grounding.get("citations")
        if citations is not None:
            return [dict(citation) for citation in citations]
        # The non-grounded RAG path reports inline citations as a
        # ``{"valid": [chunk_id...], "unsupported": [...]}`` projection.
        raw = answer.get("citations") or {}
        result: list[dict[str, Any]] = []
        for item in raw.get("valid") or []:
            if isinstance(item, Mapping):
                result.append(dict(item))
            else:
                result.append({"chunk_id": str(item), "status": "cited"})
        return result

    def _answer_status(
        self,
        answer: Mapping[str, Any],
        grounding: Mapping[str, Any],
        record: Mapping[str, Any],
    ) -> str:
        if answer.get("truncated") or answer.get("finish_reason") == "length":
            return "incomplete"
        if not str(answer.get("text") or "").strip():
            return "error"
        # Correction defect 4: an explicit insufficient-context signal is a
        # legitimate refusal; a grounding failure means the documents were passed
        # but the citations/answer could not be confirmed, which is not a refusal.
        if answer.get("insufficient_sources") or grounding.get("status") == "refused":
            return "refused"
        if grounding.get("status") == "failed":
            return "citation_failed"
        return "ok"

    def _turn_context(
        self,
        context: Mapping[str, Any],
        memory: TaskMemory,
        history_turns: int,
    ) -> dict[str, Any]:
        return {
            "heuristic": context.get("budget_method") or "heuristic-v1",
            "max_context_tokens": context.get("max_context_tokens"),
            "reserve_tokens": self._reserve_tokens(),
            "prompt_tokens_estimated": context.get("prompt_tokens_estimated", 0),
            "mandatory_parts": context.get("mandatory_parts") or {},
            "history_turns_used": history_turns,
            "goal_included": bool(memory.goal and memory.goal.get("text")),
            "active_constraints_included": len(memory.active_constraints()),
            "reduction_applied": bool(context.get("dropped_chunks")),
        }

    def _context_projection(
        self,
        memory: TaskMemory,
        *,
        history_turns: int,
        parts: Mapping[str, Any] | None,
        reduction: bool,
    ) -> dict[str, Any]:
        return {
            "heuristic": "heuristic-v1",
            "max_context_tokens": None,
            "reserve_tokens": self._reserve_tokens(),
            "prompt_tokens_estimated": 0,
            "mandatory_parts": dict(parts or {}),
            "history_turns_used": history_turns,
            "goal_included": bool(memory.goal and memory.goal.get("text")),
            "active_constraints_included": len(memory.active_constraints()),
            "reduction_applied": reduction,
        }

    def _reserve_tokens(self) -> int:
        budget = getattr(self.chat, "budget", None)
        reserved = int(getattr(self.chat, "reserved_output_tokens", 0) or 0)
        margin = int(getattr(budget, "safety_margin", 0) or 0)
        return reserved + margin


__all__ = ["ConversationService"]
