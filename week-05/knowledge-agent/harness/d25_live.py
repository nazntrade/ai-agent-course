"""Trusted D25 LIVE runner: two 12-turn dialogue scenarios (A/B).

Policy is validated before any real action. The runner owns a fresh TEMP index
and dialogue database plus its own loopback backend, reuses the selected test
profile through the existing ``TestSession``/lease lifecycle, and writes the
declared artifacts under ``local-data/d25``. It never touches the working
database, never stops a model process and never prints secrets.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

MODULE_DIR = Path(__file__).resolve().parent.parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from harness.live_policy import report_live_blocked  # noqa: E402
from harness.owned_backend import isolated_backend_settings  # noqa: E402
from harness.rag_eval_live import (  # noqa: E402
    RunnerBlocked,
    cleanup_temp,
    is_stub,
    selected_source,
    verify_chat_identity,
)
from harness.test_profile import SessionError, TestSession  # noqa: E402
from knowledge_agent.__main__ import (  # noqa: E402
    build_chat_service,
    build_conversation_service,
    build_service,
)
from knowledge_agent.api.app import create_app  # noqa: E402
from knowledge_agent.chat.test_profile import load_test_profile  # noqa: E402
from knowledge_agent.config import load_settings, d25_generation_settings  # noqa: E402

EXPECTATIONS_SCHEMA = "d25-expectations-v1"
SCENARIO_SCHEMA = "d25-scenario-v1"
SCENARIO_A = "a"
SCENARIO_B = "b"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def request(method: str, url: str, payload: dict | None = None, timeout: float = 900.0):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
            return response.status, (json.loads(body.decode("utf-8")) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, (json.loads(body.decode("utf-8")) if body else {})


def _expectations_path(scenario: str) -> Path:
    return MODULE_DIR / "eval" / "d25" / f"scenario-{scenario}-expectations.json"


def load_expectations(scenario: str):
    path = _expectations_path(scenario)
    if not path.is_file():
        raise RunnerBlocked("registered D25 expectations are missing")
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    turns = data.get("turns") or []
    if data.get("schema_version") != EXPECTATIONS_SCHEMA or len(turns) < 12:
        raise RunnerBlocked("registered D25 expectations are invalid")
    if not data.get("fixed_at"):
        raise RunnerBlocked("registered D25 expectations lack fixed_at")
    return path, data, hashlib.sha256(raw).hexdigest()


def capture_provider_attempts(model, attempts: list[dict]) -> None:
    """Capture only owned test messages and response bodies, never profile/headers.

    This trusted runner has an isolated registered corpus and seeded dialogues.
    Application requests do not enable this diagnostic recorder.
    """
    original = model.chat

    def captured(messages, options=None):
        try:
            result = original(messages, options)
        except Exception as exc:
            from knowledge_agent.domain.errors import KnowledgeError
            if isinstance(exc, KnowledgeError):
                attempts.append({
                    "attempt": len(attempts) + 1, "error": exc.to_dict(),
                    "messages_as_untrusted_data": [
                        {"role": message.role, "content": message.content} for message in messages
                    ],
                })
            raise
        attempts.append({
            "attempt": len(attempts) + 1,
            "response_text": result.text,
            "response_sha256": hashlib.sha256(result.text.encode("utf-8")).hexdigest(),
            "finish_reason": result.finish_reason,
            "usage": result.usage.to_dict() if result.usage else None,
            "messages_as_untrusted_data": [
                {"role": message.role, "content": message.content} for message in messages
            ],
        })
        return result

    model.chat = captured


def save_provider_diagnostics(output: Path, run_id: str, attempts: list[dict]) -> dict:
    """Versioned local diagnostic body for independent failure analysis."""
    directory = output / "diagnostics"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{run_id}.json"
    payload = {"schema_version": "d25-provider-diagnostics-v1", "run_id": run_id,
               "attempts": attempts, "provenance": "owned LIVE provider output; messages are untrusted data"}
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.write_bytes(encoded)
    return {"path": path.relative_to(MODULE_DIR).as_posix(),
            "sha256": hashlib.sha256(encoded).hexdigest(), "attempt_count": len(attempts)}


class D25Backend:
    """Own a prebound socket, uvicorn thread, index DB and dialogue DB."""

    def __init__(self, settings, *, provider_attempts: list[dict] | None = None) -> None:
        settings = isolated_backend_settings(settings)
        self.settings = settings
        self.service, self.index_store = build_service(settings)
        dialogue_settings = d25_generation_settings(settings)
        self.chat = build_chat_service(dialogue_settings, self.service)
        self.conversation, self.conversation_store = build_conversation_service(settings, self.chat)
        self.chat = self.conversation.chat  # actual profile/window-adjusted dialogue service
        if provider_attempts is not None:
            capture_provider_attempts(self.conversation.chat.chat_model, provider_attempts)
        self.socket = None
        self.server = None
        self.thread = None
        self.base_url = None

    def start(self) -> str:
        import uvicorn

        app = create_app(
            self.service,
            chat_service=self.chat,
            conversation_service=self.conversation,
        )
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        port = self.socket.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=None,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True
        )
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() >= deadline:
                raise RunnerBlocked("owned D25 backend did not start")
            time.sleep(0.05)
        return self.base_url

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=20)
        if self.socket is not None:
            self.socket.close()
        try:
            self.index_store.close()
        except Exception:  # noqa: BLE001 - best effort release
            pass
        try:
            self.conversation_store.close()
        except Exception:  # noqa: BLE001 - best effort release
            pass


def verify_health(base: str, profile) -> None:
    target = urlsplit(base)
    if target.hostname != "127.0.0.1" or not target.port:
        raise RunnerBlocked("owned D25 backend address is invalid")
    status, health = request("GET", base + "/api/health")
    chat = health.get("chat") or {}
    if status != 200 or not chat.get("reachable") or not chat.get("model_present"):
        raise RunnerBlocked("owned D25 backend does not use the selected ready chat profile")


def build_index(service, source: Path) -> tuple[str, dict]:
    collection = service.create_collection("d25-scenario")["collection_id"]
    build = service.build(collection, [{"path": str(source)}], "structure", wait=True)
    version = service.get_index_version(build["index_version_id"])
    if version.get("status") != "ready":
        raise RunnerBlocked("registered corpus index is not ready")
    service.set_active_index(collection, version["index_version_id"])
    return collection, version


# -- scenario definitions ----------------------------------------------------

def scenario_a_turns(version_id: str) -> list[dict]:
    # Task-state directives are answered from task memory, not from documents,
    # so they are not document-grounded (correction defect 3). Documentary
    # questions below keep the full D24 grounded path (correction defect 4).
    return [
        {"question": "Цель: изучить методы обучения автономных агентов", "settings": {"grounding": False}},
        {"question": "условие: без кода", "settings": {"grounding": False}},
        {"question": "Какие подходы к обучению агентов описаны в обзоре?", "settings": {}},
        {"question": "как это связано с планированием?", "settings": {}},
        {"question": "Что описано в разделе про память агентов?", "settings": {}},
        {"question": "Цель: научиться строить RAG-ассистентов", "settings": {"grounding": False}},
        {"question": "Продолжим: какие компоненты архитектуры агента выделяет обзор?", "settings": {}, "restart": True},
        {"question": 'измени условие "без кода" на "код можно, только Python"', "settings": {"grounding": False}},
        {"question": "Сколько будет два плюс два?", "settings": {}},
        {"question": "Вернёмся к обучению агентов: что ещё важно?", "settings": {}},
        {"question": "как это связано с памятью?", "settings": {}},
        {"question": "Подведи итог с учётом всех условий", "settings": {}},
    ]


def scenario_b_turns(version_id: str) -> list[dict]:
    return [
        {"question": "Цель: составить план внедрения RAG-ассистента", "settings": {"grounding": False}},
        {"question": "условие: без облачных сервисов", "settings": {"grounding": False}},
        {"question": "условие: только локальные модели", "settings": {"grounding": False}},
        {"question": "условие: коротко", "settings": {"grounding": False}},
        {"question": "Какие компоненты нужны для RAG-системы?", "settings": {}},
        {"question": 'измени условие "коротко" на "подробно"', "settings": {"grounding": False}},
        {"question": "Какие компоненты нужны для RAG-системы?", "settings": {"use_filter": True, "min_score": 0.999, "postfilter_top_k": 1}},
        {"question": "Продолжи план внедрения", "settings": {"use_filter": False, "min_score": 0.0}},
        {"question": "Какая сегодня погода в Москве?", "settings": {}},
        {"question": "как это связано с памятью?", "settings": {}},
        {"question": "Дай итоговый план с источниками", "settings": {}},
        {"question": "Перечисли сохранённые условия", "settings": {"grounding": False}},
    ]


# -- turn execution ----------------------------------------------------------

def _ask(
    base: str,
    dialogue_id: str,
    question: str,
    settings: dict,
    collection_id: str,
    index_version_id: str,
) -> dict:
    payload = {
        "client_turn_id": "d25-" + uuid.uuid4().hex,
        "question": question,
        "mode": "with_rag",
        "collection_id": collection_id,
        "index_version_id": index_version_id,
        "top_k": int(settings.get("top_k", 10)),
        # The full D25 answer path includes the D24 grounded layer. Grounding is
        # on by default here; a scenario turn may only switch it off explicitly,
        # and the assessment never treats a disabled check as a PASS (defect 4).
        "grounding": bool(settings.get("grounding", True)),
    }
    if settings.get("use_filter") is not None:
        payload["use_filter"] = settings["use_filter"]
    if settings.get("use_rewrite") is not None:
        payload["use_rewrite"] = settings["use_rewrite"]
    if settings.get("min_score") is not None:
        payload["min_score"] = settings["min_score"]
    if settings.get("prefilter_top_k") is not None:
        payload["prefilter_top_k"] = settings["prefilter_top_k"]
    if settings.get("postfilter_top_k") is not None:
        payload["postfilter_top_k"] = settings["postfilter_top_k"]
    last: dict = {}
    # A bounded retry absorbs transient provider timeouts; a typed error is
    # still recorded honestly when both attempts fail.
    for attempt in range(2):
        payload["client_turn_id"] = "d25-" + uuid.uuid4().hex
        status, record = request(
            "POST", base + f"/api/dialogues/{dialogue_id}/turns", payload
        )
        if status == 200 and isinstance(record, dict) and record.get("status") != "error":
            record["_attempts"] = attempt + 1
            return record
        if status == 200 and isinstance(record, dict):
            last = record
        else:
            error = (record.get("error") if isinstance(record, dict) else None) or {
                "code": "http_error",
                "message": str(status),
            }
            last = {"status": "error", "error": error, "user_message": question}
        last["_attempts"] = attempt + 1
        # Formatting/contract/configuration errors are not transient. The
        # service already made its bounded format retry; do not repeat the
        # entire scenario turn three times in the hope of a lucky response.
        if (last.get("error") or {}).get("code") not in {"chat_timeout", "chat_unavailable", "embedding_timeout", "embedding_unavailable"}:
            return last
    return last


# A displayed "refusal" made only of a short service marker is not user-facing.
_REFUSAL_MARKERS = {
    "insufficient",
    "недостаточно",
    "no answer",
    "no information",
    "insufficient information",
    "n/a",
    "none",
}


def _is_user_facing_text(text: str) -> bool:
    normalized = str(text or "").strip().strip(".").lower()
    if normalized in _REFUSAL_MARKERS:
        return False
    return len(normalized) >= 15 or len(normalized.split()) >= 4


def _format_assessment(
    record: dict, answer: dict, text: str, status: str | None, technical: bool
) -> dict:
    """Automated format/evidence checks, separate from semantic assessment.

    A legitimate insufficient-context refusal passes; a citation or answer
    failure with passed documents does not. Meaning is never judged here.
    """

    if not technical:
        return {"status": "FAIL", "reason": "answer did not complete", "details": {}}
    grounding_status = answer.get("grounding_status")
    citations = record.get("citations") or []
    if status == "refused":
        if not (answer.get("insufficient_sources") and _is_user_facing_text(text)):
            return {
                "status": "FAIL",
                "reason": "refusal without a clear insufficient-context signal",
                "details": {"insufficient_sources": bool(answer.get("insufficient_sources"))},
            }
        verified = [
            citation
            for citation in citations
            if citation.get("source_exists") and citation.get("quote_verbatim")
        ]
        if verified:
            # Consistency (C07): a turn shown as a refusal must not also carry
            # verified citations, which would contradict the refusal.
            return {
                "status": "PARTIAL",
                "reason": "refusal carried verified citations",
                "details": {"verified_citations": len(verified)},
            }
        return {
            "status": "PASS",
            "reason": "expected insufficient-context refusal",
            "details": {"insufficient_sources": True},
        }
    if status == "citation_failed":
        return {
            "status": "FAIL",
            "reason": "documents were passed but citations did not verify",
            "details": {"grounding_status": grounding_status},
        }
    if status == "clarification":
        return {"status": "PASS", "reason": "clarifying question", "details": {}}
    if status == "ok":
        if answer.get("task_state_summary"):
            # A task-state summary is answered from the confirmed task memory, so
            # the documentary grounded checks do not apply to it; the summary text
            # itself is the semantic object the Tester reads.
            return {
                "status": "PASS",
                "reason": "task-state summary answered from task memory",
                "details": {"citations": len(citations)},
            }
        if grounding_status == "failed":
            return {
                "status": "FAIL",
                "reason": "grounding failed while documents were passed",
                "details": {},
            }
        unknown = [
            citation.get("chunk_id")
            for citation in citations
            if citation.get("source_exists") is False
        ]
        if unknown:
            return {
                "status": "FAIL",
                "reason": "a citation references a chunk that was not passed",
                "details": {"unknown_chunk_ids": len(unknown)},
            }
        mismatched = [
            citation.get("chunk_id")
            for citation in citations
            if citation.get("quote_verbatim") is False
        ]
        if mismatched:
            return {
                "status": "PARTIAL",
                "reason": "some citations did not verify verbatim",
                "details": {"mismatched_citations": len(mismatched)},
            }
        grounding = answer.get("grounding") or {}
        if grounding.get("inline_unsupported"):
            return {"status": "FAIL", "reason": "inline reference was not passed",
                    "details": {"unsupported_inline": len(grounding["inline_unsupported"])}}
        if grounding.get("inline_missing_quote"):
            return {"status": "PARTIAL", "reason": "inline reference lacks a verified structured quote",
                    "details": {"missing_inline_quotes": len(grounding["inline_missing_quote"])}}
        if grounding_status in ("verified", "partial"):
            if not citations:
                return {"status": "FAIL", "reason": "documentary answer has no verified quotes", "details": {}}
            if not all(c.get("source_exists") is True and c.get("quote_verbatim") is True for c in citations):
                return {"status": "PARTIAL", "reason": "citation verification metadata incomplete", "details": {}}
            # A honest semantic scope limitation is separate from formal format:
            # it does not make exact, passed quotations a verbatim mismatch.
            details = {"limitation": grounding["limitation"]} if grounding.get("limitation") else {}
            return {"status": "PASS", "reason": "citations formally verified", "details": details}
        return {"status": "PASS", "reason": "completed answer (grounding disabled)", "details": {}}
    return {"status": "NOT_ASSESSED", "reason": f"unrecognized status: {status!r}", "details": {}}


def _answer_assessment(record: dict) -> dict:
    """Split technical completion, automated format checks and semantic quality.

    A completed non-empty answer is never a substantive PASS here. The semantic
    verdict stays NOT_ASSESSED until the independent Tester supplies its own
    per-turn verdicts (correction defects 1-3).
    """

    answer = record.get("answer") or {}
    status = record.get("status")
    text = str(answer.get("text") or "").strip()
    truncated = bool(answer.get("truncated")) or answer.get("finish_reason") == "length"
    error = record.get("error")
    if error or status == "error":
        technical, technical_reason = False, "typed error or empty provider answer"
    elif not text and status != "clarification":
        technical, technical_reason = False, "empty answer text"
    elif truncated or status == "incomplete":
        technical, technical_reason = False, "truncated or incomplete answer"
    else:
        technical, technical_reason = True, None
    return {
        "technical_completion": {"completed": technical, "reason": technical_reason},
        "format_checks": _format_assessment(record, answer, text, status, technical),
        "semantic": {
            "status": "NOT_ASSESSED",
            "reason": "independent Tester assessment pending",
        },
        "truncated": bool(truncated),
    }


def scenario_verdict(turns: list[dict], semantic: dict[int, str] | None = None) -> dict:
    """Aggregate per-turn technical, format and semantic verdicts.

    Without the independent Tester's per-turn verdicts the substantive result is
    ``NOT_ASSESSED``. When verdicts are supplied, a mandatory or relevant turn
    marked PARTIAL/FAIL can never produce an overall substantive PASS
    (correction defects 2-3).
    """

    errored = [
        turn.get("turn_index")
        for turn in turns
        if not (turn.get("technical_completion") or {}).get("completed")
    ]
    format_failures = [
        turn.get("turn_index")
        for turn in turns
        if (turn.get("format_checks") or {}).get("status") == "FAIL"
    ]
    format_partials = [
        turn.get("turn_index")
        for turn in turns
        if (turn.get("format_checks") or {}).get("status") == "PARTIAL"
    ]
    technical = len(turns) >= 12 and not errored
    if not technical or format_failures:
        automated = "FAIL"
    elif format_partials:
        automated = "PARTIAL"
    else:
        automated = "PASS"

    supplied = dict(semantic or {})
    relevant: dict[object, str | None] = {}
    for turn in turns:
        index = turn.get("turn_index")
        if index in supplied:
            relevant[index] = str(supplied[index])
        elif turn.get("mandatory_answer"):
            relevant[index] = None
    values = list(relevant.values())
    if not values:
        substantive = "NOT_ASSESSED"
    elif any(value in ("FAIL", "BLOCKED") for value in values):
        substantive = "FAIL"
    elif any(value == "PARTIAL" for value in values):
        substantive = "PARTIAL"
    elif any(value is None for value in values):
        substantive = "NOT_ASSESSED"
    else:
        substantive = "PASS"
    truncated = [
        turn.get("turn_index")
        for turn in turns
        if (turn.get("answer") or {}).get("truncated")
        or (turn.get("answer") or {}).get("finish_reason") == "length"
    ]
    return {
        "turn_count": len(turns),
        "technical_completion": technical,
        "automated_checks": automated,
        "substantive_acceptance": substantive,
        "errored_turns": errored,
        "truncated_turns": truncated,
        "format_failures": format_failures,
        "format_partials": format_partials,
        "refusals": sum(1 for turn in turns if turn.get("refusal")),
    }


def _project_turn(
    record: dict, memory: dict, expected_goal: str | None, expectation: dict | None = None
) -> dict:
    retrieval = record.get("retrieval") or {}
    answer = record.get("answer") or {}
    grounding_status = answer.get("grounding_status")
    citations = record.get("citations") or []
    sources = record.get("sources") or []
    status = record.get("status")
    refusal = None
    if status == "refused":
        refusal = {
            "reason": "insufficient_sources",
            "message": answer.get("text") or "",
        }
    goal_after = (memory or {}).get("goal") or {}
    goal_retained = bool(goal_after.get("text")) if expected_goal else True
    if expected_goal:
        goal_retained = goal_after.get("text") == expected_goal
    # The runner only records the structural observation that an active
    # condition exists. Compliance with a condition is a semantic judgement:
    # it is left unset (not_checked) until an actual check is performed by the
    # independent Tester, never auto-confirmed here (defect 2).
    constraints = [
        {
            "item_id": item.get("item_id"),
            "text": item.get("text"),
            "status": item.get("status"),
            "present_in_task_memory": True,
            "respected": None,
            "compliance_check": "not_checked",
            "compliance_note": "condition compliance is a semantic judgement made by the independent Tester",
        }
        for item in (memory or {}).get("constraints") or []
        if item.get("status") == "active"
    ]
    assessment = _answer_assessment(record)
    return {
        "turn_index": record.get("_turn_index"),
        "attempts": record.get("_attempts", 1),
        "turn_id": record.get("turn_id"),
        "user_message": record.get("user_message"),
        "original_question": record.get("original_query"),
        "search_query": record.get("search_query"),
        "reference_resolution": record.get("reference_resolution"),
        "retrieval": {
            "performed": bool(record.get("retrieval_performed")),
            "fresh": bool(record.get("retrieval_performed")),
            "found_count": retrieval.get("found_count", 0),
            "selected_count": retrieval.get("selected_count", 0),
            "passed_count": retrieval.get("passed_count", 0),
            "passed_chunk_ids": retrieval.get("passed_chunk_ids") or [],
            "sources": [
                {"chunk_id": item.get("chunk_id"), "source": item.get("source"), "section": item.get("section")}
                for item in sources
            ],
        },
        "context": record.get("context"),
        "memory": memory,
        "goal_retained": goal_retained,
        "constraints_compliance": constraints,
        "structural": {
            "citations_present": bool(citations),
            "sources_present": bool(sources),
            "active_constraints_present": len(constraints),
        },
        "answer": {
            "text": answer.get("text"),
            "finish_reason": answer.get("finish_reason"),
            "truncated": bool(answer.get("truncated")),
            "insufficient_sources": bool(answer.get("insufficient_sources")),
            "grounding_status": grounding_status,
            "grounding": answer.get("grounding"),
            "task_state_summary": bool(answer.get("task_state_summary")),
            "origin": answer.get("origin"),
            "generation_performed": answer.get("generation_performed"),
            "generation_diagnostics": answer.get("generation_diagnostics") or [],
        },
        "citations": [
            dict(c) for c in citations
        ],
        "support": {
            # Presence of citations/sources is only a structural observation; it
            # proves neither claim support nor meaning. `source_backed` stays
            # unset until an actual semantic check is performed (defect 2).
            "source_backed": None,
            "citations_present": bool(citations),
            "sources_present": bool(sources),
            "checked": "not_checked",
            "notes": "semantic support is assessed by the independent Tester",
        },
        "refusal": refusal,
        "status": status,
        "technical_completion": assessment["technical_completion"],
        # Automated format/evidence checks and the (still unassessed) semantic
        # verdict are kept strictly separate (correction defects 1-3).
        "format_checks": assessment["format_checks"],
        "semantic": assessment["semantic"],
        "mandatory_answer": bool((expectation or {}).get("mandatory_answer", False)),
        "error": record.get("error"),
        "usage": record.get("usage"),
        "latency_ms": record.get("latency_ms"),
    }


def run_scenario(
    base: str,
    dialogue_id: str,
    turns: list[dict],
    collection_id: str,
    version_id: str,
    backend_ref: dict,
    settings,
    expectations_by_index: dict[int, dict] | None = None,
):
    records: list[dict] = []
    expected_goal: str | None = None
    expectations_by_index = expectations_by_index or {}
    for index, turn in enumerate(turns, start=1):
        if turn.get("restart"):
            backend_ref["backend"].stop()
            backend_ref["backend"] = D25Backend(settings, provider_attempts=backend_ref.get("provider_attempts"))
            base = backend_ref["backend"].start()
            verify_health(base, settings.test_profile)
            backend_ref["base"] = base
        record = _ask(
            base, dialogue_id, turn["question"], turn.get("settings") or {}, collection_id, version_id
        )
        record["_turn_index"] = index
        memory = request("GET", base + f"/api/dialogues/{dialogue_id}/memory")[1]
        if memory.get("goal") and memory["goal"].get("text"):
            expected_goal = memory["goal"]["text"]
        records.append(
            _project_turn(record, memory, expected_goal, expectations_by_index.get(index))
        )
        print(f"D25_TURN_{index}: {record.get('status')} grounding={(record.get('answer') or {}).get('grounding_status')}", flush=True)
    return records, base


def _run_id(scenario: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"d25-{scenario}-{stamp}-{uuid.uuid4().hex[:8]}"


def _archive_previous(output: Path, scenario: str) -> list[str]:
    """Move the previous current-run artifact into distinguishable history.

    The new run always writes ``scenario-<x>.json``; the prior artifact is kept
    under ``history/`` with its old run id/created_at in the file name so prior
    LIVE evidence is preserved and the current artifact is unambiguously bound
    to the new run (correction C08).
    """

    history_dir = output / "history"
    current = output / f"scenario-{scenario}.json"
    if current.is_file():
        try:
            existing = json.loads(current.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            existing = {}
        previous_id = str(existing.get("run_id") or existing.get("created_at") or "previous")
        safe = re.sub(r"[^0-9A-Za-z_-]+", "-", previous_id).strip("-")[:80] or "previous"
        history_dir.mkdir(parents=True, exist_ok=True)
        target = history_dir / f"scenario-{scenario}-{safe}.json"
        if not target.exists():
            target.write_bytes(current.read_bytes())
    if not history_dir.is_dir():
        return []

    def relative(path: Path) -> str:
        try:
            return path.relative_to(MODULE_DIR).as_posix()
        except ValueError:
            return path.as_posix()

    return sorted(relative(path) for path in history_dir.glob(f"scenario-{scenario}-*.json"))


def run_owned(settings, scenario: str) -> int:
    if report_live_blocked("D25_RUNNER_STATUS"):
        return 3
    if settings.test_profile is None or is_stub(settings.test_profile.name):
        print("D25_RUNNER_STATUS: BLOCKED (selected non-stub profile required; no fallback)")
        return 3

    expect_path, expectations, expect_sha = load_expectations(scenario)
    expectations_by_index = {
        int(item["turn_index"]): item
        for item in expectations.get("turns") or []
        if isinstance(item, dict) and item.get("turn_index") is not None
    }
    source, label = selected_source(settings)
    run_started = _now()
    run_id = _run_id(scenario)

    temporary_parent = Path(tempfile.gettempdir()).resolve()
    root = Path(tempfile.mkdtemp(prefix="knowledge-rag-eval-d25-", dir=temporary_parent))
    output = MODULE_DIR / "local-data" / "d25"
    previous_artifacts: list[str] = []
    cleanup_ok = True
    result = 1
    backend = None
    backend_ref = None
    provider_attempts: list[dict] = []
    diagnostics_ref = None
    try:
        output.mkdir(parents=True, exist_ok=True)
        settings = dataclasses.replace(
            d25_generation_settings(settings),
            db_path=str(root / "index.db"),
            dialogue_db_path=str(root / "conversations.db"),
            chat_runs_path=str(root / "chat-runs"),
            host="127.0.0.1",
            port=0,
            # Correction defect 4: sufficient completed-answer budget. With the
            # default 1024 output tokens the selected model truncated long
            # mandatory answers (finish_reason=length). The grounded D24 JSON
            # contract also needs a larger reserve, and the input context must
            # stay comfortably larger than that reserve.
            rag_grounding_enabled=True,
        )
        if urlsplit(settings.embed_base_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise RunnerBlocked("LIVE embedding requires a loopback endpoint")
        previous_artifacts = _archive_previous(output, scenario)

        backend = D25Backend(settings, provider_attempts=provider_attempts)
        chat_identity = verify_chat_identity(backend.chat.chat_model, settings.test_profile)
        embedding = backend.service._embedder.preflight()
        if (
            not embedding.get("reachable")
            or not embedding.get("model_present")
            or is_stub(embedding.get("version"))
            or is_stub(settings.embed_model)
        ):
            raise RunnerBlocked("real configured embedding provider is not ready")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        collection, version = build_index(backend.service, source)
        pinned = version["index_version_id"]

        base = backend.start()
        verify_health(base, settings.test_profile)

        dialogue = request("POST", base + "/api/dialogues", {"name": f"scenario-{scenario}"})[1]
        dialogue_id = dialogue["dialogue_id"]
        turns_def = scenario_a_turns(pinned) if scenario == SCENARIO_A else scenario_b_turns(pinned)
        backend_ref = {"backend": backend, "base": base, "provider_attempts": provider_attempts}
        turns, base = run_scenario(
            base,
            dialogue_id,
            turns_def,
            collection,
            pinned,
            backend_ref,
            settings,
            expectations_by_index,
        )
        backend = backend_ref["backend"]

        if scenario == SCENARIO_A:
            restart_observed = any(turn.get("restart") for turn in turns_def)
            cross_chat_leak = False
        else:
            # In-process control dialogue must stay empty (no cross-chat leak).
            control = request("POST", base + "/api/dialogues", {"name": "control"})[1]["dialogue_id"]
            control_turns = request("GET", base + f"/api/dialogues/{control}/turns")[1].get("total", 0)
            control_memory = request("GET", base + f"/api/dialogues/{control}/memory")[1]
            restart_observed = False
            cross_chat_leak = bool(control_turns or (control_memory.get("goal") or {}).get("text"))

        verdict = scenario_verdict(turns)
        diagnostics_ref = save_provider_diagnostics(output, run_id, provider_attempts)
        artifact = {
            "schema_version": SCENARIO_SCHEMA,
            "provider_diagnostics": diagnostics_ref,
            "run_id": run_id,
            "created_at": _now(),
            "scenario": scenario.upper(),
            "corpus_label": label,
            "source_sha256": digest,
            "index": {
                "collection_id": collection,
                "index_version_id": pinned,
                "strategy": version.get("strategy"),
                "fingerprint": version.get("fingerprint"),
            },
            "model": {
                "provider": chat_identity.get("provider"),
                "model": chat_identity.get("model"),
                "kind": chat_identity.get("kind"),
                "settings": chat_identity.get("settings"),
            },
            "embedding": {
                "model": version.get("model"),
                "dimension": version.get("dimension"),
                "digest": version.get("digest"),
            },
            "budget": {
                "chat_context_tokens": backend.chat.max_context_tokens,
                "chat_max_output_tokens": backend.chat.reserved_output_tokens,
                "grounding_enabled": bool(settings.rag_grounding_enabled),
            },
            "history": {"previous_artifacts": previous_artifacts},
            "expectations_ref": expect_path.relative_to(MODULE_DIR).as_posix(),
            "expectations_sha256": expect_sha,
            "pre_run": {
                "expectations_created_at": expectations.get("fixed_at"),
                "run_started_at": run_started,
            },
            "turns": turns,
            "summary": {
                "restart_observed": restart_observed,
                "cross_chat_leak": cross_chat_leak,
                "refusals": verdict["refusals"],
                "turn_count": verdict["turn_count"],
                "technical_completion": verdict["technical_completion"],
                "automated_checks": verdict["automated_checks"],
                # Semantic quality is the independent Tester's verdict; the runner
                # never upgrades it to PASS on its own (correction defect 2).
                "substantive_acceptance": verdict["substantive_acceptance"],
                "errored_turns": verdict["errored_turns"],
                "truncated_turns": verdict["truncated_turns"],
                "format_failures": verdict["format_failures"],
                "format_partials": verdict["format_partials"],
            },
        }
        (output / f"scenario-{scenario}.json").write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        label_status = scenario.upper()
        print(f"D25_SCENARIO_{label_status}_RUN_ID: {run_id}")
        print(
            f"D25_SCENARIO_{label_status}_TURNS: {verdict['turn_count']} "
            f"(errors {len(verdict['errored_turns'])}, truncated {len(verdict['truncated_turns'])})"
        )
        print(
            f"D25_SCENARIO_{label_status}_TECHNICAL_STATUS: "
            f"{'PASS' if verdict['technical_completion'] else 'FAIL'}"
        )
        print(f"D25_SCENARIO_{label_status}_FORMAT_STATUS: {verdict['automated_checks']}")
        print(
            f"D25_SCENARIO_{label_status}_SUBSTANTIVE_STATUS: "
            f"{verdict['substantive_acceptance']} (independent Tester assessment pending)"
        )
        # The trusted entrypoint succeeds only when the owned LIVE run completed
        # technically (all turns persisted, no typed error / format-parse failure).
        # Format/evidence and semantic verdicts are reported separately and are
        # never upgraded to PASS by the runner (correction defect 2).
        result = 0 if verdict["technical_completion"] else 1
    except RunnerBlocked:
        print("D25_RUNNER_STATUS: BLOCKED (provider, corpus or index validation; no fallback)")
        result = 3
    except KeyboardInterrupt:
        print("D25_RUNNER_STATUS: INTERRUPTED")
        result = 130
    except Exception as exc:  # noqa: BLE001 - never leak internals/endpoints
        print(f"D25_RUNNER_STATUS: FAIL (owned D25 run failed: {type(exc).__name__})")
        result = 1
    finally:
        if provider_attempts and diagnostics_ref is None:
            try:
                save_provider_diagnostics(output, run_id, provider_attempts)
            except OSError:
                # Failure to save diagnostics must never prevent cleanup.
                result = 1
                print("D25_DIAGNOSTICS_STATUS: FAIL (local artifact could not be saved)")
        try:
            latest = backend_ref["backend"] if backend_ref else backend
            if latest is not None:
                latest.stop()
            cleanup_temp(root, temporary_parent)
        except Exception:
            cleanup_ok = False
            result = 1
            print("D25_CLEANUP_STATUS: FAIL (owned resources retained; no shared process stopped)")
        if cleanup_ok:
            print("D25_CLEANUP_STATUS: PASS")
    model_kind = "NETWORK" if settings.test_profile.kind == "remote" else "LOCAL"
    print(f"D25_MODEL_CHECK_KIND: {model_kind}")
    print("D25_QUALITY_STATUS: NOT_ASSESSED (manual answer/source assessment required)")
    return result


def main(scenario: str | None = None, argv=None) -> int:
    if report_live_blocked("D25_RUNNER_STATUS"):
        return 3
    scenario = scenario if scenario is not None else (sys.argv[1].lower() if len(sys.argv) > 1 else "")
    if scenario not in (SCENARIO_A, SCENARIO_B):
        print("D25_RUNNER_STATUS: setup error (scenario must be 'a' or 'b')")
        return 2
    env = dict(os.environ)
    try:
        try:
            profile = load_test_profile(env)
        except ValueError:
            raise RunnerBlocked("selected chat profile is invalid") from None
        if profile is None or is_stub(profile.name):
            raise RunnerBlocked("a complete selected non-stub chat profile is required")
        settings = load_settings(env)
        with TestSession(env) as ready_env:
            settings = load_settings(ready_env)
            return run_owned(settings, scenario)
    except (RunnerBlocked, SessionError):
        print("D25_RUNNER_STATUS: BLOCKED (selected profile, corpus or lifecycle unavailable)")
        return 3
    except KeyboardInterrupt:
        print("D25_RUNNER_STATUS: INTERRUPTED")
        return 130
    except Exception:
        print("D25_RUNNER_STATUS: setup error (configuration or result storage unavailable)")
        return 2


if __name__ == "__main__":
    sys.exit(main())
