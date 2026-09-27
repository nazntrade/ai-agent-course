"""Configuration of the notifier MCP server (server B).

Only the notifier process reads this configuration, so the Telegram bot token
and the recipient chat id never reach the backend, the model, the UI or the
trace. The module never imports ``agent.*``: server B is a separate process
with its own database and must stay independent from the agent package.

The token and the recipient are excluded from ``repr()`` by the frozen
dataclass, and a missing token simply leaves delivery unconfigured instead of
turning it into a failed network call. ``NOTIFIER_LOAD_DOTENV=0`` fully
disables reading a local ``.env``, which the tests and harnesses rely on for an
isolated child environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

DEFAULT_DB_FILENAME = "day20-notifier.sqlite3"
DB_PATH_ENV = "NOTIFIER_DB_PATH"

TELEGRAM_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
TELEGRAM_RECIPIENT_ENV = "TELEGRAM_CHAT_ID"
DEFAULT_TELEGRAM_API_BASE_URL = "https://api.telegram.org"

# ``NOTIFIER_LOAD_DOTENV=0`` keeps the notifier process from reading a local
# ``.env`` at all; the harnesses use it to guarantee an isolated child
# environment that never touches the real secrets.
LOAD_DOTENV_ENV = "NOTIFIER_LOAD_DOTENV"


def load_dotenv_if_present(env_path=None, env=None) -> bool:
    """Load ``.env`` when it exists and ``NOTIFIER_LOAD_DOTENV`` allows it.

    A missing file, a missing ``python-dotenv`` or ``NOTIFIER_LOAD_DOTENV=0`` is
    not an error. Values already present in the process environment win, because
    the file is loaded with ``override=False``.
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


def _environment_with_dotenv(env, env_path):
    """Return the environment mapping, loading ``.env`` when none was supplied.

    ``env`` is an explicit mapping for callers that fully control the values
    (tests); it never touches the file system. With ``env is None`` the process
    environment is used and ``.env`` is loaded first, unless
    ``NOTIFIER_LOAD_DOTENV=0`` disables the read.
    """
    if env is not None:
        return env
    load_dotenv_if_present(env_path=env_path)
    return os.environ


def _clean_base_url(value) -> str:
    """Return a usable http(s) base URL, otherwise the documented default."""
    text = str(value or "").strip()
    if not text:
        return DEFAULT_TELEGRAM_API_BASE_URL
    if not (text.startswith("http://") or text.startswith("https://")):
        return DEFAULT_TELEGRAM_API_BASE_URL
    text = text.rstrip("/")
    return text if text not in ("http:", "https:") else DEFAULT_TELEGRAM_API_BASE_URL


def resolve_db_path(env=None, *, env_path=None) -> Path:
    """Resolve ``NOTIFIER_DB_PATH``, defaulting inside the project.

    The default is computed from the project root here as well, so both the
    service and the tests agree on the file without ``agent.*`` being imported.
    ``.env`` is loaded before the value is read unless ``NOTIFIER_LOAD_DOTENV=0``
    disables it.
    """
    environment = _environment_with_dotenv(env, env_path)
    raw = str(environment.get(DB_PATH_ENV) or "").strip()
    path = Path(raw) if raw else PROJECT_ROOT / "data" / DEFAULT_DB_FILENAME
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


@dataclass(frozen=True)
class TelegramConfig:
    """The resolved Telegram configuration of the notifier server.

    ``token`` and ``recipient`` are excluded from ``repr()`` so a debugging
    print can never leak them, and the recipient is never an argument of a tool:
    the model cannot choose an arbitrary Telegram chat.
    """

    token: str = field(default="", repr=False)
    recipient: str = field(default="", repr=False)
    base_url: str = DEFAULT_TELEGRAM_API_BASE_URL

    @property
    def configured(self) -> bool:
        """Whether a non-empty token and recipient are available."""
        return bool(self.token and self.recipient)


def resolve_telegram_config(env=None, *, dotenv=True) -> TelegramConfig:
    """Build :class:`TelegramConfig` from the environment over the defaults."""
    environment = os.environ if env is None else env
    if dotenv:
        load_dotenv_if_present(env=environment)
        environment = os.environ if env is None else env

    return TelegramConfig(
        token=str(environment.get(TELEGRAM_TOKEN_ENV) or "").strip(),
        recipient=str(environment.get(TELEGRAM_RECIPIENT_ENV) or "").strip(),
        base_url=_clean_base_url(environment.get("NOTIFIER_TELEGRAM_API_BASE_URL")),
    )
