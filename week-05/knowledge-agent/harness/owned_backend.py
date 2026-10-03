"""Owned test backend paths; production settings are deliberately unchanged."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Mapping


def _index_and_root(db_path: str | Path) -> tuple[Path, Path]:
    database = Path(db_path)
    if not database.is_absolute():
        raise ValueError("an owned test backend requires an explicit absolute index path")
    database = database.resolve()
    return database, database.parent


def _owned_path(value: str | Path, root: Path) -> Path | None:
    path = Path(value)
    if not path.is_absolute():
        return None
    path = path.resolve()
    return path if path.is_relative_to(root) and path != root else None


def isolated_backend_env(db_path: str | Path, overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """Ignore inherited data paths, preserve only explicitly owned overrides.

    The caller owns the temporary index directory. A configured child path may
    be customized within that directory; an explicit escape is an error before
    a backend process can start. The real application's .env is never loaded.
    """
    database, root = _index_and_root(db_path)
    supplied = dict(overrides or {})
    env = dict(os.environ)
    env.update(supplied)
    env["KNOWLEDGE_SKIP_ENV_FILE"] = "1"
    env["KNOWLEDGE_DB_PATH"] = str(database)
    for key, fallback in (("DIALOGUE_DB_PATH", "conversations.db"), ("CHAT_RUNS_PATH", "chat-runs")):
        path = root / fallback
        if supplied.get(key):
            path = _owned_path(supplied[key], root)
            if path is None:
                raise ValueError(f"{key} must remain inside the owned test directory")
        env[key] = str(path)
    return env


def isolated_backend_settings(settings):
    """Keep in-process D25 test stores beside its explicit owned index.

    Settings have no provenance for inherited environment values. Any path
    outside the owned index directory is therefore replaced by the safe
    sibling; an already owned explicit path is retained.
    """
    database, root = _index_and_root(settings.db_path)
    dialogue = _owned_path(settings.dialogue_db_path, root) or root / "conversations.db"
    runs = _owned_path(settings.chat_runs_path, root) or root / "chat-runs"
    return dataclasses.replace(settings, db_path=str(database),
                               dialogue_db_path=str(dialogue), chat_runs_path=str(runs))
