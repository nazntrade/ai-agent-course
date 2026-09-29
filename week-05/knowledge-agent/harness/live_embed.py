"""Opt-in LIVE embedding check against local Ollama (MODEL_CHECK_KIND: LOCAL).

Runs a real ``embeddinggemma:300m`` embedding over the user-provided PDF and a
small second source, persists the index and searches it. Never downloads models
and never calls paid APIs. Exit codes: 0 = PASS, 1 = FAIL, 3 = BLOCKED.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MODULE_DIR))

from knowledge_agent.__main__ import build_service  # noqa: E402
from knowledge_agent.config import load_settings  # noqa: E402

PDF_PATH = MODULE_DIR / "local-data" / "input" / "agents-survey.pdf"
SEARCH_QUERY = "planning in LLM agents"


def emit(status: str, message: str) -> None:
    print(f"{status}: {message}")


def _find_or_create_collection(service, name: str) -> str:
    """Reuse the newest collection with ``name`` so its older versions remain."""

    matches = [
        collection["collection_id"]
        for collection in service.list_collections()
        if collection.get("name") == name
    ]
    if matches:
        return matches[-1]
    return service.create_collection(name)["collection_id"]


def _print_versions(service, collection_id: str, label: str) -> None:
    versions = service.list_index_versions(collection_id)
    print(f"  versions[{label}]: total={len(versions)}")
    for version in versions:
        pipeline = (version.get("manifest") or {}).get("pipeline") or {}
        print(
            f"    id={version['index_version_id']} strategy={version['strategy']}"
            f" status={version['status']}"
            f" extraction_versions={pipeline.get('extraction_versions')}"
            f" created_at={version.get('created_at')}"
        )


def _snippet(text: str, limit: int = 180) -> str:
    return " ".join(text.split())[:limit]


def main() -> int:
    settings = load_settings()
    live_db = MODULE_DIR / "local-data" / "live" / "index.db"
    settings = dataclasses.replace(settings, db_path=str(live_db))
    service, _store = build_service(settings)

    print("MODEL_CHECK_KIND: LOCAL")
    preflight = service._embedder.preflight()  # noqa: SLF001 - explicit diagnostic
    if not preflight.get("reachable"):
        emit("LOCAL_MODEL_START", "FAIL (Ollama is not reachable)")
        emit("EMBEDDING_LIVE_STATUS", "BLOCKED")
        print("Start the local Ollama service, then retry.")
        return 3
    emit("LOCAL_MODEL_START", f"PASS (Ollama version {preflight.get('version')})")

    if not preflight.get("model_present"):
        emit("LOCAL_MODEL_INFERENCE", "BLOCKED (embedding model is missing)")
        emit("EMBEDDING_LIVE_STATUS", "BLOCKED")
        print("Run: ollama pull embeddinggemma:300m")
        return 3

    if not PDF_PATH.is_file():
        emit("LOCAL_MODEL_INFERENCE", "BLOCKED (the input PDF was not found)")
        emit("EMBEDDING_LIVE_STATUS", "BLOCKED")
        print(f"Expected the corpus at {PDF_PATH.name} under local-data/input/.")
        return 3

    try:
        dimension = preflight.get("dimension")
        emit(
            "LOCAL_MODEL_INFERENCE",
            f"PASS (model={service._embedder.model}, digest={preflight.get('digest')},"
            f" dimension={dimension})",
        )

        pdf_collection = _find_or_create_collection(service, "live-pdf")
        _print_versions(service, pdf_collection, "before")

        versions: dict[str, dict] = {}
        for strategy in ("fixed", "structure"):
            result = service.build(
                pdf_collection, [{"path": str(PDF_PATH)}], strategy, wait=True
            )
            versions[strategy] = service.get_index_version(result["index_version_id"])
            metrics = versions[strategy].get("metrics") or {}
            tokens = metrics.get("chunk_tokens") or {}
            print(
                f"  {strategy}: index_version_id={result['index_version_id']}"
                f" reused={result['reused']}"
                f" chunks={versions[strategy]['counts']['chunks']}"
                f" build_seconds={metrics.get('build_seconds')}"
                f" input_tokens={metrics.get('input_tokens')}"
                f" embed_latency_median_ms={metrics.get('embed_latency_median_ms')}"
                f" chunks_per_second={metrics.get('chunks_per_second')}"
                f" tokens(min/median/p95/max)="
                f"{tokens.get('min')}/{tokens.get('median')}/{tokens.get('p95')}/{tokens.get('max')}"
                f" overlap_overhead={metrics.get('overlap_overhead')}"
                f" section_crossing_ratio={metrics.get('section_crossing_ratio')}"
                f" vector_bytes={metrics.get('vector_bytes')}"
                f" db_size_bytes={metrics.get('db_size_bytes')}"
                f" excluded_roles={versions[strategy]['manifest'].get('excluded_roles')}"
                f" finished_at={versions[strategy].get('finished_at') is not None}"
            )

        _print_versions(service, pdf_collection, "after")

        for strategy in ("fixed", "structure"):
            # Search the freshly built version explicitly: an activation pointer
            # left by an older run must not silently select a stale version.
            search = service.search(
                pdf_collection,
                SEARCH_QUERY,
                top_k=3,
                index_version_id=versions[strategy]["index_version_id"],
            )
            print(
                f"  search[{strategy}] query={search['query']!r}"
                f" index_version_id={search['index_version_id']}"
                f" fragments={len(search['fragments'])}"
            )
            for fragment in search["fragments"]:
                metadata = fragment["metadata"]
                print(
                    f"    rank={fragment['rank']} score={fragment['score']}"
                    f" section_path={metadata.get('section_path')!r}"
                    f" page_start={metadata.get('page_start')}"
                    f" :: {_snippet(fragment['text'])}"
                )

        for strategy in ("fixed", "structure"):
            repeat = service.build(
                pdf_collection, [{"path": str(PDF_PATH)}], strategy, wait=True
            )
            print(
                f"  repeat build {strategy}: reused={repeat['reused']}"
                f" index_version_id={repeat['index_version_id']}"
                f" same_id={repeat['index_version_id'] == versions[strategy]['index_version_id']}"
            )

        service.set_active_index(pdf_collection, versions["structure"]["index_version_id"])
        replayed = service.search(
            pdf_collection, "memory and planning in autonomous agents", top_k=3
        )
        if not replayed["fragments"]:
            raise AssertionError("real search returned no fragments")
        print(
            f"  search: top_score={replayed['fragments'][0]['score']} "
            f"fragments={len(replayed['fragments'])}"
        )

        notes_dir = MODULE_DIR / "local-data" / "live"
        notes_dir.mkdir(parents=True, exist_ok=True)
        notes = notes_dir / "notes.md"
        notes.write_text(
            "# Notes\n\nLocal embedding notes about retrieval and chunking. " * 20,
            encoding="utf-8",
        )
        notes_collection = _find_or_create_collection(service, "live-notes")
        notes_build = service.build(
            notes_collection, [{"path": str(notes)}], "fixed", wait=True
        )
        notes_version = service.get_index_version(notes_build["index_version_id"])
        notes_search = service.search(
            notes_collection,
            "chunking notes",
            top_k=2,
            index_version_id=notes_version["index_version_id"],
        )
        if not notes_search["fragments"]:
            raise AssertionError("second collection search returned no fragments")
        emit(
            "LOCAL_SCENARIO_TEST",
            "PASS (build+search on PDF and a small second collection)",
        )
        emit(
            "LIVE_METRICS",
            "pdf_fixed_chunks=%s pdf_structure_chunks=%s notes_chunks=%s"
            % (
                versions["fixed"]["counts"]["chunks"],
                versions["structure"]["counts"]["chunks"],
                notes_version["counts"]["chunks"],
            ),
        )
        emit("EMBEDDING_LIVE_STATUS", "PASS")
        return 0
    except Exception as exc:  # noqa: BLE001 - report and fail
        emit("LOCAL_SCENARIO_TEST", f"FAIL ({exc})")
        emit("EMBEDDING_LIVE_STATUS", "FAIL")
        return 1
    finally:
        try:
            service._store.close()  # noqa: SLF001 - close the live store
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
