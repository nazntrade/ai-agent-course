"""D24-19: offline D24 projections and the eight edge-case fixtures."""

from __future__ import annotations

from harness import d24_grounding

REQUIRED_CASE_IDS = {
    "unknown_chunk_id",
    "fabricated_quote_real_id",
    "answer_contradicts_real_quote",
    "high_score_no_fact",
    "weak_context",
    "instruction_inside_document",
    "empty_or_truncated_generation",
    "provider_error",
}


def _record():
    chunk_id = "a" * 64
    return {
        "run_id": "r1",
        "answer": {
            "text": "answer",
            "finish_reason": "stop",
            "truncated": False,
            "insufficient_sources": False,
            "grounding": {
                "status": "verified",
                "reason": None,
                "threshold": 0.45,
                "meaning_check": "not_performed",
                "limitation": None,
                "citations": [
                    {
                        "chunk_id": chunk_id,
                        "source": "agents.md",
                        "section": "S",
                        "quote": "q",
                        "is_translation": False,
                        "source_exists": True,
                        "quote_verbatim": True,
                        "meaning_supported": None,
                        "status": "verified",
                        "reason": None,
                    }
                ],
                "refusal": None,
            },
        },
        "retrieval": {
            "passed": [
                {
                    "rank": 1,
                    "chunk_id": chunk_id,
                    "estimated_tokens": 3,
                    "metadata": {"source_label": "agents.md", "section_path": "S", "page_start": 1, "page_end": 2},
                }
            ],
            "found": [{"chunk_id": chunk_id, "score": 0.61}],
        },
        "latency_ms": {"retrieval": 1.0, "context": 2.0, "chat": 3.0, "total": 6.0},
        "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        "errors": [],
    }


def test_project_result_flattens_the_record():
    result = d24_grounding.project_result(
        _record(), question_id="Q1", question="q", answerable=True, index_version_id="idx"
    )
    assert result["index_version_id"] == "idx"
    assert result["grounding_status"] == "verified"
    assert result["verification"]["meaning_supported"] is None
    assert result["verification"]["meaning_check"] == "not_performed"
    assert result["passed_chunk_ids"] == ["a" * 64]
    assert result["sources"][0]["source"] == "agents.md"
    assert result["sources"][0]["score"] == 0.61
    assert result["citations"][0]["status"] == "verified"


def test_summarize_counts_statuses():
    summary = d24_grounding.summarize(
        [
            {"grounding_status": "verified", "truncated": False, "insufficient_sources": False, "answerable": True, "refusal": None},
            {"grounding_status": "refused", "truncated": False, "insufficient_sources": True, "answerable": False, "refusal": {"reason": "model_insufficient"}},
        ]
    )
    assert summary["results_total"] == 2
    assert summary["verified"] == 1
    assert summary["refused"] == 1


def test_offline_observations_cover_all_eight_cases_and_pass():
    observations = d24_grounding.offline_observations()
    assert set(observations) == REQUIRED_CASE_IDS
    for case_id, value in observations.items():
        assert value.get("observed"), case_id
        assert value.get("status") == "PASS", case_id


def test_build_edge_cases_has_eight_cases():
    artifact = d24_grounding.build_edge_cases(
        d24_grounding.offline_observations(), index_version_id="idx"
    )
    assert artifact["schema_version"] == "d24-edge-cases-v1"
    assert len(artifact["cases"]) == 8
    assert {case["case_id"] for case in artifact["cases"]} == REQUIRED_CASE_IDS
    assert all(case["status"] == "PASS" for case in artifact["cases"])
    assert all(case["observed"] for case in artifact["cases"])


def test_merge_live_observations_appends_the_live_note():
    observations = d24_grounding.offline_observations()
    merged = d24_grounding.merge_live_observations(
        observations,
        [
            {
                "answerable": False,
                "grounding_status": "refused",
                "grounding_reason": "model_insufficient",
                "insufficient_sources": True,
            }
        ],
    )
    assert "LIVE(Q10)" in merged["weak_context"]["observed"]
    assert "LIVE(Q10)" in merged["high_score_no_fact"]["observed"]
