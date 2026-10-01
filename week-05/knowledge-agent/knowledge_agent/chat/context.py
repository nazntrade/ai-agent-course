"""Deterministic context budget ``heuristic-v1`` (SPEC D22 9).

The estimator is approximate on purpose: D21 ``lexical-v1`` counts lexical units
and is never reused here as if it were a chat tokenizer. Chunks are added whole;
a chunk that does not fit is skipped, never silently truncated.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..domain.errors import ContextOverflow

BUDGET_METHOD = "heuristic-v1"
SAFETY_MARGIN = 64


@dataclass
class ContextPlan:
    passed: list[dict[str, Any]]
    dropped_chunks: int
    prompt_tokens_estimated: int
    max_context_tokens: int
    reserved_output_tokens: int
    budget_method: str = BUDGET_METHOD
    overflow: bool = False


class ContextBudget:
    def __init__(
        self,
        *,
        max_context_tokens: int = 8192,
        reserved_output_tokens: int = 1024,
        chars_per_token: int = 3,
        safety_margin: int = SAFETY_MARGIN,
        model_context_length: int | None = None,
    ) -> None:
        self.max_context_tokens = max(1, int(max_context_tokens))
        self.reserved_output_tokens = max(0, int(reserved_output_tokens))
        self.chars_per_token = max(1, int(chars_per_token))
        self.safety_margin = max(0, int(safety_margin))
        self.model_context_length = model_context_length

    def estimate(self, text: str) -> int:
        if not text:
            return 0
        return math.ceil(len(text.encode("utf-8")) / self.chars_per_token)

    def effective_context_tokens(
        self,
        request_max_context_tokens: int | None = None,
        model_context_length: int | None = None,
    ) -> int:
        # SPEC D22 9.2: effective = min(config, model.context_length, request).
        # Both the model length and the request can only shrink the budget; the
        # cap is passed per call so a shared budget is never mutated (threadpool).
        effective = self.max_context_tokens
        caps = [
            int(cap)
            for cap in (self.model_context_length, model_context_length)
            if cap
        ]
        if caps:
            effective = min(effective, *caps)
        if request_max_context_tokens is not None:
            effective = min(effective, max(1, int(request_max_context_tokens)))
        return effective

    def plan(
        self,
        *,
        mandatory_texts: Sequence[str],
        candidates: Sequence[Mapping[str, Any]],
        request_max_context_tokens: int | None = None,
        model_context_length: int | None = None,
    ) -> ContextPlan:
        effective = self.effective_context_tokens(
            request_max_context_tokens, model_context_length
        )
        prompt_budget = effective - self.reserved_output_tokens - self.safety_margin
        mandatory_tokens = sum(self.estimate(text) for text in mandatory_texts)
        if prompt_budget <= 0 or mandatory_tokens > prompt_budget:
            raise ContextOverflow(
                "The instructions and question do not fit the context budget.",
                details={
                    "budget_method": BUDGET_METHOD,
                    "prompt_budget": prompt_budget,
                    "mandatory_tokens": mandatory_tokens,
                    "max_context_tokens": effective,
                    "reserved_output_tokens": self.reserved_output_tokens,
                },
            )

        passed: list[dict[str, Any]] = []
        used = mandatory_tokens
        dropped = 0
        for candidate in candidates:
            estimated = self.estimate(str(candidate.get("text") or ""))
            if used + estimated > prompt_budget:
                # Keep the whole chunk out and still try smaller later chunks.
                dropped += 1
                continue
            used += estimated
            passed.append(
                {
                    "rank": candidate.get("rank"),
                    "chunk_id": candidate.get("chunk_id"),
                    "estimated_tokens": estimated,
                    "metadata": candidate.get("metadata") or {},
                    # Internal only: needed to build the prompt. The HTTP/record
                    # projection drops it (SPEC D22 6.2 passed shape).
                    "text": str(candidate.get("text") or ""),
                }
            )

        return ContextPlan(
            passed=passed,
            dropped_chunks=dropped,
            prompt_tokens_estimated=used,
            max_context_tokens=effective,
            reserved_output_tokens=self.reserved_output_tokens,
        )
