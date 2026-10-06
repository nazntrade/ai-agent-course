"""D26 acceptance harness: real Gemma through the application (SPEC R7, E8).

Runs the three fixed questions plus Local->Network->Local, manual unload/reload
and a RAG control question against the real local Gemma, using the application's
own provider adapter and process manager. Artifacts are written under
``local-data/acceptance/day-26``. DeepSeek is attempted only when the app's own
network configuration is complete; otherwise the missing parameter names are
recorded (no secrets).

Each sub-check is saved immediately. A failing sub-check does not hide earlier
successes. Exit code: 0 = all required local checks passed; 1 = a real failure;
2 = preflight blocked (missing runtime/model); 3 = LIVE policy blocked.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent.parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from harness.live_policy import report_live_blocked  # noqa: E402

from app.__main__ import build_service  # noqa: E402
from app.config import apply_env_file, load_settings  # noqa: E402

QUESTIONS = json.loads((MODULE_DIR / "harness" / "d26_questions.json").read_text(encoding="utf-8"))
OUT_DIR = MODULE_DIR / "local-data" / "acceptance" / "day-26"


def emit(status: str, message: str) -> None:
    print(f"{status}: {message}")


def save(name: str, payload: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _answer_payload(record: dict, question: str) -> dict:
    answer = record["answer"]
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "question": question,
        "provider": record["provider"],
        "model": answer["model"],
        "text": answer["text"],
        "finish_reason": answer["finish_reason"],
        "usage": answer["usage"],
        "latency_ms": answer["latency_ms"],
        "parameters": answer["parameters"],
        "rag": record["rag"],
    }


def _is_json_object(text: str) -> tuple[bool, dict | str]:
    candidate = text.strip()
    try:
        parsed = json.loads(candidate)
    except ValueError as exc:
        return False, f"invalid JSON: {exc}"
    if not isinstance(parsed, dict):
        return False, "not a JSON object"
    return True, parsed


def valid_city_json(text: str) -> tuple[bool, dict | None]:
    valid, parsed = _is_json_object(text)
    if not valid:
        return False, None
    good = (
        set(parsed) == {"city", "country", "population_millions"}
        and parsed["city"] == "Paris" and parsed["country"] == "France"
        and isinstance(parsed["population_millions"], (int, float))
        and not isinstance(parsed["population_millions"], bool)
        and parsed["population_millions"] > 0
    )
    return bool(good), parsed


def run() -> int:
    if report_live_blocked("D26_LIVE_STATUS"):
        return 3

    # LIVE uses the same private module configuration as the application.
    # Offline tests never invoke this authorized branch.
    apply_env_file(MODULE_DIR / ".env")
    settings = load_settings(os.environ)
    if not Path(settings.gemma_runtime_path).is_file() or not Path(settings.gemma_gguf_path).is_file():
        emit("D26_LIVE_STATUS", "BLOCKED (local runtime or model file is missing)")
        return 2

    # Own, isolated data directories so acceptance never touches user data.
    workspace = Path(tempfile.mkdtemp(prefix="d26-acceptance-"))
    import dataclasses

    settings = dataclasses.replace(
        settings,
        dialogue_db_path=str(workspace / "conversations.db"),
    )
    service = build_service(settings)
    results: dict[str, dict] = {}
    try:
        # E2-style preflight within the application stack.
        preflight = service.local_provider.preflight()
        save("preflight-health.json", {"provider": "local", "preflight": preflight, "identity": service.local_provider.identity()})
        if not preflight.get("reachable"):
            # Start own Gemma via the manager, then re-probe.
            service.ensure_local_ready()
            preflight = service.local_provider.preflight()
            save("preflight-health.json", {"provider": "local", "preflight": preflight, "identity": service.local_provider.identity()})
        emit("D26_PREFLIGHT", "PASS" if preflight.get("reachable") else "FAIL")

        service.select_provider("local")
        dialogue = service.dialogues.create_dialogue("d26-acceptance")
        dialogue_id = dialogue["dialogue_id"]

        q1 = QUESTIONS["q1"]
        first = service.ask(dialogue_id, q1["question"], provider="local")
        payload = _answer_payload(first, q1["question"])
        payload["expected_value"] = q1["expected_value"]
        payload["value_ok"] = payload["text"].strip() == str(q1["expected_value"])
        save("gemma-q1.json", payload)
        save("preflight.json", {**payload, "preflight": preflight, "identity": service.local_provider.identity()})
        results["q1"] = payload["value_ok"]

        q2 = QUESTIONS["q2"]
        second = service.ask(dialogue_id, q2["question"], provider="local")
        payload = _answer_payload(second, q2["question"])
        valid, parsed = valid_city_json(payload["text"])
        payload["json_valid"] = _is_json_object(payload["text"])[0]
        payload["parsed"] = parsed
        payload["required_keys_present"] = valid
        save("gemma-q2.json", payload)
        results["q2"] = payload["json_valid"] and payload["required_keys_present"]

        q3 = QUESTIONS["q3"]
        third = service.ask(dialogue_id, q3["question"], provider="local")
        payload = _answer_payload(third, q3["question"])
        payload["expected_value"] = q3["expected_value"]
        payload["meaning_check"] = "not_assessed"  # Tester must inspect the actual reasoning.
        payload["value_ok"] = str(q3["expected_value"]) in payload["text"]
        save("gemma-q3.json", payload)
        results["q3"] = payload["value_ok"]

        # Local -> Network -> Local lifecycle.
        before = service.gemma.state_snapshot()
        network_attempt = {"selected": "network", "answered": False, "missing_config": [], "note": None}
        service.select_provider("network", dialogue_id=dialogue_id)
        after_network = service.gemma.state_snapshot()
        missing = service.network_provider.missing_config()
        network_attempt["missing_config"] = missing
        if not missing:
            try:
                for number in (1, 2, 3):
                    question = QUESTIONS[f"q{number}"]
                    record = service.ask(dialogue_id, question["question"], provider="network")
                    payload = _answer_payload(record, question["question"])
                    if number == 2:
                        valid, parsed = _is_json_object(payload["text"])
                        fields_ok, parsed = valid_city_json(payload["text"])
                        payload.update(json_valid=valid, required_keys_present=bool(fields_ok))
                        content_ok = bool(fields_ok)
                    else:
                        payload["expected_value"] = question["expected_value"]
                        content_ok = payload["text"].strip() == str(question["expected_value"]) if number == 1 else str(question["expected_value"]) in payload["text"]
                        payload["value_ok"] = content_ok
                    results[f"deepseek_q{number}"] = bool(content_ok and payload["text"].strip() and payload["finish_reason"] == "stop")
                    save(f"deepseek-q{number}.json", payload)
                network_attempt["answered"] = all(results[f"deepseek_q{number}"] for number in (1, 2, 3))
            except Exception as exc:  # noqa: BLE001
                results["deepseek"] = False
                network_attempt["note"] = type(exc).__name__
                save("deepseek-q1.json", {"error": type(exc).__name__, "missing_config": missing})
        else:
            save("deepseek-q1.json", {"missing_params": missing})
            network_attempt["note"] = "network provider not configured (parameters named, no secrets)"
        service.select_provider("local", dialogue_id=dialogue_id)
        service.ensure_local_ready()
        after_back_local = service.gemma.state_snapshot()
        save("lifecycle.json", {
            "local_before": before,
            "network": network_attempt,
            "local_after_network_state": after_network,
            "local_after_back_state": after_back_local,
        })
        results["lifecycle"] = (
            after_network["state"] == "unloaded"
            and after_back_local["state"] == "ready"
        )

        # Manual unload + reload.
        service.unload_local()
        unloaded = service.gemma.state_snapshot()
        service.ensure_local_ready()
        reloaded = service.gemma.state_snapshot()
        reload_check = service.ask(dialogue_id, q1["question"], provider="local")
        reload_ok = str(q1["expected_value"]) in reload_check["answer"]["text"]
        save("lifecycle.json", {
            "local_before": before,
            "network": network_attempt,
            "local_after_network_state": after_network,
            "local_after_back_state": after_back_local,
            "unloaded_state": unloaded,
            "reloaded_state": reloaded,
            "reload_answer_model": reload_check["answer"]["model"],
            "reload_value_ok": reload_ok,
        })
        results["unload_reload"] = unloaded["state"] == "unloaded" and reloaded["state"] == "ready" and reload_ok

        # RAG control: index a small document; RAG answer must cite it; no-RAG must not.
        control = QUESTIONS["rag_control"]
        indexed = service.add_document(control["document_label"], control["document_text"])
        rag_record = service.ask(
            dialogue_id, control["question"], rag_enabled=True, provider="local", use_rewrite=True, min_score=0.3
        )
        no_rag_record = service.ask(dialogue_id, control["question"], rag_enabled=False, provider="local")
        rag_payload = _answer_payload(rag_record, control["question"])
        rag_payload["expected_substring"] = control["expected_substring"]
        rag_payload["documented"] = control["expected_substring"] in rag_payload["text"]
        rag_payload["indexed"] = indexed
        rag_payload["citations"] = rag_record["rag"]["citations"]
        rag_payload["rewrite"] = rag_record["rag"]["rewrite"]
        # Retrieval filter + comparison regression on the real index.
        comparison = service.compare(control["question"], top_k=3, min_score=0.3)
        rag_payload["compare"] = {
            "unfiltered_count": len(comparison["unfiltered"]),
            "filtered_count": len(comparison["filtered"]),
            "stable": comparison["stable"],
        }
        no_rag_payload = _answer_payload(no_rag_record, control["question"])
        no_rag_payload["source_count"] = len(no_rag_payload["rag"]["sources"])
        save("rag-control.json", {"rag": rag_payload, "no_rag": no_rag_payload, "compare": comparison})
        citation_ok = rag_record["rag"]["citations"]["inline_unsupported"] == []
        results["rag"] = (
            rag_payload["documented"]
            and no_rag_payload["source_count"] == 0
            and citation_ok
            and len(comparison["filtered"]) >= 1
        )

        emit("D26_RESULTS", json.dumps(results, ensure_ascii=False))
        passed = all(results.values())
        emit("D26_LIVE_STATUS", "PASS" if passed else "FAIL")
        return 0 if passed else 1
    except Exception as exc:  # noqa: BLE001 - report the concrete failed real check
        # Key/type messages carry only a key or type name, never secrets.
        detail = str(exc) if isinstance(exc, (KeyError, TypeError, ValueError)) else ""
        emit("D26_LIVE_STATUS", f"FAIL ({type(exc).__name__}: {detail})" if detail else f"FAIL ({type(exc).__name__})")
        return 1
    finally:
        service.close()


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
