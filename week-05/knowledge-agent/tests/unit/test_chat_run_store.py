"""FileChatRunStore: immutable runs, runs.jsonl index and evaluations (D22-11/13)."""

from __future__ import annotations

import json

import pytest

from knowledge_agent.chat.run_store import FileChatRunStore


def _record(run_id: str, *, created_at: str = "2026-10-01T10:00:00+00:00", kind: str = "single", mode: str = "with_rag") -> dict:
    return {
        "schema_version": "chat-run-v1",
        "run_id": run_id,
        "created_at": created_at,
        "result_kind": kind,
        "mode": mode,
        "question": "q",
        "model": {"model": "fake-chat"},
        "index": {"index_version_id": "idx-1"},
        "answer": {"truncated": False},
        "errors": [],
    }


def test_create_get_and_immutability(tmp_path):
    store = FileChatRunStore(tmp_path / "runs")
    created = store.create_run(_record("d22-1"))
    assert created["schema_version"] == "chat-run-v1"
    assert (tmp_path / "runs" / "d22-1.json").is_file()
    assert store.get_run("d22-1")["run_id"] == "d22-1"
    assert store.get_run("missing") is None
    with pytest.raises(FileExistsError):
        store.create_run(_record("d22-1"))


def test_runs_jsonl_index_and_filters(tmp_path):
    store = FileChatRunStore(tmp_path / "runs")
    store.create_run(_record("d22-a", created_at="2026-10-01T10:00:00+00:00", mode="with_rag"))
    store.create_run(_record("d22-b", created_at="2026-10-01T11:00:00+00:00", mode="without_rag"))
    store.create_run(_record("d22-c", created_at="2026-10-01T12:00:00+00:00", kind="compare", mode="compare"))
    index_lines = (tmp_path / "runs" / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(index_lines) == 3
    summary = json.loads(index_lines[0])
    assert {"run_id", "created_at", "result_kind", "mode", "question", "model", "index_version_id", "truncated", "errors"} <= set(summary)

    all_runs = store.list_runs(limit=10)
    assert [run["run_id"] for run in all_runs] == ["d22-c", "d22-b", "d22-a"]
    assert [run["run_id"] for run in store.list_runs(limit=10, kind="single")] == ["d22-b", "d22-a"]
    assert [run["run_id"] for run in store.list_runs(limit=10, kind="compare", mode="compare")] == ["d22-c"]
    assert store.list_runs(limit=10, mode="without_rag")[0]["run_id"] == "d22-b"


def test_evaluation_is_separate_and_overwritable(tmp_path):
    store = FileChatRunStore(tmp_path / "runs")
    store.create_run(_record("d22-eval"))
    saved = store.save_evaluation({"run_id": "d22-eval", "overall": "pass"})
    assert saved["schema_version"] == "chat-eval-v1"
    assert saved["evaluated_at"]
    assert (tmp_path / "runs" / "evaluations" / "d22-eval.json").is_file()
    updated = store.save_evaluation({"run_id": "d22-eval", "overall": "fail"})
    assert updated["overall"] == "fail"
    assert store.get_evaluation("d22-eval")["overall"] == "fail"
    assert store.get_evaluation("missing") is None


def test_evaluation_for_missing_run_is_rejected(tmp_path):
    store = FileChatRunStore(tmp_path / "runs")
    with pytest.raises(KeyError):
        store.save_evaluation({"run_id": "missing", "overall": "pass"})


def test_unknown_run_ids_cannot_escape_the_root(tmp_path):
    store = FileChatRunStore(tmp_path / "runs")
    created = store.create_run(_record("../outside"))
    assert created["run_id"] == "../outside"
    assert not (tmp_path / "outside.json").exists()
