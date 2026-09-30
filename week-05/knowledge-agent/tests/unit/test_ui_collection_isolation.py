"""Execute the actual UI controller with delayed mocked HTTP and a minimal DOM."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Optional UI controller regression needs an existing Node.js runtime")
def test_ui_collection_switch_rejects_delayed_foreign_results(tmp_path):
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [shutil.which("node"), str(root / "tests" / "fixtures" / "ui_collection_isolation.js"),
         str(root / "knowledge_agent" / "ui" / "app.js")],
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "UI_COLLECTION_ISOLATION: PASS" in completed.stdout
