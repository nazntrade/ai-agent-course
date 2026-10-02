"""RelevanceFilter: deterministic cosine-threshold filtering (SPEC D23 7.1).

The filter is a pure function over candidates already returned by
``KnowledgeService.search``. It never touches the network, the database or
HTTP, and it never calls the embedding provider. Candidates are ordered by
score (descending) with the original rank as a stable tie-breaker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass
class FilterResult:
    selected: list[dict[str, Any]]
    threshold_excluded: list[str] = field(default_factory=list)
    top_k_excluded: list[str] = field(default_factory=list)


def _score(candidate: Mapping[str, Any]) -> float:
    value = candidate.get("score")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _rank(candidate: Mapping[str, Any]) -> int:
    value = candidate.get("rank")
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class RelevanceFilter:
    """Threshold + post-filter top-K selection with typed exclusion reasons."""

    def apply(
        self,
        candidates: Sequence[Mapping[str, Any]],
        *,
        threshold: float,
        postfilter_top_k: int,
    ) -> FilterResult:
        threshold = max(0.0, float(threshold))
        limit = max(0, int(postfilter_top_k))

        passing: list[dict[str, Any]] = []
        threshold_excluded: list[str] = []
        for candidate in candidates:
            item = dict(candidate)
            if threshold > 0.0 and _score(item) < threshold:
                chunk_id = item.get("chunk_id")
                if chunk_id is not None:
                    threshold_excluded.append(str(chunk_id))
                continue
            passing.append(item)

        ordered = sorted(passing, key=lambda item: (-_score(item), _rank(item)))
        selected = ordered[:limit]
        top_k_excluded = [
            str(item.get("chunk_id"))
            for item in ordered[limit:]
            if item.get("chunk_id") is not None
        ]
        return FilterResult(
            selected=selected,
            threshold_excluded=threshold_excluded,
            top_k_excluded=top_k_excluded,
        )
