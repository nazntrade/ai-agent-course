"""Test launch boundaries never inherit production dialogue or run stores."""

from __future__ import annotations

import dataclasses
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from harness import d25_live
from harness.owned_backend import isolated_backend_env
from knowledge_agent.config import load_settings


MODULE_DIR = Path(__file__).resolve().parents[2]


def _constructor(filename):
    spec = importlib.util.spec_from_file_location("owned_fixture_" + Path(filename).stem,
                                               MODULE_DIR / "tests" / "integration" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("filename", ["test_api_end_to_end.py", "test_chat_end_to_end.py", "test_d25_chat_end_to_end.py"])
@pytest.mark.parametrize("custom_paths", [False, True])
def test_process_constructor_overrides_inherited_work_data_before_popen(tmp_path, monkeypatch, filename, custom_paths):
    working = tmp_path / "working"
    working.mkdir()
    sentinel = working / "conversations.db"
    sentinel.write_bytes(b"unrelated working history")
    runs = working / "chat-runs"
    runs.mkdir()
    receipt = runs / "receipt.json"
    receipt.write_bytes(b"unrelated saved answer")
    owned = tmp_path / "owned"
    owned.mkdir()
    database = owned / "index.db"
    for key, value in (("KNOWLEDGE_DB_PATH", sentinel), ("DIALOGUE_DB_PATH", sentinel), ("CHAT_RUNS_PATH", runs)):
        monkeypatch.setenv(key, str(value))
    monkeypatch.setenv("KNOWLEDGE_SKIP_ENV_FILE", "0")
    module = _constructor(filename)
    process = Mock()
    launched = []

    def start(args, **kwargs):
        env = kwargs["env"]
        assert env["KNOWLEDGE_SKIP_ENV_FILE"] == "1"
        assert Path(env["KNOWLEDGE_DB_PATH"]) == database
        expected_dialogue = owned / ("custom-history.db" if custom_paths else "conversations.db")
        expected_runs = owned / ("custom-runs" if custom_paths else "chat-runs")
        assert Path(env["DIALOGUE_DB_PATH"]) == expected_dialogue
        assert Path(env["CHAT_RUNS_PATH"]) == expected_runs
        launched.append(args)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", start)
    monkeypatch.setattr(module, "free_port", lambda: 19090)
    extra = {"DIALOGUE_DB_PATH": str(owned / "custom-history.db"), "CHAT_RUNS_PATH": str(owned / "custom-runs")} if custom_paths else None
    args = [database, "http://127.0.0.1:19091"]
    if filename != "test_api_end_to_end.py":
        args.append("http://127.0.0.1:19092")
    backend = module.Backend(*args, extra_env=extra)
    assert backend.process is process and len(launched) == 1
    assert sentinel.read_bytes() == b"unrelated working history"
    assert receipt.read_bytes() == b"unrelated saved answer"


@pytest.mark.parametrize("key", ["DIALOGUE_DB_PATH", "CHAT_RUNS_PATH"])
def test_explicit_external_test_override_fails_before_launch(tmp_path, key):
    with pytest.raises(ValueError, match="owned test directory"):
        isolated_backend_env(tmp_path / "owned" / "index.db", {key: str(tmp_path / "working")})


@pytest.mark.parametrize("custom_paths", [False, True])
def test_d25_in_process_backend_isolates_settings_before_any_store_builder(tmp_path, monkeypatch, custom_paths):
    working = tmp_path / "working-history.db"
    working.write_bytes(b"user history must not be opened")
    owned = tmp_path / "owned"
    owned.mkdir()
    settings = load_settings({
        "KNOWLEDGE_DB_PATH": str(owned / "index.db"),
        "DIALOGUE_DB_PATH": str(working), "CHAT_RUNS_PATH": str(tmp_path / "working-runs"),
    })
    if custom_paths:
        settings = dataclasses.replace(settings, dialogue_db_path=str(owned / "custom.db"),
                                       chat_runs_path=str(owned / "custom-runs"))
    observed = []
    store = Mock()
    chat = SimpleNamespace()
    conversation = SimpleNamespace(chat=chat)

    def capture(config):
        assert Path(config.db_path) == owned / "index.db"
        assert Path(config.dialogue_db_path) == owned / ("custom.db" if custom_paths else "conversations.db")
        assert Path(config.chat_runs_path) == owned / ("custom-runs" if custom_paths else "chat-runs")
        observed.append(config)

    def index_builder(config):
        capture(config)
        return object(), store

    def chat_builder(config, service):
        capture(config)
        return chat

    def conversation_builder(config, model):
        capture(config)
        return conversation, store

    monkeypatch.setattr(d25_live, "build_service", index_builder)
    monkeypatch.setattr(d25_live, "build_chat_service", chat_builder)
    monkeypatch.setattr(d25_live, "build_conversation_service", conversation_builder)
    backend = d25_live.D25Backend(settings)
    assert len(observed) == 3 and backend.conversation is conversation
    assert settings.dialogue_db_path == (str(owned / "custom.db") if custom_paths else str(working))
    assert working.read_bytes() == b"user history must not be opened"


def test_d25_backend_rejects_default_working_index_before_opening_store(monkeypatch):
    builder = Mock(side_effect=AssertionError("must not open a production index"))
    monkeypatch.setattr(d25_live, "build_service", builder)
    with pytest.raises(ValueError, match="explicit absolute index"):
        d25_live.D25Backend(load_settings({}))
    builder.assert_not_called()
