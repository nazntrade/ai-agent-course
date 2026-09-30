"""Startup preserves ordinary .env loading and supports isolated test processes."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
import uvicorn

from knowledge_agent import __main__ as entrypoint


@pytest.mark.parametrize("skip", [None, "0", "true", "1"])
def test_startup_env_file_requires_explicit_opt_out(tmp_path, monkeypatch, skip):
    synthetic_env = tmp_path / ".env"
    synthetic_env.write_text("KNOWLEDGE_PORT=9123\n", encoding="utf-8")
    monkeypatch.setattr(entrypoint, "MODULE_DIR", tmp_path)
    monkeypatch.delenv("KNOWLEDGE_PORT", raising=False)
    if skip is None:
        monkeypatch.delenv("KNOWLEDGE_SKIP_ENV_FILE", raising=False)
    else:
        monkeypatch.setenv("KNOWLEDGE_SKIP_ENV_FILE", skip)

    store = Mock()
    service = object()
    build = Mock(return_value=(service, store))
    serve = Mock()
    monkeypatch.setattr(entrypoint, "build_service", build)
    monkeypatch.setattr(entrypoint, "create_app", lambda service, **kwargs: service)
    monkeypatch.setattr(uvicorn, "run", serve)
    if skip == "1":
        monkeypatch.setattr(
            entrypoint,
            "apply_env_file",
            Mock(side_effect=AssertionError("isolated startup must not inspect an env file")),
        )

    assert entrypoint.main() == 0
    settings = build.call_args.args[0]
    assert settings.port == (8770 if skip == "1" else 9123)
    assert serve.call_args.kwargs["port"] == settings.port
    store.close.assert_called_once()
