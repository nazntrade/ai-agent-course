"""Opt-in LIVE check on a fresh owned index, never an existing working database."""

from __future__ import annotations

import dataclasses
import json
import math
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse

MODULE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MODULE_DIR))

from knowledge_agent.__main__ import build_service  # noqa: E402
from knowledge_agent.config import Settings, load_settings  # noqa: E402
from knowledge_agent.domain.models import ChunkMetadata  # noqa: E402
from knowledge_agent.sources import pdf_source  # noqa: E402
from knowledge_agent.sources.pdf_layout import order_lines, strip_headers_footers  # noqa: E402

PDF_PATH = MODULE_DIR / "local-data" / "input" / "agents-survey.pdf"
SEARCH_QUERY = "planning in LLM agents"


def emit(status: str, message: str) -> None:
    print(f"{status}: {message}")


def indexable_pdf_pages(service, source: Path, threshold: int) -> int:
    """Count useful cleaned paragraph text after the same section-role exclusions."""

    prepared = service._prepare_sources([{"path": str(source)}])[0]  # noqa: SLF001
    adapter = prepared["adapter"]
    reference = prepared["source_ref"]
    lines, heights, widths = adapter._extract_layout(source.read_bytes(), reference)  # noqa: SLF001
    cleaned, _ = strip_headers_footers(lines, heights)
    ordered = [order_lines(page, width) for page, width in zip(cleaned, widths)]
    sizes = [line.size for page in ordered for line in page if line.size > 0]
    body_size = pdf_source._dominant_size(sizes)  # noqa: SLF001
    page_chars = Counter()
    role = "body"
    for page in ordered:
        for heading, paragraph in pdf_source._page_units(page, body_size):  # noqa: SLF001
            if heading:
                role = pdf_source._role_for(paragraph.text)  # noqa: SLF001
            elif role not in service.excluded_roles:
                page_chars[paragraph.page] += len(paragraph.text)
    count = sum(chars >= threshold for chars in page_chars.values())
    print(f"LIVE_CORPUS: total_pages={len(lines)} indexed_useful_pages={count} minimum_chars={threshold}")
    return count


def validate_ready_index(service, store, version_id: str) -> dict:
    version = service.get_index_version(version_id)
    assert version["status"] == "ready", version.get("error")
    chunks = store.iter_chunk_vectors(version_id)
    assert chunks and len(chunks) == version["counts"]["chunks"]
    dimension = version["dimension"]
    assert isinstance(dimension, int) and dimension > 0
    required = set(ChunkMetadata.model_fields)
    for chunk in chunks:
        assert required <= set(chunk["metadata"])
        vector = chunk["vector"]
        assert len(vector) == dimension and all(math.isfinite(value) for value in vector)
        assert "/" not in chunk["metadata"]["source_uri"] and "\\" not in chunk["metadata"]["source_uri"]
    print(f"LIVE_VECTOR_CHECK: PASS chunks={len(chunks)} dimension={dimension} metadata=complete finite=true")
    return version


def row_counts(store) -> dict:
    with closing(sqlite3.connect(store.db_path)) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("sources", "documents", "sections", "chunks", "index_versions")
        }


def run_scenario(settings: Settings, root: Path, source: Path) -> int:
    service = store = None
    inference_observed = False
    print("MODEL_CHECK_KIND: LOCAL")
    try:
        assert urlparse(settings.embed_base_url).hostname in {"127.0.0.1", "localhost", "::1"}, "LIVE requires a loopback Ollama endpoint"
        settings = dataclasses.replace(settings, db_path=str(root / "index.db"))
        service, store = build_service(settings)
        preflight = service._embedder.preflight()  # noqa: SLF001
        if not preflight.get("reachable") or not preflight.get("model_present"):
            emit("LOCAL_MODEL_START", "FAIL (Ollama or the configured embedding model is unavailable)")
            emit("LOCAL_MODEL_INFERENCE", "NOT_RUN")
            emit("EMBEDDING_LIVE_STATUS", "BLOCKED")
            return 3
        emit("LOCAL_MODEL_START", f"PASS (model={settings.embed_model}, Ollama version {preflight.get('version')})")
        if not source.is_file():
            emit("LOCAL_MODEL_INFERENCE", "NOT_RUN (the explicitly configured PDF is missing)")
            emit("EMBEDDING_LIVE_STATUS", "BLOCKED")
            return 3
        assert indexable_pdf_pages(service, source, settings.pdf_useful_page_min_chars) >= 20, "fewer than 20 useful indexed PDF pages after exclusions"

        collection = service.create_collection("live-pdf")["collection_id"]
        versions = {}
        for strategy in ("fixed", "structure"):
            result = service.build(collection, [{"path": str(source)}], strategy, wait=True)
            assert result["reused"] is False, "fresh LIVE must execute document embeddings"
            version = validate_ready_index(service, store, result["index_version_id"])
            versions[strategy] = version
            if not inference_observed:
                inference_observed = True
                emit("LOCAL_MODEL_INFERENCE", f"PASS (fresh document embeddings persisted; model={version['model']}, digest={version['digest']}, dimension={version['dimension']})")
            print(f"LIVE_METRICS[{strategy}]: {json.dumps(version['metrics'], sort_keys=True)}")
            manifest = version["manifest"]
            print(f"LIVE_INDEX[{strategy}]: chunks={version['counts']['chunks']} extraction_versions={manifest['pipeline']['extraction_versions']} excluded_roles={manifest['excluded_roles']}")
            chunks = store.iter_chunk_vectors(version["index_version_id"])
            preview = next((chunk for chunk in chunks if "introduction" in chunk["metadata"]["section_path"].lower()), chunks[0])
            print(f"LIVE_TEXT[{strategy}]: section={preview['metadata']['section_path']!r} page={preview['metadata']['page_start']} :: {' '.join(preview['text'].split())[:700]}")

            search = service.search(collection, SEARCH_QUERY, top_k=3, index_version_id=version["index_version_id"])
            assert search["fragments"] and search["index_version_id"] == version["index_version_id"]
            for fragment in search["fragments"]:
                assert math.isfinite(fragment["score"])
                print(f"LIVE_SEARCH[{strategy}]: rank={fragment['rank']} score={fragment['score']} section={fragment['metadata']['section_path']!r} page={fragment['metadata']['page_start']} :: {' '.join(fragment['text'].split())[:220]}")

            before = row_counts(store)
            repeat = service.build(collection, [{"path": str(source)}], strategy, wait=True)
            assert repeat["reused"] is True and repeat["index_version_id"] == version["index_version_id"]
            assert row_counts(store) == before, "repeat import inserted new rows"
            print(f"LIVE_REPEAT[{strategy}]: PASS reused=true same_id=true new_rows=0")

        comparison = service.compare(collection, ["fixed", "structure"])
        assert comparison["comparable"] is True
        assert versions["fixed"]["manifest"]["cleaned_corpus_sha256"] == versions["structure"]["manifest"]["cleaned_corpus_sha256"]

        service.set_active_index(collection, versions["structure"]["index_version_id"])
        active_search = service.search(collection, "memory and planning in autonomous agents", top_k=3)
        assert active_search["index_version_id"] == versions["structure"]["index_version_id"]
        assert active_search["fragments"]

        notes = root / "notes.md"
        notes.write_text("# Notes\n\nLocal embedding notes about retrieval and chunking. " * 20, encoding="utf-8")
        other = service.create_collection("live-notes")["collection_id"]
        result = service.build(other, [{"path": str(notes)}], "fixed", wait=True)
        notes_version = validate_ready_index(service, store, result["index_version_id"])
        notes_search = service.search(other, "chunking notes", top_k=2)
        assert notes_search["fragments"] and notes_search["index_version_id"] == notes_version["index_version_id"]
        print("LIVE_OUTPUT_TOKENS: n/a (embedding, no text generation)")
        return 0
    except Exception as exc:  # noqa: BLE001 - report the failed real-provider scenario
        if not inference_observed:
            emit("LOCAL_MODEL_INFERENCE", "FAIL (no successful fresh index embedding was verified)")
        emit("LOCAL_SCENARIO_TEST", f"FAIL ({exc})")
        emit("EMBEDDING_LIVE_STATUS", "FAIL")
        return 1
    finally:
        if store is not None:
            store.close()


def main() -> int:
    if os.environ.get("RUN_EMBED_LIVE") != "1":
        emit("EMBEDDING_LIVE_STATUS", "BLOCKED (explicit RUN_EMBED_LIVE=1 opt-in required)")
        return 3
    settings = load_settings()
    source = Path(settings.source_path) if settings.source_path else PDF_PATH
    try:
        with tempfile.TemporaryDirectory(prefix="knowledge-live-") as directory:
            result = run_scenario(settings, Path(directory), source)
    except OSError as exc:
        emit("LIVE_CLEANUP", f"FAIL ({type(exc).__name__})")
        emit("LOCAL_SCENARIO_TEST", "FAIL (owned temporary data could not be cleaned up)")
        emit("EMBEDDING_LIVE_STATUS", "FAIL")
        return 1
    emit("LIVE_CLEANUP", "PASS")
    if result == 0:
        emit("LOCAL_SCENARIO_TEST", "PASS (fresh PDF and notes indexes, finite vectors, complete metadata, search, switching, zero-row dedup and owned cleanup)")
        emit("EMBEDDING_LIVE_STATUS", "PASS")
    return result


if __name__ == "__main__":
    sys.exit(main())
