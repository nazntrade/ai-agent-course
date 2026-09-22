"""Artifact directory of one harness run.

Every run writes into ``.runs/<timestamp>-<label>/`` (git-ignored):
``trace.jsonl``, ``mcp_server.log``, ``backend.log``, ``model_stub.log``,
``report.json`` and ``screenshots/``. Reports only ever reference
repository-relative paths, never an absolute machine path.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from harness.qa_bridge import PROJECT_DIR, REPO_ROOT

RUNS_ROOT = PROJECT_DIR / ".runs"


def create_run_dir(label: str = "run") -> Path:
    """Create a fresh, uniquely named run directory."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = RUNS_ROOT / f"{stamp}-{label}"
    candidate = base
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = RUNS_ROOT / f"{stamp}-{label}-{suffix}"
    (candidate / "screenshots").mkdir(parents=True, exist_ok=True)
    return candidate


def relative(path) -> str:
    """Return a repository-relative label for a path inside the repository."""
    if path is None:
        return ""
    resolved = Path(path)
    for root in (REPO_ROOT, PROJECT_DIR):
        try:
            return resolved.relative_to(root).as_posix()
        except ValueError:
            continue
    return resolved.name


def write_json(path, payload) -> Path:
    """Write pretty JSON with a trailing newline."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def write_report(run_dir, payload: dict) -> Path:
    """Write ``report.json`` into the run directory."""
    report = dict(payload)
    report.setdefault("run_dir", relative(run_dir))
    return write_json(Path(run_dir) / "report.json", report)
