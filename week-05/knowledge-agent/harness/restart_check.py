"""INT check: the index survives a real process restart (D21-05).

Builds an index in one process, stops it, starts the backend fresh on the same
database and verifies that collections/chunks/search still work. Uses only the
local stub (no external network).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
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
from knowledge_agent.config import Settings  # noqa: E402


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


def request(method: str, url: str, payload: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        body = response.read()
        return response.status, (json.loads(body.decode("utf-8")) if body else {})


def run_check(root: Path) -> int:
    stub_port = free_port()
    api_port = free_port()
    server = embed_stub.create_server("127.0.0.1", stub_port, dimension=64)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    store = None
    try:
        db_path = root / "index.db"
        settings = Settings(
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

        source = root / "source.md"
        source.write_text("# Memory\n\n" + "Agent memory stores observations. " * 50, encoding="utf-8")
        service, store = build_service(settings)
        collection = service.create_collection("restart-check")["collection_id"]
        result = service.build(collection, [{"path": str(source)}], "fixed", wait=True)
        version = service.get_index_version(result["index_version_id"])
        if version["status"] != "ready":
            raise AssertionError("build did not reach ready")
        store.close()  # simulate a full process shutdown

        env = dict(os.environ)
        env.update(
            {
                "KNOWLEDGE_SKIP_ENV_FILE": "1",
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
        base = f"http://127.0.0.1:{api_port}"
        try:
            if not wait_health(base, process):
                print("RESTART_STATUS: FAIL (backend did not start after restart)")
                return 1
            status, collections = request("GET", base + "/api/collections")
            match = [
                item
                for item in collections["collections"]
                if item["collection_id"] == collection
            ]
            if not match or match[0]["active_index_version_id"] != version["index_version_id"]:
                print("RESTART_STATUS: FAIL (active index was not preserved)")
                return 1
            status, chunks = request(
                "GET", base + f"/api/index-versions/{version['index_version_id']}/chunks?limit=5"
            )
            if status != 200 or not chunks["items"]:
                print("RESTART_STATUS: FAIL (chunks were not readable after restart)")
                return 1
            status, search = request(
                "POST",
                base + "/api/search",
                {"collection_id": collection, "query": "agent memory", "top_k": 3},
            )
            if status != 200 or not search["fragments"]:
                print("RESTART_STATUS: FAIL (search failed after restart)")
                return 1
            print(
                "RESTART_STATUS: PASS (index, active pointer and search survived restart;"
                f" chunks={len(chunks['items'])})"
            )
            return 0
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    except Exception as exc:  # noqa: BLE001 - report and fail
        print(f"RESTART_STATUS: FAIL ({exc})")
        return 1
    finally:
        if store is not None:
            store.close()
        server.shutdown()
        server.server_close()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="knowledge-restart-") as directory:
        return run_check(Path(directory))


if __name__ == "__main__":
    sys.exit(main())
