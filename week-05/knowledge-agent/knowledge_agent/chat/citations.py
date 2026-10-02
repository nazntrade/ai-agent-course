"""Citation extraction and grounded-RAG verification (SPEC D22 10, D24 7).

Two independent concerns live here:

* ``extract_citations`` — the D22 projection: bracketed hexadecimal identifiers
  in the provider text partitioned into ``valid``/``unsupported``. It only checks
  that the id was passed to the model; it proves neither a quote nor meaning.
* ``GroundingVerifier`` / ``normalize_whitespace`` / ``parse_grounded_response`` —
  the D24 formal layer over really passed chunks: ``source_exists`` (the id is in
  ``passed``) and ``quote_verbatim`` (the normalized quote is a substring of the
  normalized chunk text). Meaning is deliberately NOT checked automatically:
  ``meaning_supported`` stays ``None`` and ``meaning_check`` is ``not_performed``.

The verifier only reports; it never substitutes a similar fragment for a
fabricated quote and never repairs a malformed provider answer.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence

from ..domain.contracts import (
    MEANING_CHECK_NOT_PERFORMED,
    Citation,
    GroundedAnswer,
    GroundingResult,
)
from ..domain.errors import ChatInvalidResponse

CITATION_RE = re.compile(r"\[([0-9a-fA-F]{16,64})\]")
_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)
_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)

GROUNDED_JSON_FORMAT = "grounded_json"


def extract_citations(text: str, passed_ids: Iterable[str]) -> dict[str, list[str]]:
    """Partition cited ids into ``valid`` (in ``passed``) and ``unsupported``."""

    allowed = set(passed_ids)
    valid: list[str] = []
    unsupported: list[str] = []
    seen: set[str] = set()
    for match in CITATION_RE.finditer(text or ""):
        chunk_id = match.group(1)
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        if chunk_id in allowed:
            valid.append(chunk_id)
        else:
            unsupported.append(chunk_id)
    return {"valid": valid, "unsupported": unsupported}


def normalize_whitespace(text: str) -> str:
    """NFC + collapse every Unicode whitespace run to one ASCII space + trim.

    This is the only normalization applied before verbatim comparison; it is
    case-sensitive and performs no punctuation/lemma/fuzzy matching (SPEC D24 7.2).
    """

    if not text:
        return ""
    value = unicodedata.normalize("NFC", str(text))
    value = value.replace("\u00a0", " ")
    value = _WHITESPACE_RE.sub(" ", value)
    return value.strip()


def _format_error(detail: str) -> ChatInvalidResponse:
    return ChatInvalidResponse(
        f"The grounded answer is not valid JSON ({detail}).",
        details={"format": GROUNDED_JSON_FORMAT},
    )


def _extract_single_object(text: str) -> dict[str, Any] | None:
    """Locate exactly one top-level JSON object in surrounding prose.

    Some real providers wrap the required object in a sentence. This never
    rewrites the JSON: it only accepts one well-formed top-level object and
    returns ``None`` (a format error) when there is none or more than one.
    """

    decoder = json.JSONDecoder()
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        if isinstance(value, dict):
            candidates.append((index, index + end, value))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[1] - item[0], reverse=True)
    outer = candidates[0]
    for other in candidates[1:]:
        if other[1] <= outer[0] or other[0] >= outer[1]:
            return None
    return outer[2]


def parse_grounded_response(text: str) -> GroundedAnswer:
    """Strictly parse the grounded-JSON provider answer; never repair it.

    Any deviation (non-JSON, missing/empty ``answer``, ``citations`` not a list,
    wrong field types) raises a typed ``chat_invalid_response`` with
    ``details.format="grounded_json"``.
    """

    raw = text or ""
    if not raw.strip():
        raise _format_error("empty provider text")
    body = raw.strip()
    fences = _FENCE_RE.findall(body)
    if len(fences) > 1:
        raise _format_error("more than one fenced block")
    if fences:
        body = fences[0]
    try:
        payload = json.loads(body)
    except ValueError:
        # Real providers may prefix/suffix the object with prose; accept exactly
        # one well-formed top-level object and reject anything else.
        payload = _extract_single_object(body)
        if payload is None:
            raise _format_error("not parseable") from None
    if not isinstance(payload, dict):
        raise _format_error("the top-level value is not an object")

    answer = payload.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise _format_error("missing or empty answer")
    citations = payload.get("citations")
    if not isinstance(citations, list):
        raise _format_error("citations is not a list")
    parsed: list[dict[str, Any]] = []
    for item in citations:
        if not isinstance(item, dict):
            raise _format_error("a citation is not an object")
        chunk_id = item.get("chunk_id")
        quote = item.get("quote")
        translation = item.get("translation")
        if not isinstance(chunk_id, str) or not chunk_id.strip():
            raise _format_error("a citation chunk_id is missing")
        if not isinstance(quote, str):
            raise _format_error("a citation quote is missing")
        if translation is not None and not isinstance(translation, str):
            raise _format_error("a citation translation is not a string")
        parsed.append(
            {
                "chunk_id": chunk_id,
                "quote": quote,
                "translation": translation,
            }
        )

    insufficient = payload.get("insufficient", False)
    if not isinstance(insufficient, bool):
        raise _format_error("insufficient is not a boolean")
    limitation = payload.get("limitation")
    if limitation is not None and not isinstance(limitation, str):
        raise _format_error("limitation is not a string or null")

    return GroundedAnswer(
        answer=answer,
        citations=parsed,
        insufficient=insufficient,
        limitation=limitation,
    )


class GroundingVerifier:
    """Formal verification of parsed citations against really passed chunks."""

    def __init__(self, passed_chunks: Sequence[Mapping[str, Any]]) -> None:
        self._chunks: dict[str, Mapping[str, Any]] = {}
        for chunk in passed_chunks:
            chunk_id = chunk.get("chunk_id")
            if chunk_id is not None:
                self._chunks[str(chunk_id)] = chunk

    def verify(
        self, grounded: GroundedAnswer, *, threshold: float | None = None
    ) -> GroundingResult:
        citations = [self._verify_citation(item) for item in grounded.citations]

        # D24 defect fix: an inline ``[chunk_id]`` in the answer text only holds
        # if it was passed to the model AND has a verified structured citation.
        # A bare inline reference alone never proves grounding.
        inline = extract_citations(grounded.answer, self._chunks.keys())
        verified_ids = {item.chunk_id for item in citations if item.status == "verified"}
        inline_unsupported = list(inline["unsupported"])
        inline_missing_quote = [
            chunk_id for chunk_id in inline["valid"] if chunk_id not in verified_ids
        ]
        if inline_unsupported:
            inline_reason: str | None = "unsupported_citation"
        elif inline_missing_quote:
            inline_reason = "missing_quote"
        else:
            inline_reason = None

        if grounded.insufficient:
            return GroundingResult(
                status="refused",
                reason="model_insufficient",
                threshold=threshold,
                limitation=grounded.limitation,
                citations=citations,
                refusal={
                    "reason": "model_insufficient",
                    "message": _REFUSAL_MESSAGE,
                    "threshold": threshold,
                },
                inline_unsupported=inline_unsupported,
                inline_missing_quote=inline_missing_quote,
            )

        if not citations:
            # A non-empty answer with no structured citations is failed and
            # never partial: a limitation (including ``""``) cannot upgrade it.
            status, reason = "failed", "no_citations"
        else:
            verified = [item for item in citations if item.status == "verified"]
            failed = [item for item in citations if item.status != "verified"]
            limitation = (grounded.limitation or "").strip()
            if inline_reason is not None:
                status = "partial" if verified else "failed"
                reason = inline_reason
            elif limitation and verified:
                # ``partial`` from a limitation is only honest with a verified
                # citation to support the (partial) answer.
                status = "partial"
                reason = failed[0].status if failed else None
            elif not failed:
                status, reason = "verified", None
            elif verified:
                status, reason = "partial", failed[0].status
            else:
                status, reason = "failed", failed[0].status

        return GroundingResult(
            status=status,
            reason=reason,
            threshold=threshold,
            meaning_check=MEANING_CHECK_NOT_PERFORMED,
            limitation=grounded.limitation,
            citations=citations,
            refusal=None,
            inline_unsupported=inline_unsupported,
            inline_missing_quote=inline_missing_quote,
        )

    def _verify_citation(self, item: Mapping[str, Any]) -> Citation:
        chunk_id = str(item.get("chunk_id") or "")
        quote = str(item.get("quote") or "")
        translation = item.get("translation")
        chunk = self._chunks.get(chunk_id)
        if chunk is None:
            return Citation(
                chunk_id=chunk_id,
                quote=quote,
                translation=translation,
                is_translation=bool(translation),
                source_exists=False,
                quote_verbatim=False,
                meaning_supported=None,
                status="unknown_chunk_id",
                reason="chunk_id_not_passed",
            )
        metadata = chunk.get("metadata") or {}
        text = str(chunk.get("text") or "")
        normalized_quote = normalize_whitespace(quote)
        verbatim = bool(normalized_quote) and normalized_quote in normalize_whitespace(text)
        return Citation(
            chunk_id=chunk_id,
            source=metadata.get("source_label"),
            section=metadata.get("section_path"),
            page_start=metadata.get("page_start"),
            page_end=metadata.get("page_end"),
            quote=quote,
            translation=translation,
            is_translation=bool(translation),
            source_exists=True,
            quote_verbatim=verbatim,
            meaning_supported=None,
            status="verified" if verbatim else "quote_mismatch",
            reason=None if verbatim else "quote_not_in_chunk",
        )


_REFUSAL_MESSAGE = (
    "The answer is not available in the provided documents. "
    "Rephrase the question, lower the relevance threshold, or choose another index."
)
