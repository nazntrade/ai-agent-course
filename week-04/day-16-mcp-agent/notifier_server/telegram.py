"""The single network boundary of the notifier server.

Only this module talks to Telegram. The base URL is injected through the
configuration (``NOTIFIER_TELEGRAM_API_BASE_URL``), so tests point it at a
loopback fake and never touch the real service.

Every failure is mapped to a fixed, sanitized sentence: the bot token, the
request headers, the response body and the full request URL can never appear in
a result, an error message, a delivery row or a log line. A missing token (or a
missing recipient) yields an honest ``not_configured`` without any network call,
so an unconfigured server never invents a delivery.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass

from notifier_server.config import TelegramConfig, resolve_telegram_config

STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_NOT_CONFIGURED = "not_configured"

TELEGRAM_TIMEOUT_SECONDS = 10.0
SEND_MESSAGE_METHOD = "sendMessage"

NOT_CONFIGURED_MESSAGE = (
    "Telegram delivery is not configured on this server (the bot token or the "
    "recipient chat id is missing). Do not invent a delivery."
)
TIMEOUT_MESSAGE = "The Telegram API request timed out."
UNREACHABLE_MESSAGE = "The Telegram API is unreachable."
INVALID_RESPONSE_MESSAGE = "The Telegram API returned an unexpected response."


@dataclass(frozen=True)
class TelegramResult:
    """The outcome of one Telegram delivery attempt."""

    status: str
    message_id: str | None = None
    error: str | None = None


class _TelegramTransportError(Exception):
    """Base class of the categorized transport failures."""


class _TelegramTimeout(_TelegramTransportError):
    """The request did not finish in time."""


class _TelegramUnreachable(_TelegramTransportError):
    """The service could not be reached."""


def _post_json(url: str, payload: dict, timeout_s: float):
    """POST a JSON body and return ``(status, body_text)``.

    A non-2xx response is returned as a regular response so the caller can
    classify it; transport failures raise a categorized error. Nothing from the
    exception (URL, headers, body) is carried into the message.
    """
    body = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        str(url),
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            text = response.read().decode("utf-8", errors="replace")
            return int(getattr(response, "status", 200)), text
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - the body is optional
            text = ""
        return int(getattr(exc, "code", 0)), text
    except (TimeoutError, socket.timeout) as exc:
        raise _TelegramTimeout() from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise _TelegramTimeout() from exc
        raise _TelegramUnreachable() from exc
    except OSError as exc:
        raise _TelegramUnreachable() from exc


class TelegramClient:
    """One configured Telegram sender; the base URL is injectable for tests."""

    def __init__(self, config: TelegramConfig):
        self._config = config

    @property
    def configured(self) -> bool:
        """Whether both a token and a recipient are available."""
        return self._config.configured

    def send_message(self, text: str) -> TelegramResult:
        """Send one plain-text message to the configured recipient.

        The recipient comes from the configuration, never from the arguments, so
        the model cannot redirect a delivery. A missing token is reported before
        any transport call.
        """
        if not self._config.configured:
            return TelegramResult(
                status=STATUS_NOT_CONFIGURED, error=NOT_CONFIGURED_MESSAGE
            )

        body = str(text or "").strip()
        if not body:
            return TelegramResult(
                status=STATUS_FAILED, error="The Telegram message text is empty."
            )

        url = f"{self._config.base_url}/bot{self._config.token}/{SEND_MESSAGE_METHOD}"
        payload = {
            "chat_id": self._config.recipient,
            "text": body,
            "disable_web_page_preview": True,
        }
        try:
            status, raw = _post_json(url, payload, TELEGRAM_TIMEOUT_SECONDS)
        except _TelegramTimeout:
            return TelegramResult(status=STATUS_FAILED, error=TIMEOUT_MESSAGE)
        except _TelegramUnreachable:
            return TelegramResult(status=STATUS_FAILED, error=UNREACHABLE_MESSAGE)

        if status != 200:
            return TelegramResult(
                status=STATUS_FAILED,
                error=f"The Telegram API rejected the request (HTTP {int(status)}).",
            )

        try:
            parsed = json.loads(raw or "")
        except (TypeError, ValueError):
            return TelegramResult(
                status=STATUS_FAILED, error=INVALID_RESPONSE_MESSAGE
            )
        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            return TelegramResult(
                status=STATUS_FAILED, error=INVALID_RESPONSE_MESSAGE
            )

        result = parsed.get("result")
        message_id = None
        if isinstance(result, dict) and result.get("message_id") is not None:
            message_id = str(result.get("message_id"))
        return TelegramResult(status=STATUS_SENT, message_id=message_id)


def default_telegram_client() -> TelegramClient:
    """Build a client from the process environment (used by the service)."""
    return TelegramClient(resolve_telegram_config())


__all__ = [
    "TelegramClient",
    "TelegramResult",
    "default_telegram_client",
    "STATUS_SENT",
    "STATUS_FAILED",
    "STATUS_NOT_CONFIGURED",
    "NOT_CONFIGURED_MESSAGE",
    "TIMEOUT_MESSAGE",
    "UNREACHABLE_MESSAGE",
    "INVALID_RESPONSE_MESSAGE",
]
