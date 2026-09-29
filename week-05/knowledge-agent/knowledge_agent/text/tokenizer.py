"""Deterministic offline tokenizer v1 (SPEC 8.4)."""

from __future__ import annotations

import re

from ..domain.contracts import Tokenizer

# Unicode word characters or a single non-space, non-word symbol.
TOKEN_PATTERN = r"\w+|[^\w\s]"
_TOKEN_RE = re.compile(TOKEN_PATTERN, re.UNICODE)


class LexicalTokenizer(Tokenizer):
    """Regex tokenizer: ``\\w+|[^\\w\\s]`` over Unicode text."""

    name = "lexical-v1"
    unit = "token"

    def tokenize(self, text: str) -> list[str]:
        if not text:
            return []
        return _TOKEN_RE.findall(text)

    def tokenize_spans(self, text: str) -> list[tuple[str, int, int]]:
        if not text:
            return []
        return [(m.group(0), m.start(), m.end()) for m in _TOKEN_RE.finditer(text)]


def count_tokens(text: str) -> int:
    return len(_TOKEN_RE.findall(text or ""))
