"""D25 LIVE-runner assessment regressions (correction defects 1-3, C08)."""

from __future__ import annotations

import json
from pathlib import Path

from harness.d25_live import (
    _archive_previous,
    _answer_assessment,
    _project_turn,
    _run_id,
    scenario_verdict,
)

NOW = "2026-10-03T00:00:00+00:00"


def _citations(status: str = "verified") -> list[dict]:
    if status == "none":
        return []
    source_exists = status != "unknown_chunk_id"
    quote_verbatim = status == "verified"
    return [
        {
            "chunk_id": "c1",
            "status": status,
            "source_exists": source_exists,
            "quote_verbatim": quote_verbatim,
        }
    ]


def _record(
    index: int,
    *,
    status: str = "ok",
    text: str = "a completed and sufficiently long answer",
    finish: str = "stop",
    grounding_status: str | None = "verified",
    insufficient: bool = False,
    citation_status: str = "verified",
) -> dict:
    return {
        "_turn_index": index,
        "_attempts": 1,
        "turn_id": f"turn-{index}",
        "user_message": f"question {index}",
        "original_query": f"question {index}",
        "search_query": f"question {index}",
        "reference_resolution": {"ambiguous": False},
        "retrieval_performed": True,
        "retrieval": {
            "found_count": 3,
            "selected_count": 3,
            "passed_count": 3,
            "passed_chunk_ids": ["c1"],
        },
        "sources": [{"chunk_id": "c1", "source": "doc.pdf", "section": "S"}],
        "citations": _citations(citation_status),
        "context": {},
        "memory": {},
        "status": status,
        "answer": {
            "text": text,
            "finish_reason": finish,
            "truncated": finish == "length",
            "insufficient_sources": insufficient,
            "grounding_status": grounding_status,
        },
        "error": None,
    }


def _turns(count: int = 12, member_overrides: dict[int, dict] | None = None) -> list[dict]:
    overrides = member_overrides or {}
    turns = []
    for index in range(1, count + 1):
        record = _record(index, **overrides.get(index, {}))
        turns.append(_project_turn(record, {}, None, {"mandatory_answer": True}))
    return turns


def test_completed_answer_is_not_auto_substantive_pass():
    # Regression defect 2: a completed non-empty answer used to be labelled
    # substantive PASS. It may pass automated format checks, but the semantic
    # verdict stays NOT_ASSESSED until the independent Tester reports it.
    turns = _turns()
    assert all(turn["format_checks"]["status"] == "PASS" for turn in turns)
    assert all(turn["semantic"]["status"] == "NOT_ASSESSED" for turn in turns)
    verdict = scenario_verdict(turns)
    assert verdict["technical_completion"] is True
    assert verdict["automated_checks"] == "PASS"
    assert verdict["substantive_acceptance"] == "NOT_ASSESSED"


def test_truncated_mandatory_answer_fails_format_and_is_not_assessed():
    turns = _turns(member_overrides={
        3: {"status": "incomplete", "text": "cut off", "finish": "length", "grounding_status": "failed"},
    })
    assert turns[2]["format_checks"]["status"] == "FAIL"
    verdict = scenario_verdict(turns)
    assert verdict["technical_completion"] is False
    assert verdict["automated_checks"] == "FAIL"
    assert verdict["substantive_acceptance"] == "NOT_ASSESSED"
    assert 3 in verdict["errored_turns"]


def test_errored_turn_is_not_technically_complete():
    turns = _turns()
    turns[4] = _project_turn(
        {**_record(5), "status": "error", "answer": {"text": "", "finish_reason": None, "truncated": False}},
        {}, None, {"mandatory_answer": True},
    )
    turns[4]["error"] = {"code": "chat_invalid_response", "message": "bad"}
    verdict = scenario_verdict(turns)
    assert verdict["technical_completion"] is False
    assert verdict["automated_checks"] == "FAIL"
    assert 5 in verdict["errored_turns"]


def test_expected_refusal_is_a_format_pass():
    refused = {
        2: {
            "status": "refused",
            "text": "В переданных документах нет ответа на этот вопрос. "
            "Переформулируйте вопрос или выберите другой индекс.",
            "grounding_status": "refused",
            "insufficient": True,
            "citation_status": "none",
        }
    }
    turns = _turns(member_overrides=refused)
    assert turns[1]["status"] == "refused"
    assert turns[1]["format_checks"]["status"] == "PASS"
    assert scenario_verdict(turns)["automated_checks"] == "PASS"


def test_bare_refusal_marker_is_a_format_fail():
    # Regression defect 5: a bare service marker must not pass the format checks.
    turns = _turns(member_overrides={
        2: {
            "status": "refused",
            "text": "insufficient",
            "grounding_status": "refused",
            "insufficient": True,
            "citation_status": "none",
        }
    })
    assert turns[1]["format_checks"]["status"] == "FAIL"
    assert "insufficient-context" in turns[1]["format_checks"]["reason"]


def test_refusal_with_verified_citations_is_inconsistent():
    # Consistency (C07): a refusal that also carries verified citations is a
    # self-contradictory turn and must not be a clean format PASS.
    turns = _turns(member_overrides={
        2: {
            "status": "refused",
            "text": "В переданных документах нет ответа на этот вопрос. Переформулируйте вопрос.",
            "grounding_status": "refused",
            "insufficient": True,
            "citation_status": "verified",
        }
    })
    assert turns[1]["format_checks"]["status"] == "PARTIAL"
    assert "verified citations" in turns[1]["format_checks"]["reason"]


def test_citation_failure_is_not_a_refusal():
    # Regression defect 4: documents were passed but the answer could not be
    # confirmed; this is a format failure, not a legitimate refusal.
    turns = _turns(member_overrides={
        3: {
            "status": "citation_failed",
            "grounding_status": "failed",
            "citation_status": "quote_mismatch",
            "insufficient": False,
        }
    })
    assert turns[2]["status"] == "citation_failed"
    assert turns[2]["format_checks"]["status"] == "FAIL"
    assert scenario_verdict(turns)["automated_checks"] == "FAIL"


def test_unknown_chunk_id_is_a_format_fail_but_quote_mismatch_is_partial():
    unknown = _turns(member_overrides={3: {"citation_status": "unknown_chunk_id", "grounding_status": "failed"}})
    assert unknown[2]["format_checks"]["status"] == "FAIL"
    mismatch = _turns(member_overrides={4: {"citation_status": "quote_mismatch", "grounding_status": "partial"}})
    assert mismatch[3]["format_checks"]["status"] == "PARTIAL"
    assert scenario_verdict(mismatch)["automated_checks"] == "PARTIAL"


def test_semantic_partial_or_fail_cannot_yield_overall_substantive_pass():
    # Regression defect 3: scenario A carried per-turn PARTIALs as an overall
    # substantive PASS. Aggregation must follow the actual per-turn verdicts.
    turns = _turns()
    partial = {turn["turn_index"]: "PASS" for turn in turns}
    partial[3] = "PARTIAL"
    assert scenario_verdict(turns, partial)["substantive_acceptance"] == "PARTIAL"

    failed = {turn["turn_index"]: "PASS" for turn in turns}
    failed[4] = "FAIL"
    assert scenario_verdict(turns, failed)["substantive_acceptance"] == "FAIL"

    all_pass = {turn["turn_index"]: "PASS" for turn in turns}
    assert scenario_verdict(turns, all_pass)["substantive_acceptance"] == "PASS"

    missing = {turn["turn_index"]: "PASS" for turn in turns if turn["turn_index"] != 7}
    assert scenario_verdict(turns, missing)["substantive_acceptance"] == "NOT_ASSESSED"


def test_harness_does_not_auto_confirm_constraints_or_support():
    # Regression defect 2: `respected=true` and `source_backed=true` used to be
    # derived from presence alone. They must stay unset until an actual check.
    memory = {
        "goal": {"text": "learn", "status": "confirmed"},
        "constraints": [{"item_id": "c1", "text": "без кода", "status": "active"}],
    }
    turn = _project_turn(_record(1), memory, "learn", {"mandatory_answer": True})
    assert turn["constraints_compliance"][0]["respected"] is None
    assert turn["constraints_compliance"][0]["compliance_check"] == "not_checked"
    assert turn["support"]["source_backed"] is None
    assert turn["support"]["checked"] == "not_checked"
    # The structural observations are kept separately and are provably present.
    assert turn["structural"]["citations_present"] is True
    assert turn["structural"]["sources_present"] is True
    assert turn["structural"]["active_constraints_present"] == 1
    assert turn["support"]["citations_present"] is True


def test_answer_assessment_keeps_the_three_verdicts_separate():
    assessment = _answer_assessment(_record(1))
    assert set(assessment) == {"technical_completion", "format_checks", "semantic", "truncated"}
    assert assessment["technical_completion"]["completed"] is True
    assert assessment["semantic"]["status"] == "NOT_ASSESSED"


def test_run_id_and_prior_artifact_history_are_preserved(tmp_path):
    # Correction C08: the current artifact is bound to a run id while the prior
    # artifact is preserved under history/ with a distinguishable name.
    assert _run_id("a").startswith("d25-a-")
    output = tmp_path / "d25"
    output.mkdir()
    previous = {"schema_version": "d25-scenario-v1", "run_id": "d25-a-old", "created_at": NOW, "answer": "old"}
    (output / "scenario-a.json").write_text(json.dumps(previous), encoding="utf-8")

    history = _archive_previous(output, "a")
    assert len(history) == 1
    archived = Path(history[0])
    assert archived.is_file() and json.loads(archived.read_text(encoding="utf-8")) == previous

    # A second run keeps the previous history and appends the new one.
    (output / "scenario-a.json").write_text(json.dumps({"run_id": "d25-a-second"}), encoding="utf-8")
    history = _archive_previous(output, "a")
    assert len(history) == 2
    names = {Path(item).name for item in history}
    assert any("d25-a-old" in name for name in names)
    assert any("d25-a-second" in name for name in names)



def test_honest_limitation_is_not_a_quote_mismatch_or_semantic_pass():
    record = _record(3, grounding_status="partial")
    record["answer"]["grounding"] = {"status": "partial", "limitation": "The paper does not describe local deployment.",
                                        "inline_unsupported": [], "inline_missing_quote": []}
    turn = _project_turn(record, {}, None, {"mandatory_answer": True})
    assert turn["format_checks"]["status"] == "PASS"
    assert turn["format_checks"]["details"]["limitation"] == "The paper does not describe local deployment."
    assert "mismatch" not in turn["format_checks"]["reason"]
    assert turn["answer"]["grounding_status"] == "partial"
    assert turn["semantic"]["status"] == "NOT_ASSESSED"


def test_missing_inline_quote_is_partial_even_when_other_quotes_are_exact():
    record = _record(3, grounding_status="partial")
    record["answer"]["grounding"] = {"status": "partial", "inline_missing_quote": ["c2"], "inline_unsupported": []}
    turn = _project_turn(record, {}, None, {"mandatory_answer": True})
    assert turn["format_checks"]["status"] == "PARTIAL"
    assert "structured quote" in turn["format_checks"]["reason"]


def test_unsupported_inline_reference_is_a_format_failure_with_exact_other_quotes():
    record = _record(3, grounding_status="partial")
    record["answer"]["grounding"] = {"status": "partial", "inline_missing_quote": [], "inline_unsupported": ["outside"]}
    turn = _project_turn(record, {}, None, {"mandatory_answer": True})
    assert turn["format_checks"]["status"] == "FAIL"
