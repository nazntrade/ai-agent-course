"""D26-14 support: report llama-server.exe processes owned by this project.

Read-only: it lists processes whose executable path is the app's configured
runtime. It never stops any process. A leftover process is reported as a
finding, not silently killed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent.parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from app.config import load_settings  # noqa: E402


def emit(status: str, message: str) -> None:
    print(f"{status}: {message}")


def main() -> int:
    settings = load_settings(os.environ)
    runtime = Path(settings.gemma_runtime_path)
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" | "
        "Select-Object ProcessId,ExecutablePath | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        emit("D26_PROCESS_CHECK", "UNKNOWN (could not enumerate processes)")
        return 1
    raw = (completed.stdout or "").strip()
    if not raw:
        emit("D26_PROCESS_CHECK", "PASS (no llama-server.exe processes running)")
        return 0
    try:
        parsed = json.loads(raw)
    except ValueError:
        emit("D26_PROCESS_CHECK", "UNKNOWN (unparseable process list)")
        return 1
    entries = parsed if isinstance(parsed, list) else [parsed]
    owned = [
        {"pid": e.get("ProcessId"), "path": e.get("ExecutablePath")}
        for e in entries
        if str(e.get("ExecutablePath", "")).lower() == str(runtime).lower()
    ]
    if owned:
        emit("D26_PROCESS_CHECK", "FAIL (leftover owned llama-server processes: " + json.dumps(owned) + ")")
        return 1
    emit("D26_PROCESS_CHECK", f"PASS (no owned llama-server.exe processes; runtime={runtime.name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
