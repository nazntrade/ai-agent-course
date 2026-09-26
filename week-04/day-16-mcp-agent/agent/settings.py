"""Configuration of the agent, resolved from the environment and ``.env``.

All values have safe defaults that point at loopback endpoints, so the project
starts on a fresh machine without a ``.env`` file. The model API key is read
from the environment variable named by ``AGENT_MODEL_API_KEY_ENV`` and is never
written to a log, a trace, a report or ``repr()``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SERVICE_NAME = "day-16-mcp-agent"
SERVICE_VERSION = "1.0.0"

DEFAULT_MODEL_BASE_URL = "http://127.0.0.1:8080/v1"
DEFAULT_MODEL_NAME = "qwen3.8-27b-local"
DEFAULT_MODEL_API_KEY_ENV = "LOCAL_LLM_API_KEY"
DEFAULT_MODEL_TIMEOUT_SECONDS = 120.0

DEFAULT_MCP_SERVER_URL = "http://127.0.0.1:8765/mcp"
DEFAULT_MCP_SERVER_HOST = "127.0.0.1"
DEFAULT_MCP_SERVER_PORT = 8765
DEFAULT_MCP_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_MCP_CALL_TIMEOUT_SECONDS = 30.0

DEFAULT_BACKEND_HOST = "127.0.0.1"
DEFAULT_BACKEND_PORT = 8600
DEFAULT_TRACE_PATH = "logs/trace.jsonl"
DEFAULT_LOG_LEVEL = "INFO"

DEFAULT_DB_FILENAME = "day18.sqlite3"
DEFAULT_CHAT_CONTEXT_MESSAGES = 20
CHAT_CONTEXT_MIN_MESSAGES = 2
CHAT_CONTEXT_MAX_MESSAGES = 100

# A multi-step tool composition needs more than one round: three dependent calls
# plus the final answer, with one spare round in case a round carries several
# parallel calls.
DEFAULT_MAX_TOOL_ROUNDS = 5
MAX_TOOL_ROUNDS_MIN = 2
MAX_TOOL_ROUNDS_MAX = 10


def load_dotenv_if_present(env_path=None) -> bool:
    """Load ``.env`` when it exists; a missing file is not an error."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - python-dotenv is a dependency
        return False
    path = Path(env_path) if env_path is not None else PROJECT_ROOT / ".env"
    if not path.exists():
        return False
    load_dotenv(path, override=False)
    return True


def _clean(value, default: str) -> str:
    text = str(value or "").strip()
    return text or default


def _to_int(value, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if 1 <= parsed <= 65535 else default


def _to_float(value, default: float) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _to_int_clamped(value, default: int, low: int, high: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return min(max(parsed, low), high)


def resolve_db_path(environment) -> Path:
    """Resolve ``AGENT_DB_PATH``, defaulting to ``data/day18.sqlite3``."""
    raw = str(environment.get("AGENT_DB_PATH") or "").strip()
    path = Path(raw) if raw else PROJECT_ROOT / "data" / DEFAULT_DB_FILENAME
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


@dataclass(frozen=True)
class Settings:
    """The resolved runtime configuration.

    ``api_key`` is excluded from ``repr()`` so a debugging print can never leak
    it. ``warnings`` records values that were ignored as unusable.
    """

    model_base_url: str = DEFAULT_MODEL_BASE_URL
    model_name: str = DEFAULT_MODEL_NAME
    model_api_key_env: str = DEFAULT_MODEL_API_KEY_ENV
    model_timeout_seconds: float = DEFAULT_MODEL_TIMEOUT_SECONDS

    mcp_server_url: str = DEFAULT_MCP_SERVER_URL
    mcp_server_host: str = DEFAULT_MCP_SERVER_HOST
    mcp_server_port: int = DEFAULT_MCP_SERVER_PORT
    mcp_connect_timeout_seconds: float = DEFAULT_MCP_CONNECT_TIMEOUT_SECONDS
    mcp_call_timeout_seconds: float = DEFAULT_MCP_CALL_TIMEOUT_SECONDS

    backend_host: str = DEFAULT_BACKEND_HOST
    backend_port: int = DEFAULT_BACKEND_PORT
    public_backend_url: str = ""
    public_frontend_url: str = ""

    trace_path: Path | None = None
    log_level: str = DEFAULT_LOG_LEVEL

    db_path: Path = field(
        default_factory=lambda: PROJECT_ROOT / "data" / DEFAULT_DB_FILENAME
    )
    chat_context_messages: int = DEFAULT_CHAT_CONTEXT_MESSAGES
    max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS

    api_key: str = field(default="", repr=False)
    model_configured: bool = False
    has_env_file: bool = False
    warnings: tuple = ()

    @property
    def model_api_key(self) -> str:
        """The resolved model API key (never logged or rendered)."""
        return self.api_key


def resolve_settings(env=None, *, dotenv=True, env_path=None) -> Settings:
    """Build :class:`Settings` from the environment over the defaults."""
    if dotenv:
        load_dotenv_if_present(env_path)
    environment = os.environ if env is None else env

    warnings: list[str] = []

    model_api_key_env = _clean(
        environment.get("AGENT_MODEL_API_KEY_ENV"), DEFAULT_MODEL_API_KEY_ENV
    )
    api_key = str(environment.get(model_api_key_env) or "").strip()
    model_configured = bool(api_key)
    if not model_configured:
        warnings.append(
            f"{model_api_key_env} is not set; the model is not configured"
        )

    trace_raw = str(environment.get("AGENT_TRACE_PATH") or DEFAULT_TRACE_PATH).strip()
    trace_path = Path(trace_raw)
    if not trace_path.is_absolute():
        trace_path = PROJECT_ROOT / trace_path

    if dotenv:
        configured_env = Path(env_path) if env_path is not None else PROJECT_ROOT / ".env"
        has_env_file = configured_env.exists()
    else:
        has_env_file = False

    backend_host = _clean(environment.get("BACKEND_HOST"), DEFAULT_BACKEND_HOST)
    backend_port = _to_int(environment.get("BACKEND_PORT"), DEFAULT_BACKEND_PORT)
    public_backend_url = _clean(
        environment.get("PUBLIC_BACKEND_URL"), f"http://{backend_host}:{backend_port}"
    )
    public_frontend_url = _clean(
        environment.get("PUBLIC_FRONTEND_URL"),
        f"http://{backend_host}:{backend_port}",
    )

    return Settings(
        model_base_url=_clean(
            environment.get("AGENT_MODEL_BASE_URL"), DEFAULT_MODEL_BASE_URL
        ),
        model_name=_clean(environment.get("AGENT_MODEL_NAME"), DEFAULT_MODEL_NAME),
        model_api_key_env=model_api_key_env,
        model_timeout_seconds=_to_float(
            environment.get("AGENT_MODEL_TIMEOUT_SECONDS"), DEFAULT_MODEL_TIMEOUT_SECONDS
        ),
        mcp_server_url=_clean(
            environment.get("MCP_SERVER_URL"), DEFAULT_MCP_SERVER_URL
        ),
        mcp_server_host=_clean(
            environment.get("MCP_SERVER_HOST"), DEFAULT_MCP_SERVER_HOST
        ),
        mcp_server_port=_to_int(
            environment.get("MCP_SERVER_PORT"), DEFAULT_MCP_SERVER_PORT
        ),
        mcp_connect_timeout_seconds=_to_float(
            environment.get("MCP_CONNECT_TIMEOUT_SECONDS"),
            DEFAULT_MCP_CONNECT_TIMEOUT_SECONDS,
        ),
        mcp_call_timeout_seconds=_to_float(
            environment.get("MCP_CALL_TIMEOUT_SECONDS"), DEFAULT_MCP_CALL_TIMEOUT_SECONDS
        ),
        backend_host=backend_host,
        backend_port=backend_port,
        public_backend_url=public_backend_url,
        public_frontend_url=public_frontend_url,
        trace_path=trace_path,
        log_level=_clean(environment.get("AGENT_LOG_LEVEL"), DEFAULT_LOG_LEVEL).upper(),
        db_path=resolve_db_path(environment),
        chat_context_messages=_to_int_clamped(
            environment.get("AGENT_CHAT_CONTEXT_MESSAGES"),
            DEFAULT_CHAT_CONTEXT_MESSAGES,
            CHAT_CONTEXT_MIN_MESSAGES,
            CHAT_CONTEXT_MAX_MESSAGES,
        ),
        max_tool_rounds=_to_int_clamped(
            environment.get("AGENT_MAX_TOOL_ROUNDS"),
            DEFAULT_MAX_TOOL_ROUNDS,
            MAX_TOOL_ROUNDS_MIN,
            MAX_TOOL_ROUNDS_MAX,
        ),
        api_key=api_key,
        model_configured=model_configured,
        has_env_file=has_env_file,
        warnings=tuple(warnings),
    )


def load_settings() -> Settings:
    """Resolve settings from the real process environment and ``.env``."""
    return resolve_settings()
