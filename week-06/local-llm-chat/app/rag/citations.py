"""Citation verification for RAG answers (SPEC R5.2, I8).

The verifier checks that citations in an answer refer to fragments that were
actually passed to the model and that the quoted text appears in the fragment.
It never repairs a fabricated quote and never substitutes a similar fragment.
Meaning is not checked automatically.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

MEANING_CHECK_NOT_PERFORMED = "not_performed"
_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)
# Inline references may be a chunk id or a 1-based fragment index: [1], [b77d...].
INLINE_RE = re.compile(r"\[([^\]\s]+)\]")


def normalize_whitespace(text: str) -> str:
    if not text:
        return ""
    value = unicodedata.normalize("NFC", str(text))
    value = value.replace("\u00a0", " ")
    return _WHITESPACE_RE.sub(" ", value).strip()


@dataclass
class CitationCheck:
    reference: str
    chunk_id: str | None
    label: str | None
    source_exists: bool
    quote_verbatim: bool
    status: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "chunk_id": self.chunk_id,
            "label": self.label,
            "source_exists": self.source_exists,
            "quote_verbatim": self.quote_verbatim,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass
class CitationReport:
    status: str
    reason: str | None
    meaning_check: str = MEANING_CHECK_NOT_PERFORMED
    citations: list[CitationCheck] = field(default_factory=list)
    inline_unsupported: list[str] = field(default_factory=list)
    inline_missing_quote: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "meaning_check": self.meaning_check,
            "citations": [c.to_dict() for c in self.citations],
            "inline_unsupported": list(self.inline_unsupported),
            "inline_missing_quote": list(self.inline_missing_quote),
        }


class CitationVerifier:
    """Verify inline/quoted citations against really passed fragments."""

    def __init__(self, fragments: Sequence[Mapping[str, Any]]) -> None:
        # Index by chunk_id and by 1-based fragment position (the label used in
        # the context block, e.g. ``[1]``).
        self._by_id: dict[str, Mapping[str, Any]] = {}
        self._by_index: dict[str, Mapping[str, Any]] = {}
        for position, fragment in enumerate(fragments, start=1):
            chunk_id = fragment.get("chunk_id")
            if chunk_id is not None:
                self._by_id[str(chunk_id)] = fragment
            self._by_index[str(position)] = fragment

    def _lookup(self, reference: str) -> Mapping[str, Any] | None:
        return self._by_id.get(reference) or self._by_index.get(reference)

    def verify(self, answer_text: str, quoted_citations: Sequence[Mapping[str, Any]] = ()) -> CitationReport:
        checks: list[CitationCheck] = []
        seen: set[str] = set()

        # Extract explicit quote/reference pairs without inventing a quote.
        extracted = [
            {"reference": m.group(2), "quote": m.group(1)}
            for m in re.finditer(r'["“]([^"”]+)["”]\s*\[([^\]\s]+)\]', answer_text or "")
        ]
        quoted_citations = list(quoted_citations) + extracted
        # Structured citations explicitly quote a fragment.
        for citation in quoted_citations:
            reference = str(citation.get("chunk_id") or citation.get("reference") or "")
            if not reference:
                continue
            seen.add(reference)
            checks.append(self._check(reference, str(citation.get("quote") or "")))

        # Inline references in the answer text (e.g. ``[1]``).
        inline_unsupported: list[str] = []
        inline_missing_quote: list[str] = []
        for match in INLINE_RE.finditer(answer_text or ""):
            reference = match.group(1)
            if reference in seen:
                continue
            seen.add(reference)
            check = self._check(reference, "")
            checks.append(check)
            if not check.source_exists:
                inline_unsupported.append(reference)
            elif not check.quote_verbatim:
                inline_missing_quote.append(reference)

        if not checks:
            return CitationReport(status="not_applicable", reason="no_citations")
        if inline_unsupported:
            status, reason = "failed", "unsupported_citation"
        elif all(c.status == "verified" for c in checks):
            status, reason = "verified", None
        elif any(c.status == "verified" for c in checks):
            status, reason = "partial", next(c.status for c in checks if c.status != "verified")
        elif all(c.status == "source_only" for c in checks):
            # Every reference points at a passed fragment; no quote was claimed.
            status, reason = "source_only", "no_quote_provided"
        elif any(c.status == "source_only" for c in checks):
            status, reason = "partial", next(c.status for c in checks if c.status != "source_only")
        else:
            status, reason = "failed", checks[0].status
        return CitationReport(
            status=status,
            reason=reason,
            citations=checks,
            inline_unsupported=inline_unsupported,
            inline_missing_quote=inline_missing_quote,
        )

    def _check(self, reference: str, quote: str) -> CitationCheck:
        fragment = self._lookup(reference)
        if fragment is None:
            return CitationCheck(
                reference=reference,
                chunk_id=None,
                label=None,
                source_exists=False,
                quote_verbatim=False,
                status="unknown_reference",
                reason="reference_not_passed",
            )
        chunk_id = str(fragment.get("chunk_id")) if fragment.get("chunk_id") is not None else None
        label = fragment.get("label") or fragment.get("source")
        normalized_quote = normalize_whitespace(quote)
        if not normalized_quote:
            # A bare inline reference proves the source exists, not a quote.
            return CitationCheck(
                reference=reference,
                chunk_id=chunk_id,
                label=label,
                source_exists=True,
                quote_verbatim=False,
                status="source_only",
                reason="no_quote_provided",
            )
        verbatim = normalized_quote in normalize_whitespace(str(fragment.get("text") or ""))
        return CitationCheck(
            reference=reference,
            chunk_id=chunk_id,
            label=label,
            source_exists=True,
            quote_verbatim=verbatim,
            status="verified" if verbatim else "quote_mismatch",
            reason=None if verbatim else "quote_not_in_fragment",
        )
