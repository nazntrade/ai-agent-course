"""Offline unit tests for the D22 manual eval materialization scenario.

The scenario file is hyphenated, so it is loaded explicitly; only its pure
functions are exercised here. No network, no real run store and no LIVE.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCENARIO_PATH = Path(__file__).resolve().parents[1] / "scenarios" / "d22-save-eval.py"
_spec = importlib.util.spec_from_file_location("d22_save_eval", SCENARIO_PATH)
assert _spec and _spec.loader
d22 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(d22)


def _questions() -> list[dict]:
    # Keep real expected facts; synthetic questions only isolate run storage.
    real = d22.load_questions(d22.QUESTIONS_PATH)
    return [{**question, "question": f"Question number {i}?"} for i, question in enumerate(real, 1)]

def _questions_file(tmp_path: Path) -> Path:
    path = tmp_path / "questions.json"
    path.write_text(
        json.dumps({"schema_version": "rag-eval-questions-v1", "questions": _questions()}),
        encoding="utf-8",
    )
    return path


def _write_run(
    runs_root: Path,
    name: str,
    *,
    truncated_ids: frozenset[int] = frozenset(),
    receipt_status: str = "COMPLETED",
    pairs_completed: int = 10,
    summary_count: int = 20,
) -> Path:
    run_root = runs_root / name
    run_root.mkdir(parents=True)
    (run_root / "rag-eval-receipt.json").write_text(
        json.dumps({"runner_status": receipt_status, "pairs_completed": pairs_completed}),
        encoding="utf-8",
    )
    summaries: list[dict] = []
    ordinal = 0
    for question_number in range(1, 11):
        for mode in ("with_rag", "without_rag"):
            ordinal += 1
            run_id = f"{name}-{question_number:02d}-{mode}"
            truncated = ordinal in truncated_ids
            record = {
                "schema_version": "chat-run-v1",
                "run_id": run_id,
                "question": f"Question number {question_number}?",
                "mode": mode,
                "answer": {"truncated": truncated},
            }
            (run_root / f"{run_id}.json").write_text(json.dumps(record), encoding="utf-8")
            summaries.append(
                {"run_id": run_id, "mode": mode, "question": record["question"], "truncated": truncated}
            )
    (run_root / "runs.jsonl").write_text(
        "\n".join(json.dumps(entry) for entry in summaries[:summary_count]) + "\n",
        encoding="utf-8",
    )
    (run_root / "eval-summary-test.json").write_text("{}", encoding="utf-8")
    return run_root


def _evaluation(run_root: Path, run_id: str) -> dict:
    return json.loads((run_root / "evaluations" / f"{run_id}.json").read_text(encoding="utf-8"))


def _evaluation_snapshot(run_root: Path) -> dict[str, tuple[str, int]]:
    """Map each saved evaluation file to its content hash and modification time."""
    evaluations_dir = run_root / "evaluations"
    return {
        path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in sorted(evaluations_dir.iterdir())
        if path.is_file()
    }


def _evaluated_at(run_root: Path) -> dict[str, str]:
    return {
        path.name: _evaluation(run_root, path.name[: -len(".json")])["evaluated_at"]
        for path in sorted((run_root / "evaluations").iterdir())
        if path.is_file()
    }


def _run_id(run_root: Path, question_number: int, mode: str) -> str:
    for entry in d22.load_run_summaries(run_root):
        if entry["mode"] == mode and entry["question"] == f"Question number {question_number}?":
            return entry["run_id"]
    raise AssertionError("run not found")


# -- selector --------------------------------------------------------------
def test_selector_returns_single_completed_run(tmp_path):
    run_root = _write_run(tmp_path / "runs", "rag-eval-only")
    assert d22.find_completed_run_root(tmp_path / "runs") == run_root


def test_selector_prefers_the_complete_run_among_candidates(tmp_path):
    _write_run(tmp_path / "runs", "rag-eval-superseded", truncated_ids=frozenset({1, 3, 5, 7}))
    clean = _write_run(tmp_path / "runs", "rag-eval-clean")
    assert d22.find_completed_run_root(tmp_path / "runs") == clean


def test_selector_rejects_no_candidate(tmp_path):
    _write_run(tmp_path / "runs", "rag-eval-failed", receipt_status="FAIL")
    with pytest.raises(d22.EvalSaveError):
        d22.find_completed_run_root(tmp_path / "runs")


def test_selector_rejects_a_receipt_with_wrong_pair_count(tmp_path):
    _write_run(tmp_path / "runs", "rag-eval-partial", pairs_completed=4)
    with pytest.raises(d22.EvalSaveError):
        d22.find_completed_run_root(tmp_path / "runs")


def test_selector_rejects_completed_but_truncated(tmp_path):
    _write_run(tmp_path / "runs", "rag-eval-truncated", truncated_ids=frozenset({2, 4}))
    with pytest.raises(d22.EvalSaveError):
        d22.find_completed_run_root(tmp_path / "runs")


def test_selector_rejects_two_complete_candidates(tmp_path):
    _write_run(tmp_path / "runs", "rag-eval-a")
    _write_run(tmp_path / "runs", "rag-eval-b")
    with pytest.raises(d22.EvalSaveError):
        d22.find_completed_run_root(tmp_path / "runs")


def test_selector_rejects_incomplete_summary_count(tmp_path):
    _write_run(tmp_path / "runs", "rag-eval-short", summary_count=19)
    with pytest.raises(d22.EvalSaveError):
        d22.find_completed_run_root(tmp_path / "runs")


# -- materialization -------------------------------------------------------
def test_materialize_writes_twenty_separate_evaluations(tmp_path):
    runs_root = tmp_path / "chat-runs"
    run_root = _write_run(runs_root, "rag-eval-f6df68b8")
    questions_path = _questions_file(tmp_path)
    before = d22.immutable_snapshot(run_root)

    result_root, saved, total = d22.materialize(runs_root, questions_path)

    assert result_root == run_root
    assert (saved, total) == (20, 20)
    evaluation_files = sorted(path.name for path in (run_root / "evaluations").iterdir())
    assert len(evaluation_files) == 20
    answerable = {q["question"] for q in _questions() if q["answerable"]}
    for entry in d22.load_run_summaries(run_root):
        payload = _evaluation(run_root, entry["run_id"])
        assert payload["schema_version"] == "chat-eval-v1"
        assert payload["run_id"] == entry["run_id"]
        assert payload["evaluator"] == "D22 manual assessment"
        for section in ("retrieval", "content", "sources"):
            assert section in payload
        if entry["mode"] == "without_rag":
            assert payload["retrieval"] is None
            assert payload["sources"] is None
            assert payload["content"]["grounded"] is False
            assert "baseline without retrieval" in payload["notes"]
        elif entry["question"] in answerable:
            assert isinstance(payload["retrieval"], dict)
            assert isinstance(payload["sources"], dict)
        else:
            assert payload["retrieval"] is None
            assert isinstance(payload["sources"], dict)
    assert d22.immutable_snapshot(run_root) == before


def test_manual_scores_match_the_review(tmp_path):
    runs_root = tmp_path / "chat-runs"
    run_root = _write_run(runs_root, "rag-eval-f6df68b8")
    d22.materialize(runs_root, _questions_file(tmp_path))

    q01 = _evaluation(run_root, _run_id(run_root, 1, "with_rag"))
    assert q01["retrieval"]["score"] == 1
    assert q01["content"]["expected_facts_present"] == 5
    assert q01["content"]["expected_facts_total"] == 5
    assert q01["overall"] == "partial"

    q06 = _evaluation(run_root, _run_id(run_root, 6, "with_rag"))
    assert q06["retrieval"]["score"] == 0
    assert q06["content"]["expected_facts_present"] == 2
    assert q06["overall"] == "partial"

    q10 = _evaluation(run_root, _run_id(run_root, 10, "with_rag"))
    assert q10["retrieval"] is None
    assert q10["content"]["grounded"] is True
    assert q10["content"]["score"] == 2
    assert q10["overall"] == "pass"

    q10_baseline = _evaluation(run_root, _run_id(run_root, 10, "without_rag"))
    assert "unsupported by this corpus" in q10_baseline["content"]["notes"]
    assert q10_baseline["content"]["score"] == 0
    assert q10_baseline["overall"] == "fail"


def test_materialize_is_idempotent(tmp_path):
    runs_root = tmp_path / "chat-runs"
    run_root = _write_run(runs_root, "rag-eval-f6df68b8")
    questions_path = _questions_file(tmp_path)
    d22.materialize(runs_root, questions_path)
    immutable_before = d22.immutable_snapshot(run_root)
    evaluations_before = _evaluation_snapshot(run_root)
    evaluated_at_before = _evaluated_at(run_root)

    d22.materialize(runs_root, questions_path)

    assert d22.immutable_snapshot(run_root) == immutable_before
    assert _evaluation_snapshot(run_root) == evaluations_before
    assert _evaluated_at(run_root) == evaluated_at_before
    assert len(list((run_root / "evaluations").iterdir())) == 20


def test_changed_assessment_is_rewritten(tmp_path, monkeypatch):
    runs_root = tmp_path / "chat-runs"
    run_root = _write_run(runs_root, "rag-eval-f6df68b8")
    questions_path = _questions_file(tmp_path)
    d22.materialize(runs_root, questions_path)
    run_id = _run_id(run_root, 1, "with_rag")
    before = _evaluation_snapshot(run_root)[f"{run_id}.json"]

    original = d22._WITH_RAG["D22-Q01"]
    changed = {**original, "content": {**original["content"], "score": 1}}
    monkeypatch.setitem(d22._WITH_RAG, "D22-Q01", changed)

    d22.materialize(runs_root, questions_path)

    assert _evaluation_snapshot(run_root)[f"{run_id}.json"] != before
    assert _evaluation(run_root, run_id)["content"]["score"] == 1


def test_materialize_rejects_missing_run_record(tmp_path):
    runs_root = tmp_path / "chat-runs"
    run_root = _write_run(runs_root, "rag-eval-f6df68b8")
    (run_root / f"{_run_id(run_root, 1, 'with_rag')}.json").unlink()
    with pytest.raises(d22.EvalSaveError):
        d22.materialize(runs_root, _questions_file(tmp_path))


# -- validation detectors --------------------------------------------------
def test_missing_evaluation_file_is_detected(tmp_path):
    runs_root = tmp_path / "chat-runs"
    run_root = _write_run(runs_root, "rag-eval-f6df68b8")
    questions_path = _questions_file(tmp_path)
    d22.materialize(runs_root, questions_path)
    summaries = d22.load_run_summaries(run_root)
    (run_root / "evaluations" / f"{summaries[0]['run_id']}.json").unlink()
    with pytest.raises(d22.EvalSaveError):
        d22.validate_saved_evaluations(run_root, summaries, d22.load_questions(questions_path))


def test_wrong_schema_version_is_detected(tmp_path):
    runs_root = tmp_path / "chat-runs"
    run_root = _write_run(runs_root, "rag-eval-f6df68b8")
    questions_path = _questions_file(tmp_path)
    d22.materialize(runs_root, questions_path)
    summaries = d22.load_run_summaries(run_root)
    target = run_root / "evaluations" / f"{summaries[0]['run_id']}.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["schema_version"] = "chat-eval-v0"
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(d22.EvalSaveError):
        d22.validate_saved_evaluations(run_root, summaries, d22.load_questions(questions_path))


def test_without_rag_sections_must_stay_null(tmp_path):
    runs_root = tmp_path / "chat-runs"
    run_root = _write_run(runs_root, "rag-eval-f6df68b8")
    questions_path = _questions_file(tmp_path)
    d22.materialize(runs_root, questions_path)
    summaries = d22.load_run_summaries(run_root)
    without_rag = next(entry for entry in summaries if entry["mode"] == "without_rag")
    target = run_root / "evaluations" / f"{without_rag['run_id']}.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["retrieval"] = {"score": 2}
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(d22.EvalSaveError):
        d22.validate_saved_evaluations(run_root, summaries, d22.load_questions(questions_path))


def test_immutable_snapshot_changes_when_a_run_record_changes(tmp_path):
    run_root = _write_run(tmp_path / "runs", "rag-eval-f6df68b8")
    before = d22.immutable_snapshot(run_root)
    target = run_root / "runs.jsonl"
    target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    after = d22.immutable_snapshot(run_root)
    assert before != after
    assert before["runs.jsonl"] != after["runs.jsonl"]

# D22-13: independent manual-review expectations for all twenty saved answers.
# A score is completion/partial/non-completion, never a proxy for retrieval mode.
@pytest.mark.parametrize(
    "mode,expected",
    [
        ("with_rag", [(5, 2), (2, 1), (4, 2), (4, 2), (4, 2), (2, 1), (1, 1), (4, 2), (1, 1), (0, 2)]),
        ("without_rag", [(5, 2), (1, 1), (4, 2), (4, 2), (4, 2), (4, 2), (4, 2), (4, 2), (0, 0), (0, 0)]),
    ],
)
def test_all_twenty_content_assessments_match_semantic_review(tmp_path, mode, expected):
    run_root = _write_run(tmp_path / "runs", "rag-eval-reviewed")
    d22.materialize(tmp_path / "runs", _questions_file(tmp_path))
    for number, (present, score) in enumerate(expected, 1):
        payload = _evaluation(run_root, _run_id(run_root, number, mode))
        total = len(_questions()[number - 1]["expected_facts"])
        content = payload["content"]
        assert (content["expected_facts_present"], content["expected_facts_total"], content["score"]) == (present, total, score)
        assert content["grounded"] is (mode == "with_rag")
        assert f"{present} of {total} expected facts" in content["notes"]
        if mode == "without_rag":
            assert payload["retrieval"] is None
            assert payload["sources"] is None
            assert "N/A (null), not score 0" in payload["notes"]


def test_q02_and_q07_missing_facts_are_explicit(tmp_path):
    run_root = _write_run(tmp_path / "runs", "rag-eval-reviewed")
    d22.materialize(tmp_path / "runs", _questions_file(tmp_path))
    q02 = _evaluation(run_root, _run_id(run_root, 2, "with_rag"))["content"]
    q07 = _evaluation(run_root, _run_id(run_root, 7, "with_rag"))["content"]
    assert "present: profile, role; missing: goal, agent identity" in q02["notes"]
    assert "present: tool; missing: API, external tool, action" in q07["notes"]


def test_semantic_coverage_is_not_keyword_presence(tmp_path):
    run_root = _write_run(tmp_path / "runs", "rag-eval-reviewed")
    d22.materialize(tmp_path / "runs", _questions_file(tmp_path))
    q08 = _evaluation(run_root, _run_id(run_root, 8, "with_rag"))
    q09 = _evaluation(run_root, _run_id(run_root, 9, "with_rag"))
    assert q08["content"]["score"] == 2  # interaction covered by social/dialogue evaluation
    assert q08["retrieval"]["score"] == 1  # expected 4.1 section still missing
    assert q08["overall"] == "partial"
    assert q09["content"]["score"] == 1  # prompt robustness explains a challenge


def test_grounding_and_fact_coverage_are_independent(tmp_path):
    run_root = _write_run(tmp_path / "runs", "rag-eval-reviewed")
    d22.materialize(tmp_path / "runs", _questions_file(tmp_path))
    baseline = _evaluation(run_root, _run_id(run_root, 6, "without_rag"))
    assert baseline["content"]["expected_facts_present"] == 4
    assert baseline["content"]["score"] == 2
    assert baseline["content"]["grounded"] is False
    assert baseline["retrieval"] is None and baseline["sources"] is None


def test_q10_refusal_is_judged_by_nonfabrication(tmp_path):
    run_root = _write_run(tmp_path / "runs", "rag-eval-reviewed")
    d22.materialize(tmp_path / "runs", _questions_file(tmp_path))
    rag = _evaluation(run_root, _run_id(run_root, 10, "with_rag"))
    baseline = _evaluation(run_root, _run_id(run_root, 10, "without_rag"))
    assert rag["content"]["expected_facts_total"] == baseline["content"]["expected_facts_total"] == 0
    assert rag["content"]["score"] == 2 and rag["overall"] == "pass"
    assert baseline["content"]["score"] == 0 and baseline["overall"] == "fail"
    assert "51,773,000 kWh" in baseline["content"]["notes"]
    assert "unsupported by this corpus" in baseline["content"]["notes"]


def test_inconsistent_manual_count_is_rejected(tmp_path, monkeypatch):
    run_root = _write_run(tmp_path / "runs", "rag-eval-reviewed")
    original = d22._WITH_RAG["D22-Q02"]
    monkeypatch.setitem(d22._WITH_RAG, "D22-Q02", {**original, "content": {**original["content"], "present": 3}})
    with pytest.raises(d22.EvalSaveError, match="count disagrees"):
        d22.materialize(tmp_path / "runs", _questions_file(tmp_path))
    assert not (run_root / "evaluations").exists()


def test_changed_assessment_rewrites_only_its_evaluation(tmp_path, monkeypatch):
    run_root = _write_run(tmp_path / "runs", "rag-eval-reviewed")
    questions_path = _questions_file(tmp_path)
    d22.materialize(tmp_path / "runs", questions_path)
    immutable = d22.immutable_snapshot(run_root)
    before = _evaluation_snapshot(run_root)
    run_id = _run_id(run_root, 2, "without_rag")
    original = d22._WITHOUT_RAG["D22-Q02"]
    monkeypatch.setitem(d22._WITHOUT_RAG, "D22-Q02", {**original, "notes": original["notes"] + " Reviewed."})
    d22.materialize(tmp_path / "runs", questions_path)
    after = _evaluation_snapshot(run_root)
    assert [name for name in before if before[name] != after[name]] == [f"{run_id}.json"]
    assert d22.immutable_snapshot(run_root) == immutable