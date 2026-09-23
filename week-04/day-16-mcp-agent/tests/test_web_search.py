"""Unit tests of the MCP web-search configuration and service (no network).

The HTTP transport is replaced by a scripted double, so no socket is opened and
the real ``.env`` is never read: every configuration test passes an explicit
environment and ``dotenv=False``.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcp_server import web_search
from mcp_server.config import (
    DEFAULT_SEARCH_API_KEY_ENV,
    DEFAULT_SEARCH_BASE_URL,
    DEFAULT_SEARCH_MAX_RESULTS,
    DEFAULT_SEARCH_TIMEOUT_SECONDS,
    SearchConfig,
    load_dotenv_if_present,
    resolve_search_config,
)
from mcp_server.web_search import (
    HttpResponse,
    SearchError,
    TransportError,
    WebSearchService,
)

API_KEY = "unit-test-key"


def _payload(results, *, original="cats", **extra) -> str:
    payload: dict = {"results": results}
    if original is not None:
        payload["query"] = original
    payload.update(extra)
    return json.dumps(payload)


RESULT_ONE = {
    "title": "Docs 1",
    "url": "https://docs.example.test/1",
    "content": "First snippet.",
    "score": 0.9,
}
RESULT_TWO = {
    "title": "Docs 2",
    "url": "https://docs.example.test/2",
    "content": "Second snippet.",
    "score": 0.8,
}


class FakeTransport:
    """A scripted ``Transport`` that records every request."""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls: list = []

    def post_json(self, url, *, headers, json_body, timeout_s):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "json_body": dict(json_body),
                "timeout_s": timeout_s,
            }
        )
        if self.error is not None:
            raise self.error
        return self.response


def _service(transport, *, api_key=API_KEY, **overrides) -> WebSearchService:
    config = SearchConfig(api_key=api_key, **overrides)
    return WebSearchService(config, transport)


class SearchConfigTest(unittest.TestCase):
    """The configuration resolves from the environment with safe defaults."""

    def test_defaults_without_a_key(self):
        config = resolve_search_config(env={}, dotenv=False)
        self.assertFalse(config.configured)
        self.assertEqual(config.base_url, DEFAULT_SEARCH_BASE_URL)
        self.assertEqual(config.timeout_seconds, DEFAULT_SEARCH_TIMEOUT_SECONDS)
        self.assertEqual(config.max_results, DEFAULT_SEARCH_MAX_RESULTS)
        self.assertEqual(config.api_key_env, DEFAULT_SEARCH_API_KEY_ENV)

    def test_environment_overrides(self):
        env = {
            "TAVILY_API_KEY": API_KEY,
            "MCP_SEARCH_BASE_URL": "http://127.0.0.1:9999/",
            "MCP_SEARCH_TIMEOUT_SECONDS": "4.5",
            "MCP_SEARCH_MAX_RESULTS": "7",
        }
        config = resolve_search_config(env=env, dotenv=False)
        self.assertTrue(config.configured)
        self.assertEqual(config.api_key, API_KEY)
        self.assertEqual(config.base_url, "http://127.0.0.1:9999")
        self.assertEqual(config.timeout_seconds, 4.5)
        self.assertEqual(config.max_results, 7)

    def test_custom_api_key_env_name(self):
        env = {"MCP_SEARCH_API_KEY_ENV": "MY_SEARCH_KEY", "MY_SEARCH_KEY": API_KEY}
        config = resolve_search_config(env=env, dotenv=False)
        self.assertTrue(config.configured)
        self.assertEqual(config.api_key_env, "MY_SEARCH_KEY")

    def test_base_url_must_be_http_or_https(self):
        for value in ("ftp://example.test", "example.test", "http:", ""):
            with self.subTest(value=value):
                config = resolve_search_config(
                    env={"MCP_SEARCH_BASE_URL": value}, dotenv=False
                )
                self.assertEqual(config.base_url, DEFAULT_SEARCH_BASE_URL)

    def test_timeout_is_clamped(self):
        cases = {"0": 1.0, "100": 25.0, "-5": 1.0, "abc": DEFAULT_SEARCH_TIMEOUT_SECONDS}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                config = resolve_search_config(
                    env={"MCP_SEARCH_TIMEOUT_SECONDS": raw}, dotenv=False
                )
                self.assertEqual(config.timeout_seconds, expected)

    def test_max_results_is_clamped(self):
        cases = {"0": 1, "100": 10, "-3": 1, "abc": DEFAULT_SEARCH_MAX_RESULTS}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                config = resolve_search_config(
                    env={"MCP_SEARCH_MAX_RESULTS": raw}, dotenv=False
                )
                self.assertEqual(config.max_results, expected)

    def test_repr_never_contains_the_key(self):
        config = resolve_search_config(env={"TAVILY_API_KEY": API_KEY}, dotenv=False)
        rendered = repr(config)
        self.assertNotIn(API_KEY, rendered)
        self.assertNotIn("api_key=", rendered)


class LoadDotenvTest(unittest.TestCase):
    """``MCP_LOAD_DOTENV=0`` stops the MCP server from reading ``.env``."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.env_path = Path(self._tmp.name) / ".env"
        self.env_path.write_text("MCP_TEST_DOTENV_VALUE=from-file\n", encoding="utf-8")
        self._saved = {
            "MCP_LOAD_DOTENV": os.environ.pop("MCP_LOAD_DOTENV", None),
            "MCP_TEST_DOTENV_VALUE": os.environ.pop("MCP_TEST_DOTENV_VALUE", None),
        }

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        os.environ.pop("MCP_TEST_DOTENV_VALUE", None)
        self._tmp.cleanup()

    def test_disabled_load_reads_nothing(self):
        os.environ["MCP_LOAD_DOTENV"] = "0"
        loaded = load_dotenv_if_present(env_path=self.env_path)
        self.assertFalse(loaded)
        self.assertNotIn("MCP_TEST_DOTENV_VALUE", os.environ)

    def test_enabled_load_reads_the_file(self):
        os.environ["MCP_LOAD_DOTENV"] = "1"
        loaded = load_dotenv_if_present(env_path=self.env_path)
        self.assertTrue(loaded)
        self.assertEqual(os.environ.get("MCP_TEST_DOTENV_VALUE"), "from-file")

    def test_missing_file_is_not_an_error(self):
        self.assertFalse(
            load_dotenv_if_present(env_path=Path(self._tmp.name) / "missing.env")
        )


class SearchSuccessTest(unittest.TestCase):
    """A successful search returns compact, sanitized snippets."""

    def test_result_shape_and_snippets_note(self):
        transport = FakeTransport(HttpResponse(200, _payload([RESULT_ONE, RESULT_TWO])))
        result = _service(transport).search("cats", 2)
        self.assertEqual(result["query"], "cats")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["results"][0]["url"], RESULT_ONE["url"])
        self.assertEqual(result["results"][0]["title"], "Docs 1")
        self.assertEqual(result["results"][0]["description"], "First snippet.")
        self.assertFalse(result["more_results_available"])
        self.assertIn("Snippets only", result["note"])

    def test_empty_results_are_a_success(self):
        transport = FakeTransport(HttpResponse(200, _payload([])))
        result = _service(transport).search("cats")
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["results"], [])
        self.assertFalse(result["more_results_available"])
        self.assertIn("No results", result["note"])

    def test_query_is_echoed_when_present(self):
        transport = FakeTransport(
            HttpResponse(200, _payload([RESULT_ONE], original="echoed query"))
        )
        result = _service(transport).search("cats")
        self.assertEqual(result["query"], "echoed query")

    def test_query_falls_back_to_the_normalized_query(self):
        for original in (None, "", "   ", 123):
            with self.subTest(original=repr(original)):
                transport = FakeTransport(
                    HttpResponse(200, _payload([RESULT_ONE], original=original))
                )
                result = _service(transport).search("  cats  dogs ")
                self.assertEqual(result["query"], "cats dogs")

    def test_more_results_available_is_always_false(self):
        transport = FakeTransport(
            HttpResponse(200, _payload([RESULT_ONE], more_results_available=True))
        )
        result = _service(transport).search("cats")
        self.assertFalse(result["more_results_available"])

        transport = FakeTransport(
            HttpResponse(200, _payload([], more_results_available=True))
        )
        result = _service(transport).search("cats")
        self.assertFalse(result["more_results_available"])

    def test_whitespace_is_collapsed_and_long_query_is_truncated(self):
        transport = FakeTransport(HttpResponse(200, _payload([RESULT_ONE])))
        service = _service(transport)
        service.search("  a   b  ")
        self.assertEqual(transport.calls[0]["json_body"]["query"], "a b")

        transport.calls.clear()
        service.search("x" * 700)
        self.assertEqual(
            len(transport.calls[0]["json_body"]["query"]), web_search.MAX_QUERY_LENGTH
        )

    def test_overlong_fields_are_truncated_and_collapsed(self):
        item = {
            "title": "t" * 500,
            "url": "https://docs.example.test/1",
            "content": "d\n\n  e" + "f" * 500,
        }
        transport = FakeTransport(HttpResponse(200, _payload([item])))
        result = _service(transport).search("cats")
        self.assertEqual(len(result["results"][0]["title"]), web_search.TITLE_LIMIT)
        self.assertEqual(
            len(result["results"][0]["description"]), web_search.DESCRIPTION_LIMIT
        )
        self.assertNotIn("\n", result["results"][0]["description"])

    def test_non_http_urls_are_dropped(self):
        items = [
            {"title": "bad", "url": "ftp://docs.example.test/1", "content": "x"},
            {"title": "bad2", "url": "javascript:alert(1)", "content": "x"},
            {"title": "bad3", "url": "", "content": "x"},
            RESULT_ONE,
        ]
        transport = FakeTransport(HttpResponse(200, _payload(items)))
        result = _service(transport).search("cats")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["url"], RESULT_ONE["url"])

    def test_all_invalid_urls_become_an_empty_success(self):
        items = [{"title": "bad", "url": "ftp://x/1", "content": "x"}]
        transport = FakeTransport(HttpResponse(200, _payload(items)))
        result = _service(transport).search("cats")
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["results"], [])


class SearchRequestShapeTest(unittest.TestCase):
    """The outgoing request matches the documented Tavily contract."""

    def test_request_url_headers_body_and_timeout(self):
        transport = FakeTransport(HttpResponse(200, _payload([RESULT_ONE])))
        _service(transport, timeout_seconds=4.0).search("  cats  ", 3)
        call = transport.calls[0]
        self.assertEqual(call["url"], DEFAULT_SEARCH_BASE_URL + "/search")
        self.assertEqual(call["headers"]["Accept"], "application/json")
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {API_KEY}")
        self.assertEqual(call["json_body"]["query"], "cats")
        self.assertEqual(call["json_body"]["search_depth"], "basic")
        self.assertEqual(call["json_body"]["max_results"], 3)
        self.assertEqual(call["timeout_s"], 4.0)

    def test_max_results_zero_uses_the_configured_default(self):
        transport = FakeTransport(HttpResponse(200, _payload([RESULT_ONE])))
        _service(transport, max_results=2).search("cats", 0)
        self.assertEqual(transport.calls[0]["json_body"]["max_results"], 2)

    def test_max_results_is_capped(self):
        transport = FakeTransport(HttpResponse(200, _payload([RESULT_ONE])))
        _service(transport).search("cats", 1000)
        self.assertEqual(transport.calls[0]["json_body"]["max_results"], 10)

    def test_missing_key_never_calls_the_transport(self):
        transport = FakeTransport(HttpResponse(200, _payload([RESULT_ONE])))
        service = _service(transport, api_key="")
        self.assertFalse(service.configured)
        with self.assertRaises(SearchError) as caught:
            service.search("cats")
        self.assertEqual(caught.exception.category, "not_configured")
        self.assertIn("not configured", caught.exception.message)
        self.assertEqual(transport.calls, [])


class SearchFailureTest(unittest.TestCase):
    """Every provider boundary failure maps to a fixed, sanitized sentence."""

    def _error(self, response=None, error=None) -> SearchError:
        transport = FakeTransport(response, error)
        with self.assertRaises(SearchError) as caught:
            _service(transport).search("cats")
        return caught.exception

    def test_timeout(self):
        exc = self._error(error=TransportError("timeout", "timed out"))
        self.assertEqual(exc.category, "timeout")
        self.assertEqual(exc.message, "The web search request timed out.")

    def test_unreachable(self):
        exc = self._error(error=TransportError("unreachable", "unreachable"))
        self.assertEqual(exc.category, "unreachable")
        self.assertEqual(exc.message, "The web search service is unreachable.")

    def test_rejected_statuses(self):
        for status in (401, 403, 432, 433):
            with self.subTest(status=status):
                exc = self._error(response=HttpResponse(status, "{}"))
                self.assertEqual(exc.category, "api_error")
                self.assertEqual(
                    exc.message,
                    f"The web search service rejected the request (HTTP {status}).",
                )

    def test_http_429(self):
        exc = self._error(response=HttpResponse(429, "{}"))
        self.assertEqual(exc.category, "api_error")
        self.assertEqual(
            exc.message, "The web search rate limit was reached (HTTP 429)."
        )

    def test_failed_statuses(self):
        for status in (400, 422, 500):
            with self.subTest(status=status):
                exc = self._error(response=HttpResponse(status, "server error"))
                self.assertEqual(exc.category, "api_error")
                self.assertEqual(
                    exc.message, f"The web search service failed (HTTP {status})."
                )

    def test_malformed_json(self):
        exc = self._error(response=HttpResponse(200, "<<not json>>"))
        self.assertEqual(exc.category, "invalid_response")
        self.assertEqual(
            exc.message, "The web search service returned an unexpected response."
        )

    def test_missing_results(self):
        exc = self._error(response=HttpResponse(200, json.dumps({"foo": 1})))
        self.assertEqual(exc.category, "invalid_response")

    def test_results_not_a_list(self):
        exc = self._error(
            response=HttpResponse(200, json.dumps({"query": "cats", "results": {}}))
        )
        self.assertEqual(exc.category, "invalid_response")

    def test_failure_message_never_contains_the_key(self):
        for response, error in (
            (HttpResponse(401, API_KEY), None),
            (HttpResponse(500, API_KEY), None),
            (HttpResponse(200, API_KEY), None),
            (None, TransportError("timeout", API_KEY)),
        ):
            with self.subTest(response=response, error=error):
                exc = self._error(response=response, error=error)
                self.assertNotIn(API_KEY, exc.message)


class DefaultServiceTest(unittest.TestCase):
    """The default service is built once and cached."""

    def test_lazy_singleton(self):
        with mock.patch.object(web_search, "_DEFAULT_SERVICE", None):
            with mock.patch.object(
                web_search,
                "resolve_search_config",
                return_value=SearchConfig(api_key=API_KEY),
            ) as resolver:
                first = web_search.default_service()
                second = web_search.default_service()
        self.assertIs(first, second)
        resolver.assert_called_once()


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
