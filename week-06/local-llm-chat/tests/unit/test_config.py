"""D26-01: own provider configuration is never overridden by AI_TEST_MODEL_*."""

from __future__ import annotations

from app.config import load_settings


def test_ai_test_model_variables_do_not_change_app_settings():
    env = {
        # The test-runner profile points the "chat model" elsewhere.
        "AI_TEST_MODEL_KIND": "remote",
        "AI_TEST_MODEL_BASE_URL": "https://runner.example/v1",
        "AI_TEST_MODEL_NAME": "runner-model",
        "AI_TEST_MODEL_API_KEY": "runner-key",
        # Own application configuration.
        "GEMMA_GGUF_PATH": r"D:\models\own-gemma.gguf",
        "GEMMA_MODEL_ID": "own-gemma.gguf",
        "GEMMA_PORT": "9911",
        "DEEPSEEK_BASE_URL": "https://own.deepseek/v1",
        "DEEPSEEK_MODEL_ID": "own-deepseek",
    }
    settings = load_settings(env)
    assert settings.gemma_gguf_path == r"D:\models\own-gemma.gguf"
    assert settings.gemma_model_id == "own-gemma.gguf"
    assert settings.gemma_port == 9911
    assert settings.deepseek_base_url == "https://own.deepseek/v1"
    assert settings.deepseek_model_id == "own-deepseek"
    # The runner profile must not leak into the app's own provider settings.
    assert "runner.example" not in settings.deepseek_base_url
    assert settings.gemma_model_id != "runner-model"


def test_defaults_are_used_when_own_env_is_absent():
    settings = load_settings({})
    assert settings.gemma_port == 8791
    assert settings.port == 8790
    assert settings.gemma_model_id.endswith(".gguf")
    # DeepSeek key is empty by default: missing config is reported, never invented.
    assert "DEEPSEEK_API_KEY" in settings.missing_network_config()


def test_missing_network_config_lists_names_without_values():
    settings = load_settings({"DEEPSEEK_MODEL_ID": "deepseek-chat"})
    missing = settings.missing_network_config()
    assert "DEEPSEEK_API_KEY" in missing
    assert all("key" not in value.lower() or value.isupper() for value in missing)


def test_private_env_loading_accepts_utf8_bom_and_preserves_explicit_environment(tmp_path, monkeypatch):
    from app.config import apply_env_file
    fake_env = tmp_path / "example.env"
    fake_env.write_text("DEEPSEEK_API_KEY=fake-test-key\nDEEPSEEK_MODEL_ID=file-model\n", encoding="utf-8-sig")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_MODEL_ID", "explicit-model")
    apply_env_file(fake_env)
    settings = load_settings()
    assert settings.missing_network_config() == []
    assert settings.deepseek_model_id == "explicit-model"
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
