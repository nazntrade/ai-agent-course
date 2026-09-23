"""Configuration of the MCP server's optional ``search_web`` tool.

Only the MCP server process reads this configuration, so the search API key
never reaches the backend, the model, the UI or the trace. The module never
imports ``agent.*``: the MCP server is a separate process with its own
dependencies and must stay independent from the agent package.

The key is excluded from ``repr()`` by the frozen dataclass, and a missing key
simply leaves the tool unconfigured instead of turning it into a failed call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

DEFAULT_SEARCH_API_KEY_ENV = "TAVILY_API_KEY"
DEFAULT_SEARCH_BASE_URL = "https://api.tavily.com"
DEFAULT_SEARCH_TIMEOUT_SECONDS = 10.0
DEFAULT_SEARCH_MAX_RESULTS = 5

SEARCH_TIMEOUT_MIN_SECONDS = 1.0
SEARCH_TIMEOUT_MAX_SECONDS = 25.0
SEARCH_MAX_RESULTS_MIN = 1
SEARCH_MAX_RESULTS_CAP = 10

# ``MCP_LOAD_DOTENV=0`` keeps the MCP server from reading a local ``.env`` at
# all; the harnesses use it to guarantee an isolated child environment.
LOAD_DOTENV_ENV = "MCP_LOAD_DOTENV"


def load_dotenv_if_present(env_path=None, env=None) -> bool:
    """Load ``.env`` when it exists and ``MCP_LOAD_DOTENV`` allows it.

    A missing file, a missing ``python-dotenv`` or ``MCP_LOAD_DOTENV=0`` is not
    an error. Values already present in the process environment win, because the
    file is loaded with ``override=False``.
    """
    environment = os.environ if env is None else env
    if str(environment.get(LOAD_DOTENV_ENV) or "1").strip() == "0":
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - python-dotenv is a pinned dependency
        return False
    path = Path(env_path) if env_path is not None else DEFAULT_ENV_PATH
    if not path.exists():
        return False
    load_dotenv(path, override=False)
    return True


def _clean_base_url(value) -> str:
    """Return a usable http(s) base URL, otherwise the documented default."""
    text = str(value or "").strip()
    if not text:
        return DEFAULT_SEARCH_BASE_URL
    if not (text.startswith("http://") or text.startswith("https://")):
        return DEFAULT_SEARCH_BASE_URL
    text = text.rstrip("/")
    return text if text not in ("http:", "https:") else DEFAULT_SEARCH_BASE_URL


def _clamped_float(value, default: float, low: float, high: float) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    if parsed != parsed:  # NaN
        return default
    return min(max(parsed, low), high)


def _clamped_int(value, default: int, low: int, high: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return min(max(parsed, low), high)


@dataclass(frozen=True)
class SearchConfig:
    """The resolved web-search configuration of the MCP server.

    ``api_key`` is excluded from ``repr()`` so a debugging print can never leak
    it.
    """

    api_key: str = field(default="", repr=False)
    base_url: str = DEFAULT_SEARCH_BASE_URL
    timeout_seconds: float = DEFAULT_SEARCH_TIMEOUT_SECONDS
    max_results: int = DEFAULT_SEARCH_MAX_RESULTS
    api_key_env: str = DEFAULT_SEARCH_API_KEY_ENV

    @property
    def configured(self) -> bool:
        """Whether a non-empty search API key is available."""
        return bool(self.api_key)


def resolve_search_config(env=None, *, dotenv=True) -> SearchConfig:
    """Build :class:`SearchConfig` from the environment over the defaults."""
    environment = os.environ if env is None else env
    if dotenv:
        load_dotenv_if_present(env=environment)
        environment = os.environ if env is None else env

    api_key_env = (
        str(environment.get("MCP_SEARCH_API_KEY_ENV") or DEFAULT_SEARCH_API_KEY_ENV).strip()
        or DEFAULT_SEARCH_API_KEY_ENV
    )
    api_key = str(environment.get(api_key_env) or "").strip()

    return SearchConfig(
        api_key=api_key,
        base_url=_clean_base_url(environment.get("MCP_SEARCH_BASE_URL")),
        timeout_seconds=_clamped_float(
            environment.get("MCP_SEARCH_TIMEOUT_SECONDS"),
            DEFAULT_SEARCH_TIMEOUT_SECONDS,
            SEARCH_TIMEOUT_MIN_SECONDS,
            SEARCH_TIMEOUT_MAX_SECONDS,
        ),
        max_results=_clamped_int(
            environment.get("MCP_SEARCH_MAX_RESULTS"),
            DEFAULT_SEARCH_MAX_RESULTS,
            SEARCH_MAX_RESULTS_MIN,
            SEARCH_MAX_RESULTS_CAP,
        ),
        api_key_env=api_key_env,
    )
