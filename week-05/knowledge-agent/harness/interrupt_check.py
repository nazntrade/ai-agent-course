"""INT check: an interrupted build never becomes ready/active (D21-10).

A ``building`` index version (with partial chunks) is present in the database;
starting the backend must fail it as ``interrupted`` without touching the old
active ready index. Uses only the local stub.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MODULE_DIR))
sys.path.insert(0, str(MODULE_DIR / "harness"))

import embed_stub  # noqa: E402

from knowledge_agent.__main__ import build_service  # noqa: E402
from knowledge_agent.chunking.fixed import FixedChunker  # noqa: E402
from knowledge_agent.config import Settings  # noqa: E402
from knowledge_agent.domain.models import Document, ExtractionInfo, Section, SourceRef  # noqa: E402
from knowledge_agent.storage.sqlite_store import SqliteIndexStore  # noqa: E402
from knowledge_agent.text.tokenizer import LexicalTokenizer  # noqa: E402


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_health(base: str, process: subprocess.Popen, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def build_settings(db_path: Path, stub_port: int, api_port: int) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=api_port,
        db_path=str(db_path),
        source_path="",
        embed_base_url=f"http://127.0.0.1:{stub_port}",
        embed_model="embeddinggemma:300m",
        embed_batch_size=16,
        embed_timeout_seconds=30,
        document_prefix="",
        query_prefix="",
        chunk_size=500,
        chunk_overlap=75,
        structure_max_tokens=800,
        structure_min_tokens=64,
        structure_max_chars=3200,
        pdf_useful_page_min_chars=500,
        ui_title="Knowledge Agent",
    )


def stale_document() -> Document:
    payload = b"stale"
    digest = hashlib.sha256(payload).hexdigest()
    source_ref = SourceRef(
        source_id=digest,
        uri="stale.md",
        public_uri="stale.md",
        label="stale.md",
        kind="text",
        content_sha256=digest,
        size_bytes=len(payload),
    )
    return Document(
        document_id="stale-document",
        source=source_ref,
        extraction=ExtractionInfo(
            extraction_version="text-v1", adapter="text", useful_chars=20
        ),
        sections=[
            Section(
                section_id="stale-section",
                section_path="Stale",
                level=1,
                text="Stale partial chunk that must be deleted on failure.",
            )
        ],
    )


def main() -> int:
    stub_port = free_port()
    api_port = free_port()
    server = embed_stub.create_server("127.0.0.1", stub_port, dimension=64)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    db_path = MODULE_DIR / "local-data" / "interrupt" / "index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    settings = build_settings(db_path, stub_port, api_port)

    source = MODULE_DIR / "local-data" / "interrupt" / "source.md"
    source.write_text("# Ready\n\n" + "Stable content for the active index. " * 40, encoding="utf-8")

    try:
        service, store = build_service(settings)
        collection = service.create_collection("interrupt-check")["collection_id"]
        ready = service.build(collection, [{"path": str(source)}], "fixed", wait=True)
        ready_version = service.get_index_version(ready["index_version_id"])
        if ready_version["status"] != "ready":
            raise AssertionError("baseline index is not ready")
        service.set_active_index(collection, ready_version["index_version_id"])

        identity = service._embedder.identity().to_dict()  # noqa: SLF001
        identity["corpus_schema_version"] = service.corpus_schema_version
        stale_id = store.create_index_version(
            collection, "fixed", "stale-fingerprint", identity
        )
        chunks = FixedChunker(LexicalTokenizer()).chunk(stale_document())
        store.insert_chunks(stale_id, chunks)
        if store.count_rows(stale_id)["chunks"] == 0:
            raise AssertionError("stale partial chunks were not inserted")
        store.close()

        env = dict(os.environ)
        env.update(
            {
                "KNOWLEDGE_HOST": "127.0.0.1",
                "KNOWLEDGE_PORT": str(api_port),
                "KNOWLEDGE_DB_PATH": str(db_path),
                "EMBED_BASE_URL": f"http://127.0.0.1:{stub_port}",
            }
        )
        process = subprocess.Popen(
            [sys.executable, "-m", "knowledge_agent"],
            cwd=str(MODULE_DIR),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            if not wait_health(f"http://127.0.0.1:{api_port}", process):
                print("INTERRUPT_STATUS: FAIL (backend did not start)")
                return 1
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

        check_store = SqliteIndexStore(db_path)
        stale = check_store.get_index_version(stale_id)
        collection_state = check_store.get_collection(collection)
        partial = check_store.count_rows(stale_id)["chunks"]
        check_store.close()

        if stale["status"] != "failed":
            print(f"INTERRUPT_STATUS: FAIL (stale status is {stale['status']})")
            return 1
        if stale["error"] != "interrupted":
            print(f"INTERRUPT_STATUS: FAIL (stale error is {stale['error']})")
            return 1
        if partial != 0:
            print("INTERRUPT_STATUS: FAIL (partial chunks were not deleted)")
            return 1
        if collection_state["active_index_version_id"] != ready_version["index_version_id"]:
            print("INTERRUPT_STATUS: FAIL (active index changed)")
            return 1
        print(
            "INTERRUPT_STATUS: PASS (interrupted build failed, partial chunks removed,"
            " old active index untouched)"
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - report and fail
        print(f"INTERRUPT_STATUS: FAIL ({exc})")
        return 1
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
