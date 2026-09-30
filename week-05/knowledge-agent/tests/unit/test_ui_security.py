"""Static security gate: local source and server text must never enter HTML sinks."""

from __future__ import annotations

import re
from pathlib import Path


def test_ui_html_assignment_is_limited_to_clearing_containers():
    script = (Path(__file__).resolve().parents[2] / "knowledge_agent" / "ui" / "app.js").read_text(encoding="utf-8")
    assignments = re.findall(r"\.(?:innerHTML|outerHTML)\s*=\s*([^;]*);", script)
    assert assignments and all(value.strip() in {'""', "''"} for value in assignments)
    for sink in ("insertAdjacentHTML", "createContextualFragment", "document.write"):
        assert sink not in script, f"untrusted document text must not reach {sink}"
