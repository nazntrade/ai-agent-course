"""D23-C01/C05: deterministic relevance filtering and exclusion reasons."""

from __future__ import annotations

from knowledge_agent.chat.filtering import RelevanceFilter


def candidate(chunk_id: str, score: float, rank: int) -> dict:
    return {"rank": rank, "score": score, "chunk_id": chunk_id, "text": chunk_id, "metadata": {}}


def test_threshold_drops_low_scores_and_records_ids():
    result = RelevanceFilter().apply(
        [candidate("a", 0.9, 1), candidate("b", 0.4, 2), candidate("c", 0.7, 3)],
        threshold=0.6,
        postfilter_top_k=5,
    )
    assert [item["chunk_id"] for item in result.selected] == ["a", "c"]
    assert result.threshold_excluded == ["b"]
    assert result.top_k_excluded == []


def test_postfilter_top_k_truncates_after_sorting():
    result = RelevanceFilter().apply(
        [candidate("a", 0.5, 1), candidate("b", 0.9, 2), candidate("c", 0.7, 3)],
        threshold=0.0,
        postfilter_top_k=2,
    )
    assert [item["chunk_id"] for item in result.selected] == ["b", "c"]
    assert result.top_k_excluded == ["a"]
    assert result.threshold_excluded == []


def test_threshold_zero_keeps_everything_and_one_usually_empties():
    all_kept = RelevanceFilter().apply(
        [candidate("a", 0.1, 1), candidate("b", 0.2, 2)], threshold=0.0, postfilter_top_k=9
    )
    # Ordered by score descending (0.2 before 0.1), not by input order.
    assert [item["chunk_id"] for item in all_kept.selected] == ["b", "a"]
    empty = RelevanceFilter().apply(
        [candidate("a", 0.99, 1)], threshold=1.0, postfilter_top_k=9
    )
    assert empty.selected == []
    assert empty.threshold_excluded == ["a"]


def test_exclusion_categories_never_overlap():
    result = RelevanceFilter().apply(
        [candidate("a", 0.9, 1), candidate("b", 0.8, 2), candidate("c", 0.2, 3)],
        threshold=0.5,
        postfilter_top_k=1,
    )
    assert [item["chunk_id"] for item in result.selected] == ["a"]
    assert result.threshold_excluded == ["c"]
    assert result.top_k_excluded == ["b"]
    flat = result.threshold_excluded + result.top_k_excluded
    assert len(flat) == len(set(flat))
