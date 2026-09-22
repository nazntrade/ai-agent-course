"""The single bridge to the reusable ``qa/lib`` helpers.

``qa/`` is an existing, working runtime: its configuration loading, launcher
discovery, local-LLM lifecycle, live session handling, browser launcher and port
helpers are reused as-is instead of being copied. This module only puts the
repository root and ``qa/`` on ``sys.path`` and re-exports the modules, so no
``qa/`` file has to change.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = PROJECT_DIR.parent.parent
QA_DIR = REPO_ROOT / "qa"


def ensure_paths() -> tuple:
    """Put the project and the ``qa`` runtime on ``sys.path`` (idempotent).

    The project root is placed *before* ``qa/`` on purpose: ``qa/`` contains a
    ``tests`` package that would otherwise shadow this project's own ``tests``
    package in a fresh process (``ModuleNotFoundError: tests.support``).
    """
    for entry in (str(QA_DIR), str(PROJECT_DIR)):
        while entry in sys.path:
            sys.path.remove(entry)
    sys.path.insert(0, str(QA_DIR))
    sys.path.insert(0, str(PROJECT_DIR))
    return PROJECT_DIR, QA_DIR


ensure_paths()

from lib import browser as qa_browser  # noqa: E402
from lib import config as qa_config  # noqa: E402
from lib import discovery as qa_discovery  # noqa: E402
from lib import live_session as qa_live_session  # noqa: E402
from lib import local_llm as qa_local_llm  # noqa: E402
from lib.app_process import port_is_free as qa_port_is_free  # noqa: E402

__all__ = [
    "PROJECT_DIR",
    "REPO_ROOT",
    "QA_DIR",
    "ensure_paths",
    "qa_browser",
    "qa_config",
    "qa_discovery",
    "qa_live_session",
    "qa_local_llm",
    "qa_port_is_free",
]
