"""Local-LLM configuration of the isolated E2E runtime.

The real endpoint, model name and start command belong to the owner's machine,
so they are only ever read from an untracked file (``qa/local.llm.local.json``)
or from ``QA_LOCAL_LLM_*`` environment variables. Nothing here is written to a
tracked file. The committed ``qa/local.llm.example.json`` carries placeholders
only.

When no configuration exists at all, ``lib.discovery`` may find and start the
owner's local launcher and then create the untracked ``qa/local.llm.local.json``
itself; that file is git-ignored and never becomes a tracked artifact.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from lib.paths import EXAMPLE_CONFIG_PATH, LOCAL_CONFIG_PATH, QA_ROOT

LOCAL_BASE_URL_ENV = "QA_LOCAL_LLM_BASE_URL"
LOCAL_MODEL_ENV = "QA_LOCAL_LLM_MODEL"
LOCAL_LAUNCH_COMMAND_ENV = "QA_LOCAL_LLM_LAUNCH_COMMAND"
LOCAL_LAUNCH_CWD_ENV = "QA_LOCAL_LLM_LAUNCH_CWD"
LOCAL_CONFIG_ENV = "QA_LOCAL_LLM_CONFIG"
LOCAL_API_KEY_ENV = "QA_LOCAL_LLM_API_KEY"
LOCAL_READINESS_ENV = "QA_LOCAL_LLM_READINESS_SECONDS"
LOCAL_LIVE_TIMEOUT_ENV = "QA_LOCAL_LLM_TIMEOUT_SECONDS"

LOCAL_ENV_KEYS = (
    LOCAL_BASE_URL_ENV,
    LOCAL_MODEL_ENV,
    LOCAL_LAUNCH_COMMAND_ENV,
    LOCAL_LAUNCH_CWD_ENV,
    LOCAL_API_KEY_ENV,
)

DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:8080/v1"
# Fallback model id used only when neither the configuration nor the running
# endpoint advertises one; it is not a secret and stays in tracked code.
DEFAULT_LOCAL_MODEL = "qwen3.8-27b-local"
DEFAULT_API_KEY = "local-e2e"
DEFAULT_READINESS_SECONDS = 90
# Budget of a live browser scenario. A real 27B local model may need ~72 s just
# for the prefill of a ~1000-token prompt and minutes for generation, so the
# recipe's short default must never bound a live run. This covers prefill plus
# generation plus the application's single corrective retry.
DEFAULT_LIVE_TIMEOUT_SECONDS = 1800

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

# Values that are documentation placeholders, not a real configuration.
PLACEHOLDER_MARKERS = (
    "<",
    ">",
    "your-",
    "your_",
    "example",
    "placeholder",
    "changeme",
)


def is_loopback_url(url) -> bool:
    """Whether ``url`` points at a loopback host.

    Only loopback endpoints are ever used by the runtime: the model traffic must
    stay on the machine.
    """
    try:
        split = urlsplit(str(url or ""))
    except ValueError:
        return False
    return (split.hostname or "") in LOOPBACK_HOSTS


def looks_like_placeholder(value) -> bool:
    """Whether a configured value is still documentation text."""
    text = str(value or "").strip().lower()
    if not text:
        return False
    if text.startswith("http://127.0.0.1") or text.startswith("http://localhost"):
        return False
    return any(marker in text for marker in PLACEHOLDER_MARKERS)


@dataclass(frozen=True)
class LocalLlmConfig:
    """Resolved local-LLM configuration with its provenance."""

    base_url: str = DEFAULT_LOCAL_BASE_URL
    model: str = ""
    launch_command: str = ""
    launch_cwd: str = ""
    api_key: str = DEFAULT_API_KEY
    readiness_timeout_seconds: int = DEFAULT_READINESS_SECONDS
    live_timeout_seconds: int = DEFAULT_LIVE_TIMEOUT_SECONDS
    config_path: str | None = None
    source: str = "default"
    has_local_config: bool = False
    warnings: tuple = ()

    @property
    def is_loopback(self) -> bool:
        """Whether the endpoint stays on the loopback interface."""
        return is_loopback_url(self.base_url)

    @property
    def can_launch(self) -> bool:
        """Whether a start command is configured for the local server."""
        return bool(self.launch_command.strip())


def resolve_config_path(env=None, config_path=None) -> Path | None:
    """Return the untracked local configuration path, or ``None``."""
    environment = os.environ if env is None else env
    if config_path is not None:
        return Path(config_path)
    configured = environment.get(LOCAL_CONFIG_ENV)
    if configured:
        return Path(configured)
    candidate = QA_ROOT / LOCAL_CONFIG_PATH.name
    return candidate if candidate.exists() else None


def _read_config_file(path) -> dict:
    if path is None or not Path(path).exists():
        return {}
    try:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _clean(value, warnings, label) -> str:
    text = str(value or "").strip()
    if looks_like_placeholder(text):
        warnings.append(f"{label} still looks like a placeholder and is ignored")
        return ""
    return text


def _to_int(value, default, warnings, label) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        warnings.append(f"{label} is not an integer and the default is used")
        return default
    return parsed if parsed > 0 else default


def load_config(env=None, config_path=None) -> LocalLlmConfig:
    """Load the local-LLM configuration: environment over file over defaults.

    ``has_local_config`` is true only when the owner actually provided something
    (an untracked file or a ``QA_LOCAL_LLM_*`` variable). Without it, ``LOCAL``
    mode refuses to run and ``AUTO`` falls back to the loopback mock.
    """
    environment = dict(os.environ) if env is None else dict(env)
    path = resolve_config_path(environment, config_path)
    file_data = _read_config_file(path)
    warnings: list = []

    config_file_present = path is not None and Path(path).exists()

    base_url = (
        environment.get(LOCAL_BASE_URL_ENV)
        or file_data.get("base_url")
        or DEFAULT_LOCAL_BASE_URL
    )
    model = _clean(
        environment.get(LOCAL_MODEL_ENV) or file_data.get("model"),
        warnings,
        "the local model name",
    )
    launch_command = _clean(
        environment.get(LOCAL_LAUNCH_COMMAND_ENV) or file_data.get("launch_command"),
        warnings,
        "the launch command",
    )
    launch_cwd = str(
        environment.get(LOCAL_LAUNCH_CWD_ENV) or file_data.get("launch_cwd") or ""
    ).strip()
    api_key = str(
        environment.get(LOCAL_API_KEY_ENV)
        or file_data.get("api_key")
        or DEFAULT_API_KEY
    )
    readiness = _to_int(
        environment.get(LOCAL_READINESS_ENV)
        or file_data.get("readiness_timeout_seconds"),
        DEFAULT_READINESS_SECONDS,
        warnings,
        "the readiness timeout",
    )
    live_timeout = _to_int(
        environment.get(LOCAL_LIVE_TIMEOUT_ENV)
        or file_data.get("live_timeout_seconds"),
        DEFAULT_LIVE_TIMEOUT_SECONDS,
        warnings,
        "the live scenario timeout",
    )

    base_url = str(base_url).strip() or DEFAULT_LOCAL_BASE_URL
    if looks_like_placeholder(base_url):
        warnings.append("the local endpoint still looks like a placeholder and is ignored")
        base_url = DEFAULT_LOCAL_BASE_URL

    has_env_config = any(environment.get(key) for key in LOCAL_ENV_KEYS)
    if config_file_present or has_env_config:
        source = "env" if has_env_config else "file"
        has_local_config = True
    else:
        source = "default"
        has_local_config = False

    return LocalLlmConfig(
        base_url=base_url,
        model=model,
        launch_command=launch_command,
        launch_cwd=launch_cwd,
        api_key=api_key,
        readiness_timeout_seconds=readiness,
        live_timeout_seconds=live_timeout,
        config_path=str(path) if path is not None else None,
        source=source,
        has_local_config=has_local_config,
        warnings=tuple(warnings),
    )


def load_example_config() -> dict:
    """Read the committed placeholder example (documentation/tests only)."""
    return _read_config_file(EXAMPLE_CONFIG_PATH)
