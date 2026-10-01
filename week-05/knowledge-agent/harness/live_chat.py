"""Opt-in LIVE chat check against a real local Ollama chat model.

Builds a fresh owned index with real embeddings, then calls the configured
``CHAT_MODEL`` in both modes. Never downloads a model; if the model is missing
the run is reported BLOCKED rather than faked.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

MODULE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MODULE_DIR))

from knowledge_agent.__main__ import build_chat_service, build_service  # noqa: E402
from knowledge_agent.config import Settings, load_settings  # noqa: E402

QUESTION = "How do autonomous agents use memory and planning?"


def emit(status: str, message: str) -> None:
    print(f"{status}: {message}")


def _source_for(settings: Settings, root: Path) -> Path:
    if settings.source_path and Path(settings.source_path).is_file():
        return Path(settings.source_path)
    source = root / "live-chat-source.md"
    source.write_text(
        "# Memory and planning\n\n"
        + "Autonomous agents use memory to store observations and planning to "
        "decompose goals into steps. Tool use connects them to the environment. " * 30,
        encoding="utf-8",
    )
    return source


def _chat(chat_service, collection_id: str, mode: str) -> dict:
    request = {"mode": mode, "question": QUESTION, "top_k": 3}
    if mode == "with_rag":
        request.update({"collection_id": collection_id, "strategy": "structure"})
    record = chat_service.chat(request)
    answer = record["answer"]
    print(
        f"LIVE_CHAT[{mode}]: run_id={record['run_id']} finish_reason={answer['finish_reason']} "
        f"truncated={answer['truncated']} latency_ms={json.dumps(record['latency_ms'])} "
        f"usage={json.dumps(record['usage'])} tok_s={record['output_tokens_per_second']}"
    )
    if mode == "with_rag":
        retrieval = record["retrieval"]
        print(
            f"LIVE_SOURCES[{mode}]: found={retrieval['found_count']} passed={retrieval['passed_count']}"
        )
        if retrieval["passed_count"] > retrieval["found_count"]:
            raise AssertionError("passed is not a subset of found")
        for item in retrieval["passed"]:
            metadata = item.get("metadata") or {}
            print(
                f"  passed rank={item['rank']} chunk_id={str(item['chunk_id'])[:12]}… "
                f"section={metadata.get('section_path')!r} page={metadata.get('page_start')}"
            )
    if not str(answer["text"]).strip():
        raise AssertionError(f"{mode} returned an empty answer")
    return record


def run_scenario(settings: Settings, root: Path) -> int:
    store = None
    check_kind = "NETWORK" if settings.test_profile and settings.test_profile.kind == "remote" else "LOCAL"
    prefix = "NETWORK_MODEL" if check_kind == "NETWORK" else "LOCAL_MODEL"
    print(f"MODEL_CHECK_KIND: {check_kind}")
    try:
        assert settings.test_profile is not None or urlparse(settings.chat_base_url).hostname in {"127.0.0.1", "localhost", "::1"}, (
            "LIVE requires a loopback Ollama chat endpoint"
        )
        settings = dataclasses.replace(
            settings,
            db_path=str(root / "index.db"),
            chat_runs_path=str(root / "chat-runs"),
        )
        service, store = build_service(settings)
        chat_service = build_chat_service(settings, service)
        preflight = chat_service.chat_model.preflight()
        if not preflight.get("reachable"):
            emit(prefix + "_START", "FAIL (chat endpoint unreachable)")
            emit(prefix + "_INFERENCE", "NOT_RUN")
            emit("CHAT_LIVE_STATUS", "BLOCKED")
            return 3
        actual_model = settings.test_profile.name if settings.test_profile else settings.chat_model
        if not actual_model or not preflight.get("model_present"):
            emit(
                prefix + "_START",
                f"FAIL (selected chat model is unavailable)",
            )
            emit(prefix + "_INFERENCE", "NOT_RUN")
            emit("CHAT_LIVE_STATUS", "BLOCKED")
            return 3
        emit(
            prefix + "_START",
            f"PASS (model={actual_model}, provider={chat_service.chat_model.identity().provider}, "
            f"digest={preflight.get('digest')})",
        )

        source = _source_for(settings, root)
        collection = service.create_collection("live-chat")["collection_id"]
        build = service.build(collection, [{"path": str(source)}], "structure", wait=True)
        version = service.get_index_version(build["index_version_id"])
        if version["status"] != "ready":
            raise AssertionError(version.get("error") or "index is not ready")
        print(
            f"LIVE_INDEX: index_version_id={version['index_version_id']} "
            f"chunks={version['counts']['chunks']} model={version.get('model')}"
        )

        print(f"MODEL_PROVENANCE: chat={actual_model} embedding={settings.embed_model}")
        _chat(chat_service, collection, "without_rag")
        _chat(chat_service, collection, "with_rag")
        # Inference is only claimed after a real chat generation actually happened.
        emit(prefix + "_INFERENCE", "PASS (real chat generation in both modes)")
        emit("NETWORK_SCENARIO_TEST" if check_kind == "NETWORK" else "LOCAL_SCENARIO_TEST", "PASS (both modes on the real local chat model)")
        emit("CHAT_LIVE_STATUS", "PASS")
        return 0
    except Exception as exc:  # noqa: BLE001 - report the failed real-provider scenario
        emit("NETWORK_SCENARIO_TEST" if check_kind == "NETWORK" else "LOCAL_SCENARIO_TEST", f"FAIL ({exc})")
        emit("CHAT_LIVE_STATUS", "FAIL")
        return 1
    finally:
        if store is not None:
            store.close()


def main() -> int:
    if os.environ.get("RUN_CHAT_LIVE") != "1":
        emit("CHAT_LIVE_STATUS", "BLOCKED (explicit RUN_CHAT_LIVE=1 opt-in required)")
        return 3
    settings = load_settings()
    try:
        with tempfile.TemporaryDirectory(prefix="knowledge-live-chat-") as directory:
            result = run_scenario(settings, Path(directory))
    except OSError:
        emit("LIVE_CLEANUP", "FAIL")
        emit("CHAT_LIVE_STATUS", "FAIL")
        return 1
    emit("LIVE_CLEANUP", "PASS")
    return result


if __name__ == "__main__":
    sys.exit(main())
