"""Integration smoke scenario against a running backend with the local stub.

Creates two collections, builds both strategies, checks deduplication and
active-index switching, then runs a fragment search. No external network.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MODULE_DIR))

HOST = os.environ.get("KNOWLEDGE_HOST", "127.0.0.1")
PORT = os.environ.get("KNOWLEDGE_PORT", "8770")
BASE = os.environ.get("KNOWLEDGE_BASE_URL", f"http://{HOST}:{PORT}")


def request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read()
            return response.status, json.loads(body.decode("utf-8")) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, json.loads(body.decode("utf-8")) if body else {}


def wait_ready(index_version_id: str, timeout: float = 180.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        status, last = request("GET", f"/api/index-versions/{index_version_id}")
        if status == 200 and last.get("status") in ("ready", "failed"):
            return last
        time.sleep(0.4)
    raise AssertionError(f"index {index_version_id} did not finish: {last}")


def request_text(path: str) -> tuple[int, str]:
    req = urllib.request.Request(BASE + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def write_sources(root: Path) -> tuple[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    doc_a = root / "smoke_agents.md"
    doc_b = root / "smoke_notes.txt"
    section = (
        "Autonomous agents use planning, memory and tool use to act in an "
        "environment. Memory stores observations and reflections. Planning "
        "decomposes goals into steps. Tool use connects the agent to external "
        "capabilities. Retrieval augments the agent with external knowledge. "
    )
    doc_a.write_text(
        "# 1 Overview\n\n" + section * 30 + "\n\n## 2.1 Memory\n\n" + section * 10,
        encoding="utf-8",
    )
    doc_b.write_text("Small second source used to verify collection isolation. " * 40, encoding="utf-8")
    return str(doc_a), str(doc_b)


def must(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_scenario(root: Path) -> int:
    try:
        status, health = request("GET", "/api/health")
        must(status == 200, f"health failed: {status} {health}")

        doc_a, doc_b = write_sources(root)

        _, collection_a = request("POST", "/api/collections", {"name": "smoke-a"})
        _, collection_b = request("POST", "/api/collections", {"name": "smoke-b"})
        a = collection_a["collection_id"]
        b = collection_b["collection_id"]

        _, build_fixed_a = request(
            "POST",
            "/api/index/build",
            {"collection_id": a, "sources": [{"path": doc_a}], "strategy": "fixed"},
        )
        fixed_a = wait_ready(build_fixed_a["index_version_id"])
        must(fixed_a["status"] == "ready", "fixed build on A failed")

        _, build_structure_a = request(
            "POST",
            "/api/index/build",
            {"collection_id": a, "sources": [{"path": doc_a}], "strategy": "structure"},
        )
        structure_a = wait_ready(build_structure_a["index_version_id"])
        must(structure_a["status"] == "ready", "structure build on A failed")

        _, build_fixed_b = request(
            "POST",
            "/api/index/build",
            {"collection_id": b, "sources": [{"path": doc_b}], "strategy": "fixed"},
        )
        fixed_b = wait_ready(build_fixed_b["index_version_id"])
        must(fixed_b["status"] == "ready", "fixed build on B failed")

        # Deduplication: the unchanged input must be reused without new rows.
        status, rebuild = request(
            "POST",
            "/api/index/build",
            {"collection_id": a, "sources": [{"path": doc_a}], "strategy": "fixed"},
        )
        must(status == 200, f"rebuild failed: {status}")
        must(rebuild.get("reused") is True, "unchanged rebuild was not reused")
        must(
            rebuild.get("index_version_id") == fixed_a["index_version_id"],
            "reused index id changed",
        )
        must(rebuild.get("status") == "ready", "reused index is not ready")

        # Active-index switching is a parameter change only.
        status, _ = request(
            "PUT",
            f"/api/collections/{a}/active-index",
            {"index_version_id": structure_a["index_version_id"]},
        )
        must(status == 200, "active-index switch failed")

        status, search = request(
            "POST",
            "/api/search",
            {
                "collection_id": a,
                "strategy": "structure",
                "query": "how does an agent use memory and planning",
                "top_k": 3,
            },
        )
        must(status == 200, f"search failed: {status} {search}")
        must(search["fragments"], "search returned no fragments")
        must(
            search["index_version_id"] == structure_a["index_version_id"],
            "search did not use the selected strategy index",
        )

        status, search_b = request(
            "POST",
            "/api/search",
            {"collection_id": b, "query": "second source isolation", "top_k": 2},
        )
        must(status == 200, "search on B failed")
        must(
            search_b["index_version_id"] == fixed_b["index_version_id"],
            "collection B search used a foreign index",
        )

        status, chunks = request(
            "GET", f"/api/index-versions/{structure_a['index_version_id']}/chunks?limit=5"
        )
        must(status == 200 and chunks["items"], "chunks were not readable")

        status, compare = request("GET", f"/api/compare?collection_id={a}")
        must(status == 200 and len(compare["strategies"]) == 2, "compare failed")

        ui_status, ui_html = request_text("/")
        must(
            ui_status == 200 and "Knowledge Agent" in ui_html,
            "the static UI index page is not served",
        )
        asset_status, _ = request_text("/assets/app.js")
        must(asset_status == 200, "the static UI asset is not served")

        print(
            "smoke ok: fixed_chunks=%s structure_chunks=%s reused=%s fragments=%s ui=ok"
            % (
                fixed_a["counts"]["chunks"],
                structure_a["counts"]["chunks"],
                rebuild["reused"],
                len(search["fragments"]),
            )
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - report and fail the run
        print(f"SMOKE ERROR: {exc}")
        return 1


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="knowledge-smoke-") as directory:
        return run_scenario(Path(directory))


if __name__ == "__main__":
    sys.exit(main())
