"""File-backed run records and manual evaluations (SPEC D22 15).

Runs are immutable: the full record is written once as ``<run_id>.json`` and a
summary line is appended to ``runs.jsonl``. A manual evaluation is a separate,
overwritable file under ``evaluations/``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..domain.contracts import ChatRunStore

RUN_SCHEMA_VERSION = "chat-run-v1"
EVAL_SCHEMA_VERSION = "chat-eval-v1"
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class FileChatRunStore(ChatRunStore):
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.evaluations_dir = self.root / "evaluations"
        self.index_path = self.root / "runs.jsonl"

    # -- helpers ----------------------------------------------------------
    def _ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _run_path(self, run_id: str) -> Path:
        return self.root / f"{_safe_id(run_id)}.json"

    def _evaluation_path(self, run_id: str) -> Path:
        return self.evaluations_dir / f"{_safe_id(run_id)}.json"

    # -- runs -------------------------------------------------------------
    def create_run(self, record: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(record)
        payload.setdefault("schema_version", RUN_SCHEMA_VERSION)
        run_id = payload.get("run_id")
        if not run_id:
            raise ValueError("A run record requires a run_id.")
        self._ensure_dirs()
        path = self._run_path(str(run_id))
        if path.exists():
            raise FileExistsError(f"Run record already exists: {run_id}")
        _atomic_write_json(path, payload)
        with self.index_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_summary(payload), ensure_ascii=False) + "\n")
        return payload

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        path = self._run_path(run_id)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_runs(
        self,
        *,
        limit: int = 20,
        kind: str | None = None,
        mode: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self.index_path.is_file():
            return []
        summaries: list[dict[str, Any]] = []
        for raw_line in self.index_path.read_text(encoding="utf-8").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if kind and entry.get("result_kind") != kind:
                continue
            if mode and entry.get("mode") != mode:
                continue
            summaries.append(entry)
        summaries.sort(key=lambda entry: entry.get("created_at") or "", reverse=True)
        return summaries[: max(0, int(limit))]

    # -- evaluations ------------------------------------------------------
    def save_evaluation(self, evaluation: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(evaluation)
        run_id = payload.get("run_id")
        if not run_id:
            raise ValueError("A manual evaluation requires a run_id.")
        if self.get_run(str(run_id)) is None:
            raise KeyError(run_id)
        payload["schema_version"] = EVAL_SCHEMA_VERSION
        payload.setdefault("evaluated_at", _utcnow())
        self._ensure_dirs()
        self.evaluations_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self._evaluation_path(str(run_id)), payload)
        return payload

    def get_evaluation(self, run_id: str) -> dict[str, Any] | None:
        path = self._evaluation_path(run_id)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))


def _summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    model = payload.get("model")
    model_name = model.get("model") if isinstance(model, dict) else model
    index = payload.get("index")
    index_version_id = index.get("index_version_id") if isinstance(index, dict) else None
    answer = payload.get("answer")

    truncated: bool | None = None
    if isinstance(answer, dict):
        truncated = bool(answer.get("truncated"))
    elif payload.get("result_kind") == "compare":
        branches = payload.get("branches") or {}
        flags = [
            bool(branch.get("answer", {}).get("truncated"))
            for branch in branches.values()
            if isinstance(branch, dict) and isinstance(branch.get("answer"), dict)
        ]
        truncated = any(flags) if flags else None

    return {
        "run_id": payload.get("run_id"),
        "created_at": payload.get("created_at"),
        "result_kind": payload.get("result_kind"),
        "mode": payload.get("mode"),
        "question": payload.get("question"),
        "model": model_name,
        "index_version_id": index_version_id,
        "truncated": truncated,
        "errors": payload.get("errors") or [],
    }


def _safe_id(value: str) -> str:
    cleaned = _SAFE_ID.sub("_", str(value))
    return cleaned or "run"


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    text = json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=".tmp-",
        delete=False,
    )
    try:
        with handle:
            handle.write(text)
        os.replace(handle.name, path)
    except Exception:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
