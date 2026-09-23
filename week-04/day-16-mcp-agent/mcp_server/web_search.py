"""HTTP client and service of the ``search_web`` MCP tool.

This is the only module of the MCP server that touches the network. It performs
a single outgoing HTTPS POST against the Tavily Search API (the module never
retries) and returns a compact, sanitized result set.

The API key is held by the service and written only into the outgoing request
header. It never appears in a result, an error message, a log line or
``repr()``: the error categories map every failure to a fixed, safe sentence.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from mcp_server.config import (
    SEARCH_MAX_RESULTS_CAP,
    SearchConfig,
    resolve_search_config,
)

CATEGORY_NOT_CONFIGURED = "not_configured"
CATEGORY_TIMEOUT = "timeout"
CATEGORY_UNREACHABLE = "unreachable"
CATEGORY_API_ERROR = "api_error"
CATEGORY_INVALID_RESPONSE = "invalid_response"

CATEGORY_TRANSPORT_TIMEOUT = "timeout"
CATEGORY_TRANSPORT_UNREACHABLE = "unreachable"

SEARCH_PATH = "/search"

MAX_QUERY_LENGTH = 600
TITLE_LIMIT = 200
DESCRIPTION_LIMIT = 300

SEARCH_NOTE = "Snippets only; the pages were not opened."
EMPTY_NOTE = "No results found for this query."

NOT_CONFIGURED_MESSAGE = (
    "Web search is not configured on this server (the search API key is "
    "missing). Do not invent results."
)
TIMEOUT_MESSAGE = "The web search request timed out."
UNREACHABLE_MESSAGE = "The web search service is unreachable."
INVALID_RESPONSE_MESSAGE = "The web search service returned an unexpected response."


@dataclass(frozen=True)
class HttpResponse:
    """A minimal HTTP response: status plus raw body text."""

    status: int
    text: str


class Transport(Protocol):
    """The boundary the service depends on; only :class:`UrllibTransport` talks
    to the network."""

    def post_json(
        self, url: str, *, headers: dict, json_body: dict, timeout_s: float
    ) -> HttpResponse:
        ...


class TransportError(Exception):
    """A failed transport call, categorized as ``timeout`` or ``unreachable``."""

    def __init__(self, category: str, message: str = ""):
        super().__init__(message)
        self.category = category
        self.message = message


class SearchError(Exception):
    """A sanitized web-search failure with one explicit category."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category
        self.message = message


class UrllibTransport:
    """A one-shot JSON POST over the standard library (no retries)."""

    def post_json(
        self, url: str, *, headers: dict, json_body: dict, timeout_s: float
    ) -> HttpResponse:
        body = json.dumps(json_body or {}).encode("utf-8")
        request_headers = {
            str(key): str(value) for key, value in (headers or {}).items()
        }
        request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            str(url),
            data=body,
            headers=request_headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                body = response.read().decode("utf-8", errors="replace")
                return HttpResponse(int(getattr(response, "status", 200)), body)
        except urllib.error.HTTPError as exc:
            # A 4xx/5xx is a regular response for the caller to classify.
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - the body is optional
                body = ""
            return HttpResponse(int(getattr(exc, "code", 0)), body)
        except (TimeoutError, socket.timeout) as exc:
            raise TransportError(CATEGORY_TRANSPORT_TIMEOUT, TIMEOUT_MESSAGE) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise TransportError(
                    CATEGORY_TRANSPORT_TIMEOUT, TIMEOUT_MESSAGE
                ) from exc
            raise TransportError(
                CATEGORY_TRANSPORT_UNREACHABLE, UNREACHABLE_MESSAGE
            ) from exc
        except OSError as exc:
            raise TransportError(
                CATEGORY_TRANSPORT_UNREACHABLE, UNREACHABLE_MESSAGE
            ) from exc


def _collapse(value) -> str:
    """Collapse any whitespace run into a single space."""
    return " ".join(str(value or "").split())


def _is_http_url(value) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    try:
        split = urllib.parse.urlsplit(text)
    except ValueError:
        return False
    return split.scheme in ("http", "https") and bool(split.netloc)


class WebSearchService:
    """One configured search service; the transport is injectable for tests."""

    def __init__(self, config: SearchConfig, transport: Transport | None = None):
        self._config = config
        self._transport = transport if transport is not None else UrllibTransport()

    @property
    def configured(self) -> bool:
        return self._config.configured

    def _limit(self, max_results) -> int:
        try:
            value = int(max_results)
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            return self._config.max_results
        return min(value, SEARCH_MAX_RESULTS_CAP)

    def _sanitize_results(self, items: list, limit: int) -> list:
        results: list = []
        for item in items:
            if len(results) >= limit:
                break
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not _is_http_url(url):
                continue
            results.append(
                {
                    "title": _collapse(item.get("title"))[:TITLE_LIMIT],
                    "url": url.strip(),
                    "description": _collapse(item.get("content"))[
                        :DESCRIPTION_LIMIT
                    ],
                }
            )
        return results

    def search(self, query: str, max_results: int = 0) -> dict:
        """Run one search and return a compact structured result.

        A missing key is reported before any transport call. An empty result set
        is a successful, honest answer; an API failure is never turned into a
        fabricated result.
        """
        if not self._config.configured:
            raise SearchError(CATEGORY_NOT_CONFIGURED, NOT_CONFIGURED_MESSAGE)

        normalized = " ".join(str(query or "").split())
        if len(normalized) > MAX_QUERY_LENGTH:
            normalized = normalized[:MAX_QUERY_LENGTH]
        limit = self._limit(max_results)

        json_body = {
            "query": normalized,
            "search_depth": "basic",
            "max_results": limit,
        }
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._config.api_key}",
        }
        url = self._config.base_url + SEARCH_PATH

        try:
            response = self._transport.post_json(
                url,
                headers=headers,
                json_body=json_body,
                timeout_s=self._config.timeout_seconds,
            )
        except TransportError as exc:
            if exc.category == CATEGORY_TRANSPORT_TIMEOUT:
                raise SearchError(CATEGORY_TIMEOUT, TIMEOUT_MESSAGE) from exc
            raise SearchError(CATEGORY_UNREACHABLE, UNREACHABLE_MESSAGE) from exc

        status = int(getattr(response, "status", 0))
        if status in (401, 403, 432, 433):
            raise SearchError(
                CATEGORY_API_ERROR,
                f"The web search service rejected the request (HTTP {status}).",
            )
        if status == 429:
            raise SearchError(
                CATEGORY_API_ERROR,
                "The web search rate limit was reached (HTTP 429).",
            )
        if status != 200:
            raise SearchError(
                CATEGORY_API_ERROR,
                f"The web search service failed (HTTP {status}).",
            )

        try:
            payload = json.loads(getattr(response, "text", "") or "")
        except (TypeError, ValueError) as exc:
            raise SearchError(
                CATEGORY_INVALID_RESPONSE, INVALID_RESPONSE_MESSAGE
            ) from exc
        if not isinstance(payload, dict):
            raise SearchError(CATEGORY_INVALID_RESPONSE, INVALID_RESPONSE_MESSAGE)

        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise SearchError(CATEGORY_INVALID_RESPONSE, INVALID_RESPONSE_MESSAGE)

        original = normalized
        echoed = payload.get("query")
        if isinstance(echoed, str) and echoed.strip():
            original = echoed.strip()

        results = self._sanitize_results(raw_results, limit)
        if not results:
            return {
                "query": original,
                "count": 0,
                "results": [],
                "more_results_available": False,
                "note": EMPTY_NOTE,
            }
        return {
            "query": original,
            "count": len(results),
            "results": results,
            # Tavily has no equivalent of a "more results" flag, so the shared
            # contract always reports False instead of guessing from limit/count.
            "more_results_available": False,
            "note": SEARCH_NOTE,
        }


# The config is resolved once, on the first tool call, and cached: building the
# server and serving ``tools/list`` must not read the environment or create a
# service.
_DEFAULT_SERVICE: WebSearchService | None = None


def default_service() -> WebSearchService:
    """Return the process-wide service, built lazily from the environment."""
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        _DEFAULT_SERVICE = WebSearchService(resolve_search_config())
    return _DEFAULT_SERVICE
