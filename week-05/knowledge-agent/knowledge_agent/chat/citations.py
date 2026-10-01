"""Citation extraction and validation against really passed chunk ids (SPEC 10).

Only bracketed hexadecimal identifiers (D21 chunk ids are 64 hex chars) are
treated as citations, so ordinary bracketed text is not misreported.
"""

from __future__ import annotations

import re
from typing import Iterable

CITATION_RE = re.compile(r"\[([0-9a-fA-F]{16,64})\]")


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
