"""Trusted D24 LIVE runner: ten grounded answers + eight edge cases.

Policy is validated before any real action; the runner owns a fresh TEMP index
and its own loopback backend, reuses the selected test profile through the
existing TestSession/lease lifecycle, and writes the declared artifacts under
``local-data/d24``. It never touches the working database, never stops a model
process and never prints secrets.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

MODULE_DIR = Path(__file__).resolve().parent.parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from harness import d24_grounding  # noqa: E402
from harness.live_policy import report_live_blocked  # noqa: E402
from harness.rag_eval_live import (  # noqa: E402
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

PREFILTER_TOP_K = 20
POSTFILTER_TOP_K = 5


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
    path = Path(settings.d24_data_path)
    if not path.is_absolute():
        path = MODULE_DIR / path
    return path


def questions_path(settings) -> Path:
    path = Path(settings.d24_questions_path)
    if not path.is_absolute():
        path = MODULE_DIR / path
    return path


def load_questions(settings) -> list[dict]:
    path = questions_path(settings)
    if not path.is_file():
        raise RunnerBlocked("registered question set is missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    questions = data.get("questions") or []
    if len(questions) != 10 or sum(1 for item in questions if not item.get("answerable")) != 1:
        raise RunnerBlocked("registered question set must be 9+1")
    return questions


def build_ready_index(service, source: Path, digest: str):
    collection = service.create_collection("d24-eval")["collection_id"]
    build = service.build(collection, [{"path": str(source)}], "structure", wait=True)
    version = service.get_index_version(build["index_version_id"])
    if version.get("status") != "ready":
        raise RunnerBlocked("registered corpus index is not ready")
    sources = (version.get("manifest") or {}).get("sources") or []
    if len(sources) != 1 or sources[0].get("content_sha256") != digest:
        raise RunnerBlocked("ready index does not match the registered source")
    service.set_active_index(collection, version["index_version_id"])
    return collection, version


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error_result(item: dict, error: dict) -> dict:
    return {
        "question_id": item.get("question_id"),
        "question": item.get("question"),
        "answerable": bool(item.get("answerable")),
        "run_id": None,
        "index_version_id": None,
        "answer_text": None,
        "finish_reason": None,
        "truncated": False,
        "insufficient_sources": False,
        "grounding_status": None,
        "grounding_reason": None,
        "limitation": None,
        "verification": {
            "source_exists": None,
            "quote_verbatim": None,
            "meaning_supported": None,
            "meaning_check": d24_grounding.MEANING_CHECK,
        },
        "sources": [],
        "passed_chunk_ids": [],
        "citations": [],
        "refusal": None,
        "error": error,
        "usage": None,
        "latency_ms": {"retrieval": None, "context": None, "chat": None, "total": None},
        "errors": [error] if error else ["unknown error"],
    }


def run_questions(base, collection, pinned, questions) -> list[dict]:
    results: list[dict] = []
    for item in questions:
        payload = {
            "mode": "with_rag",
            "collection_id": collection,
            "index_version_id": pinned,
            "question": item["question"],
            "grounding": True,
            "top_k": int(item.get("top_k") or 5),
            "prefilter_top_k": PREFILTER_TOP_K,
            "postfilter_top_k": POSTFILTER_TOP_K,
            "save_run": False,
        }
        try:
            status, record = request("POST", base + "/api/chat", payload)
        except Exception as exc:  # noqa: BLE001 - report a safe type only
            results.append(_error_result(item, {"code": "request_failed", "message": type(exc).__name__}))
            continue
        if status != 200 or not isinstance(record, dict):
            error = (record.get("error") if isinstance(record, dict) else None) or {
                "code": "http_error",
                "message": str(status),
            }
            results.append(_error_result(item, error))
            continue
        results.append(
            d24_grounding.project_result(
                record,
                question_id=item.get("question_id"),
                question=item.get("question"),
                answerable=bool(item.get("answerable")),
                index_version_id=pinned,
            )
        )
    return results


def run_owned(settings, source: Path, label: str) -> int:
    if report_live_blocked("D24_RUNNER_STATUS"):
        return 3
    if settings.test_profile is None or is_stub(settings.test_profile.name):
        print("D24_RUNNER_STATUS: BLOCKED (selected non-stub profile required; no fallback)")
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
            rag_grounding_enabled=True,
        )
        if urlsplit(settings.embed_base_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise RunnerBlocked("LIVE embedding requires a loopback endpoint")
        service, store = build_service(settings)
        # Grounded JSON answers are longer than free text; the selected verbose
        # remote model needs a larger context/output budget than the D22 default
        # (SPEC D24 10.1, SPEC D24 17). Bounds only affect this owned run.
        if settings.test_profile.kind == "remote":
            context_tokens = max(settings.chat_context_tokens, 65536)
            output_tokens = min(max(settings.chat_max_output_tokens, 16384), context_tokens // 2)
            settings = dataclasses.replace(
                settings, chat_context_tokens=context_tokens, chat_max_output_tokens=output_tokens
            )
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
        questions = load_questions(settings)

        backend = OwnedBackend(service, chat)
        base = backend.start()
        verify_health(base, settings.test_profile)

        results = run_questions(base, collection, pinned, questions)
        first = next((item for item in results if item.get("run_id")), None)
        model_snapshot = {
            "provider": chat_identity.get("provider"),
            "model": chat_identity.get("model"),
            "kind": chat_identity.get("kind"),
            "settings": {
                "temperature": settings.chat_temperature,
                "seed": settings.chat_seed,
                "num_predict": settings.chat_max_output_tokens,
                "num_ctx": settings.chat_context_tokens,
            },
        }
        artifact = {
            "schema_version": d24_grounding.GROUNDING_SCHEMA_VERSION,
            "created_at": _now(),
            "corpus_label": label,
            "source_sha256": digest,
            "index_version_id": pinned,
            "index": {
                "collection_id": collection,
                "index_version_id": pinned,
                "strategy": version.get("strategy"),
                "fingerprint": version.get("fingerprint"),
            },
            "model": model_snapshot,
            "embedding": {
                "model": version.get("model"),
                "dimension": version.get("dimension"),
                "digest": version.get("digest"),
            },
            "grounding": {
                "enabled": True,
                "meaning_check": d24_grounding.MEANING_CHECK,
                "whitespace_normalization": d24_grounding.WHITESPACE_NORMALIZATION,
            },
            "threshold": settings.rag_min_score,
            "results": results,
            "summary": d24_grounding.summarize(results),
        }
        (output / "grounding-results.json").write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        observations = d24_grounding.offline_observations()
        observations = d24_grounding.merge_live_observations(observations, results)
        edge = d24_grounding.build_edge_cases(
            observations, index_version_id=pinned, created_at=_now()
        )
        (output / "edge-cases.json").write_text(
            json.dumps(edge, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        answered = sum(1 for item in results if (item.get("answer_text") or "").strip())
        print(f"D24_RESULTS: {len(results)} (answered {answered})")
        failed_cases = [case["case_id"] for case in edge["cases"] if case["status"] != "PASS"]
        print(f"D24_EDGE_CASES: {len(edge['cases'])} (non-pass {len(failed_cases)})")
        if answered == len(results) and len(results) == 10 and not failed_cases:
            result = 0
            print("D24_RUNNER_STATUS: COMPLETED")
        else:
            result = 1
            print("D24_RUNNER_STATUS: FAIL (incomplete grounded results or edge cases)")
    except RunnerBlocked:
        print("D24_RUNNER_STATUS: BLOCKED (provider, corpus or index validation; no fallback)")
        result = 3
    except EvaluationFailed as exc:
        print(f"D24_RUNNER_STATUS: FAIL ({exc})")
        result = 1
    except KeyboardInterrupt:
        print("D24_RUNNER_STATUS: INTERRUPTED")
        result = 130
    except Exception as exc:  # noqa: BLE001 - never leak internals/endpoints
        print(f"D24_RUNNER_STATUS: FAIL (owned D24 evaluation failed: {type(exc).__name__})")
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
            print("D24_CLEANUP_STATUS: FAIL (owned resources retained; no shared process stopped)")
        if cleanup_ok:
            print("D24_CLEANUP_STATUS: PASS")
    print("D24_MODEL_CHECK_KIND: NETWORK")
    print("D24_QUALITY_STATUS: NOT_ASSESSED (manual answer/source assessment required)")
    return result


def main(argv=None) -> int:
    if report_live_blocked("D24_RUNNER_STATUS"):
        return 3
    arguments = sys.argv[1:] if argv is None else argv
    if arguments:
        print("D24_RUNNER_STATUS: setup error (this trusted mode accepts no arguments)")
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
        print("D24_RUNNER_STATUS: BLOCKED (selected profile, corpus or lifecycle unavailable)")
        return 3
    except KeyboardInterrupt:
        print("D24_RUNNER_STATUS: INTERRUPTED")
        return 130
    except Exception:
        print("D24_RUNNER_STATUS: setup error (configuration or result storage unavailable)")
        return 2


if __name__ == "__main__":
    sys.exit(main())
