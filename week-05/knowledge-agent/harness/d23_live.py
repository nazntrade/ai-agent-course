"""Trusted D23 LIVE runner: calibrate the threshold, then compare four modes.

It owns a fresh TEMP index and its own loopback backend, reuses the selected
test profile through the existing TestSession/lease lifecycle, and writes the
declared artifacts under ``local-data/d23``. It never touches the working
database, never stops a model process and never prints secrets.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
import urllib.error
import urllib.request

MODULE_DIR = Path(__file__).resolve().parent.parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from harness import d23_eval  # noqa: E402
from harness.live_policy import report_live_blocked  # noqa: E402
from harness.rag_eval_live import (  # noqa: E402
    CleanupFailed,
    EvaluationFailed,
    OwnedBackend,
    RunnerBlocked,
    cleanup_temp,
    is_stub,
    selected_source,
    verify_chat_identity,
    verify_health,
)
from harness.test_profile import SessionError, TestSession  # noqa: E402
from knowledge_agent.__main__ import build_chat_service, build_service  # noqa: E402
from knowledge_agent.chat.test_profile import load_test_profile  # noqa: E402
from knowledge_agent.config import load_settings  # noqa: E402

D23_QUESTIONS_PATH = MODULE_DIR / "eval" / "d22" / "questions.json"
PREFILTER_TOP_K = 20
POSTFILTER_TOP_K = 5
CANDIDATE_THRESHOLDS = (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70)


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


def data_dir(settings) -> Path:
    path = Path(settings.d23_data_path)
    if not path.is_absolute():
        path = MODULE_DIR / path
    return path


def calibration_path(settings) -> Path:
    path = Path(settings.d23_calibration_questions_path)
    if not path.is_absolute():
        path = MODULE_DIR / path
    return path


def load_calibration(settings) -> tuple[dict, list[dict]]:
    path = calibration_path(settings)
    if not path.is_file():
        raise RunnerBlocked("calibration question set is missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    questions = data.get("questions") or []
    if not isinstance(questions, list) or not questions:
        raise RunnerBlocked("calibration question set is empty")
    return data, questions


def build_ready_index(service, source: Path, digest: str):
    collection = service.create_collection("d23-eval")["collection_id"]
    build = service.build(collection, [{"path": str(source)}], "structure", wait=True)
    version = service.get_index_version(build["index_version_id"])
    if version.get("status") != "ready":
        raise RunnerBlocked("registered corpus index is not ready")
    sources = (version.get("manifest") or {}).get("sources") or []
    if len(sources) != 1 or sources[0].get("content_sha256") != digest:
        raise RunnerBlocked("ready index does not match the registered source")
    service.set_active_index(collection, version["index_version_id"])
    return collection, version


def _relevant(candidate: dict, expected: list[str]) -> bool:
    section = str(candidate.get("section") or "")
    return any(needle in section or section in needle for needle in expected)


def calibrate(service, collection: str, index_version_id: str, questions: list[dict]):
    per_question: list[dict] = []
    for item in questions:
        search = service.search(
            collection,
            item["question"],
            top_k=50,
            index_version_id=index_version_id,
        )
        expected = [str(value).lower() for value in (item.get("expected_sections") or [])]
        scored = [
            {
                "chunk_id": fragment.get("chunk_id"),
                "score": float(fragment.get("score") or 0.0),
                "section": str((fragment.get("metadata") or {}).get("section_path") or "").lower(),
            }
            for fragment in (search.get("fragments") or [])
        ]
        relevant = [candidate for candidate in scored if _relevant(candidate, expected)]
        per_question.append(
            {
                "question_id": item.get("question_id"),
                "question": item.get("question"),
                "expected_sections": item.get("expected_sections") or [],
                "candidate_scores": [candidate["score"] for candidate in scored],
                "relevant_chunk_ids": [candidate["chunk_id"] for candidate in relevant],
                "scored": scored,
                "relevant": relevant,
            }
        )

    results = []
    for threshold in CANDIDATE_THRESHOLDS:
        kept = kept_relevant = dropped_relevant = 0
        for question in per_question:
            passing = [c for c in question["scored"] if c["score"] >= threshold]
            kept += len(passing)
            kept_relevant += sum(1 for c in question["relevant"] if c["score"] >= threshold)
            dropped_relevant += sum(1 for c in question["relevant"] if c["score"] < threshold)
        precision = round(kept_relevant / kept, 4) if kept else 0.0
        relevant_total = kept_relevant + dropped_relevant
        recall = round(kept_relevant / relevant_total, 4) if relevant_total else None
        results.append(
            {
                "threshold": threshold,
                "kept": kept,
                "kept_relevant": kept_relevant,
                "dropped_relevant": dropped_relevant,
                "precision": precision,
                "recall": recall,
            }
        )

    top_scores = [question["candidate_scores"][0] for question in per_question if question["candidate_scores"]]
    min_top1 = min(top_scores) if top_scores else max(CANDIDATE_THRESHOLDS)
    eligible = [value for value in CANDIDATE_THRESHOLDS if value <= min_top1] or [min(CANDIDATE_THRESHOLDS)]
    zero_drop = [value for value in eligible if _result(results, value)["dropped_relevant"] == 0]
    # Prefer the largest eligible threshold that still keeps at least one relevant
    # chunk for every calibration question: this keeps the filter active instead
    # of selecting every candidate (the D23 filtering goal).
    active = [
        value
        for value in eligible
        if all(question["scored"] for question in per_question)
        and all(any(candidate["score"] >= value for candidate in question["relevant"])
                for question in per_question)
    ]
    if active:
        chosen = max(active)
        reason = (
            "Largest eligible threshold that keeps at least one relevant chunk for every "
            "calibration question, so the relevance filter is active."
        )
    elif zero_drop:
        chosen = max(zero_drop)
        reason = (
            "Largest threshold below the smallest top-1 retrieval score that drops no "
            "relevant calibration chunk."
        )
    else:
        fewest = min(_result(results, value)["dropped_relevant"] for value in eligible)
        chosen = min(value for value in eligible if _result(results, value)["dropped_relevant"] == fewest)
        reason = (
            "No eligible threshold avoided dropping relevant chunks; chose the smallest "
            "threshold with the fewest dropped relevant chunks."
        )
    return per_question, results, chosen, reason


def _result(results: list[dict], threshold: float) -> dict:
    return next(item for item in results if item["threshold"] == threshold)


def run_comparison(base: str, collection: str, index_version_id: str, threshold: float):
    questions = json.loads(D23_QUESTIONS_PATH.read_text(encoding="utf-8")).get("questions") or []
    if len(questions) != 10:
        raise RunnerBlocked("registered D22 question set is invalid")
    answers: list[dict] = []
    first_record = None
    trace_choice = None
    trace_priority = None
    for item in questions:
        for mode_id in ("A", "B", "C", "D"):
            payload = {
                "mode": "with_rag",
                "collection_id": collection,
                "index_version_id": index_version_id,
                "question": item["question"],
                "rag_mode": mode_id,
                "prefilter_top_k": PREFILTER_TOP_K,
                "postfilter_top_k": POSTFILTER_TOP_K,
                "min_score": threshold,
                "save_run": False,
            }
            try:
                status, record = request("POST", base + "/api/chat", payload)
            except Exception as exc:  # noqa: BLE001 - report a safe type only
                raise EvaluationFailed(
                    f"request failed for {item.get('question_id')} {mode_id}: {type(exc).__name__}"
                ) from None
            if status != 200:
                code = (record.get("error") or {}).get("code") if isinstance(record, dict) else None
                raise EvaluationFailed(
                    f"chat failed for {item.get('question_id')} {mode_id}: {status} ({code or 'unknown'})"
                )
            first_record = first_record or record
            answer = d23_eval.comparison_answer(
                record, question_id=item.get("question_id"), mode=mode_id
            )
            if not (answer.get("answer_text") or "").strip():
                raise EvaluationFailed(
                    f"empty answer for {item.get('question_id')} mode {mode_id}"
                )
            answers.append(answer)
            candidates = (record.get("retrieval") or {}).get("candidates") or []
            priority = trace_priority_for(record, mode_id, candidates)
            if priority is not None and (trace_priority is None or priority < trace_priority):
                trace_choice = record
                trace_priority = priority
    if len(answers) < 40:
        raise EvaluationFailed("comparison did not produce at least 40 answers")
    return answers, first_record, trace_choice


def trace_priority_for(record, mode_id, candidates):
    """Rank trace candidates: filtered-with-threshold beats filtered beats plain.

    Returns ``None`` for a branch without candidates so it is never selected.
    This keeps ``trace-sample.json`` on an actually filtered branch (SPEC 13.4).
    """

    if not candidates:
        return None
    reasons = (record.get("retrieval") or {}).get("exclusion_reasons") or {}
    if mode_id in ("B", "D"):
        return 0 if reasons.get("threshold") else 1
    return 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_owned(settings, source: Path, label: str) -> int:
    if report_live_blocked("D23_RUNNER_STATUS"):
        return 3
    if settings.test_profile is None or is_stub(settings.test_profile.name):
        print("D23_RUNNER_STATUS: BLOCKED (selected non-stub profile required; no fallback)")
        return 3

    temporary_parent = Path(tempfile.gettempdir()).resolve()
    root = Path(tempfile.mkdtemp(prefix="knowledge-rag-eval-", dir=temporary_parent))
    output = data_dir(settings)
    store = backend = None
    cleanup_ok = True
    result = 1
    try:
        output.mkdir(parents=True, exist_ok=True)
        settings = dataclasses.replace(
            settings,
            db_path=str(root / "index.db"),
            chat_runs_path=str(root / "chat-runs"),
            host="127.0.0.1",
            port=0,
        )
        if urlsplit(settings.embed_base_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise RunnerBlocked("LIVE embedding requires a loopback endpoint")
        service, store = build_service(settings)
        # The selected reasoning model needs a larger output budget than the D22
        # default; without it the provider returns an empty answer. This bound
        # only affects this owned D23 run and stays identical across the 4 modes.
        if settings.test_profile.kind == "remote" and settings.chat_max_output_tokens < 4096:
            settings = dataclasses.replace(settings, chat_max_output_tokens=4096)
        chat = build_chat_service(settings, service)
        chat_identity = verify_chat_identity(chat.chat_model, settings.test_profile)
        embedding = service._embedder.preflight()
        if (
            not embedding.get("reachable")
            or not embedding.get("model_present")
            or is_stub(embedding.get("version"))
            or is_stub(settings.embed_model)
        ):
            raise RunnerBlocked("real configured embedding provider is not ready")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        collection, version = build_ready_index(service, source, digest)
        pinned = version["index_version_id"]

        _, calibration_questions = load_calibration(settings)
        per_question, threshold_results, chosen, reason = calibrate(
            service, collection, pinned, calibration_questions
        )
        calibration = {
            "schema_version": d23_eval.CALIBRATION_SCHEMA_VERSION,
            "created_at": _now(),
            "corpus_label": label,
            "source_sha256": digest,
            "index": {
                "collection_id": collection,
                "index_version_id": pinned,
                "strategy": version.get("strategy"),
                "fingerprint": version.get("fingerprint"),
            },
            "embedding": {
                "model": version.get("model"),
                "dimension": version.get("dimension"),
                "digest": version.get("digest"),
            },
            "calibration_set": [
                {
                    "question_id": question["question_id"],
                    "question": question["question"],
                    "expected_sections": question["expected_sections"],
                    "candidate_scores": question["candidate_scores"],
                    "relevant_chunk_ids": question["relevant_chunk_ids"],
                }
                for question in per_question
            ],
            "candidate_thresholds": list(CANDIDATE_THRESHOLDS),
            "results": threshold_results,
            "threshold": chosen,
            "selection_reason": reason,
        }
        (output / "calibration.json").write_text(
            json.dumps(calibration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        backend = OwnedBackend(service, chat)
        base = backend.start()
        verify_health(base, settings.test_profile)

        answers, first_record, trace_choice = run_comparison(base, collection, pinned, chosen)
        first_branch = first_record or {}
        comparison = {
            "schema_version": d23_eval.COMPARISON_SCHEMA_VERSION,
            "created_at": _now(),
            "corpus_label": label,
            "source_sha256": digest,
            "index": {
                "collection_id": collection,
                "index_version_id": pinned,
                "strategy": version.get("strategy"),
                "fingerprint": version.get("fingerprint"),
            },
            "model": first_branch.get("model") or chat_identity,
            "embedding": {
                "model": version.get("model"),
                "dimension": version.get("dimension"),
                "digest": version.get("digest"),
            },
            "threshold": chosen,
            "prefilter_top_k": PREFILTER_TOP_K,
            "postfilter_top_k": POSTFILTER_TOP_K,
            "modes": [
                {"id": "A", "use_filter": False, "use_rewrite": False},
                {"id": "B", "use_filter": True, "use_rewrite": False},
                {"id": "C", "use_filter": False, "use_rewrite": True},
                {"id": "D", "use_filter": True, "use_rewrite": True},
            ],
            "answers": answers,
            "summary": _summarize(answers),
        }
        (output / "comparison.json").write_text(
            json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        if trace_choice is not None:
            (output / "trace-sample.json").write_text(
                json.dumps(d23_eval.trace_sample(trace_choice), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        result = 0
        print(f"D23_THRESHOLD: {chosen}")
        print("D23_RUNNER_STATUS: COMPLETED")
    except RunnerBlocked:
        print("D23_RUNNER_STATUS: BLOCKED (provider, corpus or index validation; no fallback)")
        result = 3
    except EvaluationFailed as exc:
        print(f"D23_RUNNER_STATUS: FAIL ({exc})")
        result = 1
    except KeyboardInterrupt:
        print("D23_RUNNER_STATUS: INTERRUPTED")
        result = 130
    except Exception as exc:  # noqa: BLE001 - never leak internals/endpoints
        print(f"D23_RUNNER_STATUS: FAIL (owned D23 evaluation failed: {type(exc).__name__})")
        result = 1
    finally:
        try:
            if backend is not None:
                backend.stop()
            if store is not None:
                store.close()
            cleanup_temp(root, temporary_parent)
        except Exception:
            cleanup_ok = False
            result = 1
            print("D23_CLEANUP_STATUS: FAIL (owned resources retained; no shared process stopped)")
        if cleanup_ok:
            print("D23_CLEANUP_STATUS: PASS")
    print("D23_QUALITY_STATUS: NOT_ASSESSED (manual answer/source assessment required)")
    return result


def _summarize(answers: list[dict]) -> dict:
    def average(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None

    # Fraction of answers that actually received context: empty passed_count is a
    # miss, not an excluded sample (B/Q10 and D/Q10 have passed_count == 0).
    retrieval_hits = [
        1.0 if (answer.get("retrieval") or {}).get("passed_count") else 0.0
        for answer in answers
    ]
    unsupported = sum(len((answer.get("citations") or {}).get("unsupported") or []) for answer in answers)
    total_citations = sum(
        len((answer.get("citations") or {}).get("valid") or [])
        + len((answer.get("citations") or {}).get("unsupported") or [])
        for answer in answers
    )
    return {
        "answers_total": len(answers),
        "retrieval_passed_rate": average(retrieval_hits),
        "unsupported_citation_rate": round(unsupported / total_citations, 4) if total_citations else 0.0,
        "truncated_answers": sum(1 for answer in answers if answer.get("truncated")),
        "insufficient_sources_answers": sum(
            1 for answer in answers if answer.get("insufficient_sources")
        ),
    }


def main(argv=None) -> int:
    if report_live_blocked("D23_RUNNER_STATUS"):
        return 3
    arguments = sys.argv[1:] if argv is None else argv
    if arguments:
        print("D23_RUNNER_STATUS: setup error (this trusted mode accepts no arguments)")
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
        source, label = selected_source(settings)
        with TestSession(env) as ready_env:
            settings = load_settings(ready_env)
            return run_owned(settings, source, label)
    except (RunnerBlocked, SessionError):
        print("D23_RUNNER_STATUS: BLOCKED (selected profile, corpus or lifecycle unavailable)")
        return 3
    except KeyboardInterrupt:
        print("D23_RUNNER_STATUS: INTERRUPTED")
        return 130
    except Exception:
        print("D23_RUNNER_STATUS: setup error (configuration or result storage unavailable)")
        return 2


if __name__ == "__main__":
    sys.exit(main())
