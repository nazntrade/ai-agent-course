"""Run the 10-question D22 eval set in both modes and write a summary.

Works against a running backend over HTTP (real Ollama for LIVE, the local
stubs for offline). It never calls a model by itself: every number comes from
the backend run records, so stub runs are never presented as LIVE inference.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent.parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))
from harness.live_policy import report_live_blocked
QUESTIONS_PATH = MODULE_DIR / "eval" / "d22" / "questions.json"

HOST = os.environ.get("KNOWLEDGE_HOST", "127.0.0.1")
PORT = os.environ.get("KNOWLEDGE_PORT", "8770")
BASE = os.environ.get("KNOWLEDGE_BASE_URL", f"http://{HOST}:{PORT}")
RUNS_PATH = Path(os.environ.get("CHAT_RUNS_PATH", str(MODULE_DIR / "local-data" / "chat-runs")))

_REFUSAL_MARKERS = (
    "not available",
    "not contain",
    "no information",
    "unknown",
    "cannot find",
    "don't know",
    "do not know",
    "не содерж",
    "неизвест",
    "не знаю",
    "нет информа",
)


def request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as response:
            body = response.read()
            return response.status, (json.loads(body.decode("utf-8")) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, (json.loads(body.decode("utf-8")) if body else {})


def load_questions(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def pick_collection(base: str) -> str:
    _, listing = request("GET", "/api/collections")
    for collection in listing.get("collections", []):
        collection_id = collection.get("collection_id")
        if not collection_id:
            continue
        _, versions = request("GET", f"/api/collections/{collection_id}/index-versions")
        if any(version.get("status") == "ready" for version in versions.get("index_versions", [])):
            return collection_id
    raise SystemExit("No collection with a ready index was found.")


def _answer_text(record: dict[str, Any]) -> str:
    return str((record.get("answer") or {}).get("text") or "")


def _invalid_answer_reason(record: dict[str, Any]) -> str | None:
    """Return why a run record is not a valid pair answer, or ``None``.

    A non-empty ``errors`` list is a provider/persistence failure; a missing,
    non-string or blank ``answer.text`` is an empty answer. Either way the pair
    must never count toward PASS.
    """
    errors = record.get("errors")
    if errors:
        if isinstance(errors, list):
            first = errors[0]
            if isinstance(first, dict):
                return str(first.get("code") or first.get("message") or "provider error")
            return str(first)
        return str(errors)
    answer = record.get("answer")
    text = answer.get("text") if isinstance(answer, dict) else None
    if not isinstance(text, str) or not text.strip():
        finish_reason = answer.get("finish_reason") if isinstance(answer, dict) else None
        return f"empty answer text (finish_reason={finish_reason})"
    return None


def _section_hit(record: dict[str, Any], expected: list[str]) -> bool:
    retrieval = record.get("retrieval") or {}
    passed = retrieval.get("passed") or []
    wanted = [item.lower() for item in expected]
    for item in passed:
        section = str((item.get("metadata") or {}).get("section_path") or "").lower()
        if any(needle in section or section in needle for needle in wanted):
            return True
    return False


def _page_hit(record: dict[str, Any], expected: list[int]) -> bool:
    retrieval = record.get("retrieval") or {}
    passed = retrieval.get("passed") or []
    wanted = set(int(page) for page in expected)
    for item in passed:
        page = (item.get("metadata") or {}).get("page_start")
        if isinstance(page, int) and page in wanted:
            return True
    return False


def run(base: str, collection_id: str | None, live: bool) -> int:
    # A non-LIVE label does not prevent these requests from invoking a real backend.
    if report_live_blocked('RAG_EVAL_PAIRS_STATUS'):
        return 3
    data = load_questions(QUESTIONS_PATH)
    questions = data.get("questions", [])
    if len(questions) != 10:
        print(f"RAG_EVAL_PAIRS_STATUS: FAIL (expected 10 questions, found {len(questions)})")
        return 1

    if collection_id is None:
        collection_id = pick_collection(base)
    print(f"RAG_EVAL: base={base} collection={collection_id} questions={len(questions)} live={live}")

    pairs: list[dict[str, Any]] = []
    section_hits = page_hits = section_total = page_total = 0
    fact_hits = fact_total = 0
    unsupported_total = citations_total = 0
    unanswerable_result: dict[str, Any] = {}
    model_name = os.environ.get("CHAT_MODEL", "")
    index_version_id: str | None = None

    for item in questions:
        question_id = item["question_id"]
        question = item["question"]
        top_k = item.get("top_k", 5)
        results: dict[str, dict[str, Any]] = {}
        for mode in ("with_rag", "without_rag"):
            payload = {"mode": mode, "question": question, "top_k": top_k, "save_run": True}
            if mode == "with_rag":
                payload["collection_id"] = collection_id
            status, record = request("POST", "/api/chat", payload)
            if status != 200:
                print(f"RAG_EVAL_PAIRS_STATUS: FAIL ({question_id} {mode}: {status} {record})")
                return 1
            reason = _invalid_answer_reason(record)
            if reason is not None:
                print(f"RAG_EVAL_PAIRS_STATUS: FAIL ({question_id} {mode}: {reason})")
                return 1
            results[mode] = record

        with_rag = results["with_rag"]
        model_name = model_name or (with_rag.get("model") or {}).get("model") or ""
        index_version_id = index_version_id or (with_rag.get("index") or {}).get("index_version_id")
        answer = _answer_text(with_rag).lower()
        expected_sections = item.get("expected_sections") or []
        expected_pages = item.get("expected_pages") or []
        if item.get("answerable", True):
            if expected_sections:
                section_total += 1
                section_hits += 1 if _section_hit(with_rag, expected_sections) else 0
            if expected_pages:
                page_total += 1
                page_hits += 1 if _page_hit(with_rag, expected_pages) else 0
            for fact in item.get("expected_facts") or []:
                fact_total += 1
                if fact.lower() in answer:
                    fact_hits += 1
        else:
            refused = any(marker in answer for marker in _REFUSAL_MARKERS)
            unanswerable_result = {"question_id": question_id, "correctly_refused": refused}

        citations = (with_rag.get("answer") or {}).get("citations") or {}
        citations_total += len(citations.get("valid") or []) + len(citations.get("unsupported") or [])
        unsupported_total += len(citations.get("unsupported") or [])

        for mode in ("with_rag", "without_rag"):
            record = results[mode]
            results[mode] = {
                "run_id": record.get("run_id"),
                "truncated": (record.get("answer") or {}).get("truncated"),
                "usage": record.get("usage"),
            }
        pairs.append({"question_id": question_id, "with_rag": results["with_rag"], "without_rag": results["without_rag"]})

    summary = {
        "schema_version": "rag-eval-summary-v1",
        "model_check_kind": ("NETWORK" if ((with_rag.get("model") or {}).get("settings") or {}).get("model_check_kind") == "remote" else "LOCAL") if live else "MOCK",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "corpus_label": data.get("corpus", {}).get("label"),
        "model": model_name,
        "index_version_id": index_version_id,
        "questions_total": len(questions),
        "retrieval": {
            "section_hit_rate": round(section_hits / section_total, 4) if section_total else None,
            "page_hit_rate": round(page_hits / page_total, 4) if page_total else None,
        },
        "content": {"facts_present_rate": round(fact_hits / fact_total, 4) if fact_total else None},
        "sources": {
            "unsupported_citation_rate": round(unsupported_total / citations_total, 4)
            if citations_total
            else 0.0
        },
        "unanswerable": unanswerable_result,
        "pairs": pairs,
    }

    RUNS_PATH.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = RUNS_PATH / f"eval-summary-{stamp}.json"
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"RAG_EVAL_SUMMARY: {json.dumps(summary['retrieval']) } {json.dumps(summary['content'])} {json.dumps(summary['sources'])}")
    print(f"RAG_EVAL_SUMMARY_FILE: {output.name}")
    print("RAG_EVAL_PAIRS_STATUS: PASS")
    return 0


def main(argv: list[str] | None = None) -> int:
    global BASE
    if report_live_blocked('RAG_EVAL_PAIRS_STATUS'):
        return 3
    parser = argparse.ArgumentParser(description="Run the D22 eval question set in both modes.")
    parser.add_argument("--base-url", default=BASE)
    parser.add_argument("--collection", default=None)
    parser.add_argument("--live", action="store_true", help="Expected to hit a real local model")
    args = parser.parse_args(argv)
    BASE = args.base_url.rstrip("/")
    status, health = request("GET", "/api/health")
    if status != 200:
        print(f"RAG_EVAL_PAIRS_STATUS: FAIL (backend health {status})")
        return 1
    chat = health.get("chat") or {}
    if args.live and not (chat.get("reachable") and chat.get("model_present")):
        print("RAG_EVAL_PAIRS_STATUS: BLOCKED (live requested but no reachable chat model)")
        return 3
    return run(args.base_url, args.collection, args.live)


if __name__ == "__main__":
    sys.exit(main())
