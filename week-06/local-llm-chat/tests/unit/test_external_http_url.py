"""Unit test for ExternalHttpProvider URL construction (D28).

Verifies that the REAL ExternalHttpProvider._request() builds correct
Request.full_url by injecting a fixture opener that captures URLs and
returns fake JSON for prefill.

This is NOT a standalone _build_url() simulation — it exercises the
actual provider code path.
"""

import json
import unittest
from unittest.mock import Mock
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "app"))

from app.providers.external_http import ExternalHttpProvider


class _CapturingOpener:
    """Callable opener that captures Request.full_url and returns fixture JSON.

    The provider stores ``self._open = opener or urllib.request.urlopen``
    and calls ``self._open(request, timeout=...)``, so the fixture must be
    directly callable (not an object with an ``.open()`` method).
    """

    def __init__(self):
        self.captured_urls: list[str] = []

    def __call__(self, request, timeout=None):
        self.captured_urls.append(request.full_url)

        class _FixtureResponse:
            """Minimal context-manager response that returns fixture JSON."""

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return json.dumps({"models": []}).encode("utf-8")

        return _FixtureResponse()


class TestExternalHttpUrlConstruction(unittest.TestCase):
    """Verify ACTUAL Request.full_url built by ExternalHttpProvider._request()."""

    def _test_url(self, base_url: str, path: str) -> str:
        """Create provider with capturing opener, call _request, return captured URL."""
        opener = _CapturingOpener()
        provider = ExternalHttpProvider(
            base_url=base_url,
            model_id="test-model",
            api_key="fake-key",
            opener=opener,
        )
        provider._request(path, {})
        return opener.captured_urls[0]

    def test_base_with_v1_models(self):
        url = self._test_url("http://fixture/v1", "/v1/models")
        self.assertEqual(url, "http://fixture/v1/models")

    def test_base_with_v1_chat(self):
        url = self._test_url("http://fixture/v1", "/v1/chat/completions")
        self.assertEqual(url, "http://fixture/v1/chat/completions")

    def test_base_without_v1_models(self):
        url = self._test_url("http://fixture", "/v1/models")
        self.assertEqual(url, "http://fixture/v1/models")

    def test_base_without_v1_chat(self):
        url = self._test_url("http://fixture", "/v1/chat/completions")
        self.assertEqual(url, "http://fixture/v1/chat/completions")

    def test_base_with_trailing_slash_v1(self):
        url = self._test_url("http://fixture/v1/", "/v1/chat/completions")
        self.assertEqual(url, "http://fixture/v1/chat/completions")

    def test_nested_proxy_with_v1(self):
        url = self._test_url(
            "https://proxy.example.com/api/local-models/my-model/v1",
            "/v1/chat/completions",
        )
        self.assertEqual(
            url,
            "https://proxy.example.com/api/local-models/my-model/v1/chat/completions",
        )

    def test_nested_proxy_without_v1(self):
        url = self._test_url(
            "https://proxy.example.com/api/local-models/my-model",
            "/v1/chat/completions",
        )
        self.assertEqual(
            url,
            "https://proxy.example.com/api/local-models/my-model/v1/chat/completions",
        )

    def test_preflight_url_with_v1(self):
        """Verify preflight() calls /v1/models with correct URL."""
        opener = _CapturingOpener()
        provider = ExternalHttpProvider(
            base_url="http://fixture/v1",
            model_id="test-model",
            api_key="fake-key",
            opener=opener,
        )
        provider.preflight()
        url = opener.captured_urls[0]
        self.assertEqual(url, "http://fixture/v1/models")


if __name__ == "__main__":
    unittest.main()
