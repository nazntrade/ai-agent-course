"""Materialize the D22 manual ``chat-eval-v1`` assessments (offline, no network).

The scenario selects the single completed D22 evaluation run, writes a separate
``chat-eval-v1`` file for each of its runs through the existing
:class:`knowledge_agent.chat.run_store.FileChatRunStore`, and proves that the
immutable ``<run_id>.json`` records, ``runs.jsonl`` and the harness
``eval-summary`` are not touched. Materialization is idempotent: an existing
evaluation whose content already matches the manual assessment is left
byte-for-byte untouched (including its original ``evaluated_at``), so repeated
offline runs never invalidate an evaluation snapshot.

A receipt alone does not prove a usable manual-assessment snapshot. The selector
independently excludes candidates whose saved summaries contain truncated
answers, including superseded 1024-token runs. Runner completion does not assert
answer quality; the receipt's quality_status remains NOT_ASSESSED.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

MODULE_DIR = Path(__file__).resolve().parents[2]
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from knowledge_agent.chat.run_store import EVAL_SCHEMA_VERSION, RUN_SCHEMA_VERSION, FileChatRunStore

QUESTIONS_PATH = MODULE_DIR / "eval" / "d22" / "questions.json"
RUNS_ROOT = MODULE_DIR / "local-data" / "chat-runs"
EVALUATOR = "D22 manual assessment"
EXPECTED_PAIRS = 10
EXPECTED_RUN_COUNT = EXPECTED_PAIRS * 2


class EvalSaveError(RuntimeError):
    """The completed run or its evaluations cannot be materialized safely."""


# Manual assessment of the RAG answers, keyed by question_id and derived
# from the reviewer's reading of the saved runs. Numbers follow SPEC 14.2: the
# three sections are scored independently on the 0/1/2 scale.
_WITH_RAG: dict[str, dict[str, Any]] = {
    "D22-Q01": {
        "retrieval": {
            "expected_sections_hit": True,
            "expected_pages_hit": True,
            "passed_relevant": True,
            "score": 1,
            "notes": "Expected architecture section/pages present; only 2/5 passed chunks directly relevant, with Introduction, Conclusion and Challenges off target.",
        },
        "content": {"present": 5, "grounded": True, "score": 2, "notes": "All five architecture facts present."},
        "sources": {"score": 2, "notes": "Citation IDs resolve to passed chunks and provenance is valid; this does not prove semantic support for every claim."},
        "overall": "partial",
    },
    "D22-Q02": {
        "retrieval": {
            "expected_sections_hit": True,
            "expected_pages_hit": True,
            "passed_relevant": True,
            "score": 1,
            "notes": "Expected profiling section/pages present; 3/5 passed chunks relevant, with Prompt Robustness and Objective Evaluation off target.",
        },
        "content": {"present": 2, "grounded": True, "score": 1, "notes": "Profile and role present; goal and agent identity absent."},
        "sources": {"score": 2, "notes": "Citation IDs resolve to passed chunks and provenance is valid; this does not prove semantic support for every claim."},
        "overall": "partial",
    },
    "D22-Q03": {
        "retrieval": {
            "expected_sections_hit": True,
            "expected_pages_hit": True,
            "passed_relevant": True,
            "score": 2,
            "notes": "2.1.2 Memory Module (pages 5-10) present in passed.",
        },
        "content": {"present": 4, "grounded": True, "score": 2, "notes": "All four memory facts present."},
        "sources": {"score": 2, "notes": "Citation IDs resolve to passed chunks and provenance is valid; this does not prove semantic support for every claim."},
        "overall": "pass",
    },
    "D22-Q04": {
        "retrieval": {
            "expected_sections_hit": True,
            "expected_pages_hit": True,
            "passed_relevant": True,
            "score": 2,
            "notes": "2.1.3 Planning Module (pages 10-13) present in passed.",
        },
        "content": {"present": 4, "grounded": True, "score": 2, "notes": "All four planning facts present."},
        "sources": {"score": 2, "notes": "Citation IDs resolve to passed chunks and provenance is valid; this does not prove semantic support for every claim."},
        "overall": "pass",
    },
    "D22-Q05": {
        "retrieval": {
            "expected_sections_hit": True,
            "expected_pages_hit": True,
            "passed_relevant": True,
            "score": 2,
            "notes": "2.1.4 Action Module (pages 13-16) present in passed.",
        },
        "content": {"present": 4, "grounded": True, "score": 2, "notes": "All four action facts present."},
        "sources": {"score": 2, "notes": "Citation IDs resolve to passed chunks and provenance is valid; this does not prove semantic support for every claim."},
        "overall": "pass",
    },
    "D22-Q06": {
        "retrieval": {
            "expected_sections_hit": False,
            "expected_pages_hit": False,
            "passed_relevant": False,
            "score": 0,
            "notes": "Section 2.2 Agent Capability Acquisition and pages 16-21 were not retrieved.",
        },
        "content": {
            "present": 2,
            "grounded": True,
            "score": 1,
            "notes": "2 of 4 facts; the answer honestly flags the missing section.",
        },
        "sources": {"score": 2, "notes": "Citation IDs resolve to passed chunks and provenance is valid; this does not prove semantic support for every claim."},
        "overall": "partial",
    },
    "D22-Q07": {
        "retrieval": {
            "expected_sections_hit": False,
            "expected_pages_hit": False,
            "passed_relevant": True,
            "score": 1,
            "notes": "3.3 Engineering (pages 24-27) present; 2.1.4 Action Module missing.",
        },
        "content": {"present": 1, "grounded": True, "score": 1, "notes": "BMTools supports tool building/sharing; API, external tool and action not explained."},
        "sources": {"score": 2, "notes": "Citation IDs resolve to passed chunks and provenance is valid; this does not prove semantic support for every claim."},
        "overall": "partial",
    },
    "D22-Q08": {
        "retrieval": {
            "expected_sections_hit": False,
            "expected_pages_hit": False,
            "passed_relevant": True,
            "score": 1,
            "notes": "4.2 Objective Evaluation present; 4.1 Subjective Evaluation missing.",
        },
        "content": {"present": 4, "grounded": True, "score": 2, "notes": "Evaluation, benchmark and human present; interaction covered by social/real-world protocols and dialogue evaluation."},
        "sources": {"score": 2, "notes": "Citation IDs resolve to passed chunks and provenance is valid; this does not prove semantic support for every claim."},
        "overall": "partial",
    },
    "D22-Q09": {
        "retrieval": {
            "expected_sections_hit": False,
            "expected_pages_hit": False,
            "passed_relevant": True,
            "score": 1,
            "notes": "6 Challenges present; 7 Conclusion missing.",
        },
        "content": {"present": 1, "grounded": True, "score": 1, "notes": "Challenge present via prompt robustness; generalization, safety and efficiency absent."},
        "sources": {"score": 2, "notes": "Citation IDs resolve to passed chunks and provenance is valid; this does not prove semantic support for every claim."},
        "overall": "partial",
    },
    # Unanswerable question: there is no retrieval target, so the retrieval section
    # stays null and the note records that; content is judged by non-fabrication.
    "D22-Q10": {
        "retrieval": None,
        "content": {
            "present": 0,
            "grounded": True,
            "score": 2,
            "notes": "Correct refusal; the GPT-4 energy figure is not invented.",
        },
        "sources": {"score": 2, "notes": "No citations required or supplied; valid absence of citations does not establish semantic correctness by itself."},
        "overall": "pass",
        "notes": "answerable:false; retrieval not applicable; correct refusal without fabrication.",
    },
}

# Manual semantic coverage of the frozen completed NETWORK run reviewed above.
# These are expected-fact labels, not automatic substring matches. Generic
# baseline answers can cover facts even though no retrieved source grounds them.
_COVERED_FACTS = {
    "with_rag": {
        "D22-Q01": ["agent architecture", "profiling module", "memory module", "planning module", "action module"],
        "D22-Q02": ["profile", "role"],
        "D22-Q03": ["short-term memory", "long-term memory", "memory", "retrieval"],
        "D22-Q04": ["planning", "feedback", "task decomposition", "reflection"],
        "D22-Q05": ["action", "tool use", "memory", "external environment"],
        "D22-Q06": ["learning", "reinforcement learning"],
        "D22-Q07": ["tool"],
        "D22-Q08": ["evaluation", "benchmark", "interaction", "human"],
        "D22-Q09": ["challenge"],
        "D22-Q10": [],
    },
    "without_rag": {
        "D22-Q01": ["agent architecture", "profiling module", "memory module", "planning module", "action module"],
        "D22-Q02": ["profile"],
        "D22-Q03": ["short-term memory", "long-term memory", "memory", "retrieval"],
        "D22-Q04": ["planning", "feedback", "task decomposition", "reflection"],
        "D22-Q05": ["action", "tool use", "memory", "external environment"],
        "D22-Q06": ["learning", "reinforcement learning", "demonstration", "supervision"],
        "D22-Q07": ["tool", "API", "external tool", "action"],
        "D22-Q08": ["evaluation", "benchmark", "interaction", "human"],
        "D22-Q09": [],
        "D22-Q10": [],
    },
}
_WITHOUT_RAG = {
    "D22-Q01": {"score": 2, "overall": "pass", "notes": "Names the survey's four-module architecture; source attribution is not verified by retrieval."},
    "D22-Q02": {"score": 1, "overall": "partial", "notes": "Generic profiling/telemetry; user goals do not establish the agent's goal, role or identity."},
    "D22-Q03": {"score": 2, "overall": "pass", "notes": "Short/long-term memory and retrieval explained in a generic architecture."},
    "D22-Q04": {"score": 2, "overall": "pass", "notes": "Planning, feedback and task decomposition explained; Reflexion covers reflection."},
    "D22-Q05": {"score": 2, "overall": "pass", "notes": "Action execution, tool use, memory update and environment interaction explained generically."},
    "D22-Q06": {"score": 2, "overall": "pass", "notes": "Learning, reinforcement learning, demonstration and supervised learning explained generically."},
    "D22-Q07": {"score": 2, "overall": "pass", "notes": "Tools, APIs, external interaction and execution are described generically; the answer disclaims a verified survey summary."},
    "D22-Q08": {"score": 2, "overall": "pass", "notes": "Evaluation, benchmarks, interactive environments and human evaluation present; generic list not verified against the survey."},
    "D22-Q09": {"score": 0, "overall": "fail", "notes": "Only asks for the paper/conclusion; no expected challenge fact explained."},
    "D22-Q10": {"score": 0, "overall": "fail", "notes": "Gives 51,773,000 kWh despite lacking the survey. The attribution is unsupported by this corpus; this is not a correct refusal, without judging the estimate's truth elsewhere."},
}
_WITHOUT_RAG_NOTE = (
    "baseline without retrieval; retrieval and sources are N/A (null), not score 0; "
    "content score independently measures expected-fact coverage/non-fabrication; "
    "grounded=false means no retrieved-source grounding, not absent facts; "
    "overall describes content only, not verified survey attribution"
)


def _content_assessment(question: Mapping[str, Any], mode: str, spec: Mapping[str, Any]) -> dict[str, Any]:
    question_id = question.get("question_id")
    covered = _COVERED_FACTS[mode].get(question_id)
    expected = question.get("expected_facts") or []
    if covered is None or len(set(covered)) != len(covered) or not set(covered).issubset(expected):
        raise EvalSaveError(f"invalid manual fact coverage for {question_id}/{mode}")
    if "present" in spec and spec["present"] != len(covered):
        raise EvalSaveError(f"manual fact count disagrees with coverage for {question_id}/{mode}")
    missing = [fact for fact in expected if fact not in covered]
    coverage_note = (
        f"{len(covered)} of {len(expected)} expected facts; "
        f"present: {', '.join(covered) or 'none'}; missing: {', '.join(missing) or 'none'}. "
    )
    return {
        "expected_facts_present": len(covered),
        "expected_facts_total": len(expected),
        "grounded": mode == "with_rag",
        "score": spec["score"],
        "notes": coverage_note + spec["notes"],
    }


# -- small helpers ---------------------------------------------------------
def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_record(run_root: Path, run_id: Any) -> dict[str, Any]:
    path = run_root / f"{run_id}.json"
    if not path.is_file():
        raise EvalSaveError(f"missing run record {path.name}")
    return _read_json(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_questions(path: Path) -> list[dict[str, Any]]:
    data = _read_json(path)
    questions = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(questions, list) or not questions:
        raise EvalSaveError(f"{path} does not contain a non-empty question list")
    return questions


def load_run_summaries(run_root: Path) -> list[dict[str, Any]]:
    index = run_root / "runs.jsonl"
    if not index.is_file():
        raise EvalSaveError(f"missing runs.jsonl in {run_root.name}")
    summaries: list[dict[str, Any]] = []
    for line in index.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError as error:
            raise EvalSaveError(f"unreadable runs.jsonl line in {run_root.name}: {error}") from None
        if not isinstance(entry, dict):
            raise EvalSaveError(f"non-object runs.jsonl entry in {run_root.name}")
        summaries.append(entry)
    return summaries


def _receipt_marks_completed(run_root: Path) -> bool:
    receipt_path = run_root / "rag-eval-receipt.json"
    if not receipt_path.is_file():
        return False
    receipt = _read_json(receipt_path)
    if not isinstance(receipt, dict):
        return False
    return receipt.get("runner_status") == "COMPLETED" and receipt.get("pairs_completed") == EXPECTED_PAIRS


def _is_complete_run(run_root: Path) -> bool:
    summaries = load_run_summaries(run_root)
    if len(summaries) != EXPECTED_RUN_COUNT:
        return False
    modes = Counter(entry.get("mode") for entry in summaries)
    if modes.get("with_rag") != EXPECTED_PAIRS or modes.get("without_rag") != EXPECTED_PAIRS:
        return False
    for entry in summaries:
        if entry.get("truncated") is True:
            return False
        run_id = entry.get("run_id")
        if not run_id or not (run_root / f"{run_id}.json").is_file():
            return False
    return True


def find_completed_run_root(runs_root: Path) -> Path:
    """Return the single usable ``rag-eval-*`` run directory.

    Candidates first pass the receipt gate (``COMPLETED`` and 10 pairs). When
    the gate alone is ambiguous, the complete run (no truncated saved answers)
    is selected, because superseded pre-fix receipts can still claim COMPLETED.
    """
    roots = sorted(path for path in Path(runs_root).glob("rag-eval-*") if path.is_dir())
    candidates = [root for root in roots if _receipt_marks_completed(root)]
    if not candidates:
        raise EvalSaveError("no rag-eval run with runner_status=COMPLETED and pairs_completed=10")
    complete = [root for root in candidates if _is_complete_run(root)]
    if len(complete) == 1:
        return complete[0]
    names = ", ".join(root.name for root in candidates)
    if not complete:
        raise EvalSaveError(f"{len(candidates)} completed candidate(s) but none has complete answers: {names}")
    complete_names = ", ".join(root.name for root in complete)
    raise EvalSaveError(f"ambiguous completed runs ({len(complete)} complete): {complete_names}")


def validate_run_records(run_root: Path, summaries: list[dict[str, Any]], questions: list[dict[str, Any]]) -> None:
    if len(summaries) != EXPECTED_RUN_COUNT:
        raise EvalSaveError(f"expected {EXPECTED_RUN_COUNT} run records, found {len(summaries)}")
    modes = Counter(entry.get("mode") for entry in summaries)
    if modes.get("with_rag") != EXPECTED_PAIRS or modes.get("without_rag") != EXPECTED_PAIRS:
        raise EvalSaveError(f"expected {EXPECTED_PAIRS} with_rag and {EXPECTED_PAIRS} without_rag runs, found {dict(modes)}")
    known_questions = {question.get("question") for question in questions}
    seen_questions: set[Any] = set()
    for entry in summaries:
        run_id = entry.get("run_id")
        record = _read_record(run_root, run_id)
        if record.get("schema_version") != RUN_SCHEMA_VERSION:
            raise EvalSaveError(f"{run_id} is not a {RUN_SCHEMA_VERSION} record")
        if record.get("run_id") != run_id:
            raise EvalSaveError(f"{run_id} record run_id mismatch: {record.get('run_id')!r}")
        question = record.get("question")
        if question not in known_questions:
            raise EvalSaveError(f"{run_id} references a question outside eval/d22/questions.json")
        seen_questions.add(question)
    if seen_questions != known_questions:
        missing = len(known_questions - seen_questions)
        raise EvalSaveError(f"run does not cover all questions; missing {missing}")


def immutable_snapshot(run_root: Path) -> dict[str, str]:
    """Hash every flat immutable file; the evaluations directory is excluded."""
    return {path.name: _sha256(path) for path in sorted(run_root.iterdir()) if path.is_file()}


def evaluations_snapshot(run_root: Path) -> dict[str, str]:
    """Hash every saved evaluation file; repeated materialization must not change them."""
    evaluations_dir = run_root / "evaluations"
    if not evaluations_dir.is_dir():
        return {}
    return {path.name: _sha256(path) for path in sorted(evaluations_dir.iterdir()) if path.is_file()}


def evaluations_digest(run_root: Path) -> str:
    """Return one stable digest over the evaluations directory for run-to-run comparison."""
    snapshot = evaluations_snapshot(run_root)
    encoded = json.dumps(snapshot, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _evaluation_matches(existing: Mapping[str, Any], desired: Mapping[str, Any]) -> bool:
    """True when the saved evaluation already carries the desired content.

    ``evaluated_at`` records when the file was first written, so it is ignored
    when deciding whether the assessment itself changed; preserving it keeps the
    file bytes stable across repeated offline materialization.
    """
    saved = {key: value for key, value in existing.items() if key != "evaluated_at"}
    wanted = dict(desired)
    wanted.setdefault("schema_version", EVAL_SCHEMA_VERSION)
    return saved == wanted


def _with_rag_evaluation(run_id: str, question: Mapping[str, Any], spec: Mapping[str, Any]) -> dict[str, Any]:
    content = _content_assessment(question, "with_rag", spec["content"])
    sources = {
        "citations_valid": True,
        "unsupported_citations": 0,
        "provenance_correct": True,
        "score": spec["sources"]["score"],
        "notes": spec["sources"]["notes"],
    }
    return {
        "run_id": run_id,
        "evaluator": EVALUATOR,
        "retrieval": spec["retrieval"],
        "content": content,
        "sources": sources,
        "overall": spec["overall"],
        "notes": spec.get("notes", ""),
    }


def _without_rag_evaluation(run_id: str, question: Mapping[str, Any]) -> dict[str, Any]:
    question_id = question.get("question_id")
    spec = _WITHOUT_RAG.get(question_id)
    if spec is None:
        raise EvalSaveError(f"no manual baseline assessment recorded for {question_id}")
    return {
        "run_id": run_id,
        "evaluator": EVALUATOR,
        "retrieval": None,
        "content": _content_assessment(question, "without_rag", spec),
        "sources": None,
        "overall": spec["overall"],
        "notes": _WITHOUT_RAG_NOTE,
    }

def build_evaluations(run_root: Path, summaries: list[dict[str, Any]], questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    index = {question.get("question"): question for question in questions}
    evaluations: list[dict[str, Any]] = []
    for entry in summaries:
        run_id = entry["run_id"]
        record = _read_record(run_root, run_id)
        question = index[record["question"]]
        if record.get("mode") == "with_rag":
            question_id = question.get("question_id")
            if question_id not in _WITH_RAG:
                raise EvalSaveError(f"no manual assessment recorded for {question_id}")
            evaluations.append(_with_rag_evaluation(run_id, question, _WITH_RAG[question_id]))
        else:
            evaluations.append(_without_rag_evaluation(run_id, question))
    return evaluations


def validate_saved_evaluations(run_root: Path, summaries: list[dict[str, Any]], questions: list[dict[str, Any]]) -> None:
    evaluations_dir = run_root / "evaluations"
    if not evaluations_dir.is_dir():
        raise EvalSaveError("evaluations directory was not created")
    expected_files = {f"{entry['run_id']}.json" for entry in summaries}
    actual_files = {path.name for path in evaluations_dir.iterdir() if path.is_file()}
    if actual_files != expected_files:
        raise EvalSaveError(f"expected {EXPECTED_RUN_COUNT} evaluation files, found {len(actual_files)}")
    index = {question.get("question"): question for question in questions}
    for entry in summaries:
        run_id = entry["run_id"]
        evaluation = _read_json(evaluations_dir / f"{run_id}.json")
        if evaluation.get("schema_version") != EVAL_SCHEMA_VERSION:
            raise EvalSaveError(f"{run_id} evaluation is not {EVAL_SCHEMA_VERSION}")
        if evaluation.get("run_id") != run_id:
            raise EvalSaveError(f"{run_id} evaluation run_id mismatch")
        for section in ("retrieval", "content", "sources"):
            if section not in evaluation:
                raise EvalSaveError(f"{run_id} evaluation lacks the {section} section")
        record = _read_record(run_root, run_id)
        question = index[record["question"]]
        answerable = question.get("answerable", True)
        if not isinstance(evaluation["content"], dict):
            raise EvalSaveError(f"{run_id} evaluation content must be an object")
        if entry.get("mode") == "without_rag":
            if evaluation["retrieval"] is not None or evaluation["sources"] is not None:
                raise EvalSaveError(f"{run_id} without_rag sections must be null")
        elif answerable:
            if not isinstance(evaluation["retrieval"], dict) or not isinstance(evaluation["sources"], dict):
                raise EvalSaveError(f"{run_id} with_rag retrieval/sources must be objects")
        else:
            if evaluation["retrieval"] is not None or not isinstance(evaluation["sources"], dict):
                raise EvalSaveError(f"{run_id} unanswerable with_rag must have null retrieval and object sources")


def materialize(runs_root: Path, questions_path: Path) -> tuple[Path, int, int]:
    questions = load_questions(questions_path)
    run_root = find_completed_run_root(runs_root)
    summaries = load_run_summaries(run_root)
    validate_run_records(run_root, summaries, questions)
    before = immutable_snapshot(run_root)
    store = FileChatRunStore(run_root)
    evaluations = build_evaluations(run_root, summaries, questions)
    for evaluation in evaluations:
        run_id = str(evaluation["run_id"])
        existing = store.get_evaluation(run_id)
        if existing is not None and _evaluation_matches(existing, evaluation):
            continue
        store.save_evaluation(evaluation)
    validate_saved_evaluations(run_root, summaries, questions)
    after = immutable_snapshot(run_root)
    if before != after:
        changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
        raise EvalSaveError(f"immutable records changed: {changed}")
    return run_root, len(evaluations), len(summaries)


def main(argv: list[str] | None = None) -> int:
    try:
        run_root, saved, total = materialize(RUNS_ROOT, QUESTIONS_PATH)
    except EvalSaveError as error:
        print(f"D22_EVAL_SAVE_STATUS: FAIL ({error})")
        return 1
    print(f"D22_EVAL_SAVE_RUN: {run_root.name}")
    print(f"D22_EVAL_SAVE_EVALS_DIGEST: {evaluations_digest(run_root)}")
    print(f"D22_EVAL_SAVE_STATUS: PASS ({saved}/{total})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
