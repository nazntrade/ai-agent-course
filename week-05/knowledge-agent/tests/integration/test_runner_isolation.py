"""Owned offline process runners preserve unrelated data and skip env loading."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness import interrupt_check, restart_check


@pytest.mark.parametrize("runner", [restart_check, interrupt_check])
def test_restart_and_interrupt_use_fresh_owned_database(tmp_path, monkeypatch, runner):
    working = tmp_path / "working.db"
    working.write_bytes(b"must not be read or changed")
    monkeypatch.setenv("KNOWLEDGE_DB_PATH", str(working))
    monkeypatch.setenv("KNOWLEDGE_SKIP_ENV_FILE", "0")
    popen = subprocess.Popen
    owned = []
    def start(args, **kwargs):
        env = kwargs["env"]
        assert env["KNOWLEDGE_SKIP_ENV_FILE"] == "1"
        database = Path(env["KNOWLEDGE_DB_PATH"])
        assert database.name == "index.db"
        assert database.parent.name.startswith("knowledge-")
        assert database.parent != tmp_path
        owned.append(database.parent)
        return popen(args, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", start)
    assert runner.main() == 0
    assert working.read_bytes() == b"must not be read or changed"
    assert len(owned) == 1 and not owned[0].exists()
