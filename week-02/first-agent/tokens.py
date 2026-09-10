"""Deterministic token-count estimation using only the standard library.

The estimate is a cheap heuristic, not a tokenizer: it is stable for the same
input and useful for rough context-limit checks and UI hints, never for
billing. Callers always render the value with the ``≈`` prefix.
"""

from __future__ import annotations

import math


def estimate_tokens(text: str) -> int:
    """Return a rough, deterministic token count for ``text``.

    Empty input yields 0. Otherwise the result is at least 1 and is computed
    as ``ceil(cyrillic / 2 + latin / 4 + other / 3)``: Cyrillic characters
    weigh more than Latin ones, and every other character (spaces, digits,
    punctuation, symbols) is counted with an intermediate weight.
    """
    if not text:
        return 0

    cyrillic = latin = other = 0
    for char in text:
        code = ord(char)
        if 0x0400 <= code <= 0x04FF:  # Cyrillic block
            cyrillic += 1
        elif char.isascii() and char.isalpha():  # Latin letters only
            latin += 1
        else:
            other += 1

    return max(1, math.ceil(cyrillic / 2 + latin / 4 + other / 3))
