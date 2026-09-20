"""Filesystem layout of the isolated local E2E runtime.

The module resolves every path from its own location, so the runner works from
any working directory and never depends on absolute paths of a previous project.
The application under test lives under ``week-03/memory-state-agent``; the runner
only ever writes inside ``qa/``.
"""

from __future__ import annotations

from pathlib import Path

QA_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = QA_ROOT.parent

APP_DIR = REPO_ROOT / "week-03" / "memory-state-agent"
APP_ENTRY = APP_DIR / "app.py"
APP_DB_PATH = APP_DIR / "data" / "chat_history.db"
OWNER_ENV_PATH = APP_DIR / ".env"

RUNS_DIR = QA_ROOT / ".runs"
# Cross-invocation live-session state. It lives inside the git-ignored run
# directory, so a started local model can be joined by later runs.
SESSION_STATE_PATH = RUNS_DIR / "live_session.json"
EXAMPLE_CONFIG_PATH = QA_ROOT / "local.llm.example.json"
# The real local configuration is untracked (``qa/*.local.json`` in .gitignore).
LOCAL_CONFIG_NAME = "local.llm.local.json"
LOCAL_CONFIG_PATH = QA_ROOT / LOCAL_CONFIG_NAME
# Repository-relative report label of the untracked config: a constant, never
# the machine-specific absolute path.
LOCAL_CONFIG_RELATIVE = f"qa/{LOCAL_CONFIG_NAME}"

DEFAULT_APP_PORT = 8599
