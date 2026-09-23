"""Regression tests for the readability of chat answer links.

``LinkStyleBrowserTest`` loads the real ``static/index.html`` in a real
headless browser (no server, no network): the request is intercepted and the
files on disk are served, so the page is styled by the real ``static/styles.css``.
A rendered Markdown link is then inspected through ``getComputedStyle``.

The legacy check is deliberate: it renders the same link with an empty
stylesheet, where the browser falls back to its default dark blue. It proves
that the brightness threshold used here really fails on the pre-fix CSS.
"""

from __future__ import annotations

import re
import unittest

from harness.qa_bridge import PROJECT_DIR, qa_browser

STATIC_DIR = (PROJECT_DIR / "static").resolve()
STYLES_CSS = (PROJECT_DIR / "static" / "styles.css").read_text(encoding="utf-8")

# The link must stay readable on the dark panel. #dbeafe has a luminance of
# about 232; the browser default blue (#0000EE) is about 17.
LIGHT_LUMINANCE_THRESHOLD = 150.0

LINK_MARKDOWN = "See [the docs](https://example.com/docs) for details."

# Renders the answer into a real assistant bubble and reads the anchor style.
LINK_STYLE_SNIPPET = r"""
(raw) => {
  document.body.replaceChildren();
  const bubble = document.createElement("div");
  bubble.className = "bubble assistant";
  const text = document.createElement("span");
  text.className = "text";
  bubble.appendChild(text);
  document.body.appendChild(bubble);
  window.renderMarkdownInto(text, raw);
  const anchor = bubble.querySelector("a");
  if (!anchor) {
    return { found: false };
  }
  const style = window.getComputedStyle(anchor);
  return {
    found: true,
    color: style.color,
    decoration_line: style.textDecorationLine,
    decoration_style: style.textDecorationStyle,
    text: anchor.textContent,
  };
}
"""


def _luminance(css_color: str) -> float:
    match = re.match(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", css_color)
    if not match:
        raise AssertionError(f"unexpected computed color: {css_color!r}")
    red, green, blue = (int(value) for value in match.groups())
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


class LinkStyleBrowserTest(unittest.TestCase):
    """The real page and the real stylesheet are exercised in a browser."""

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise unittest.SkipTest(f"playwright is not installed: {exc}")

        cls._manager = None
        cls._browser = None
        cls._context = None
        cls.page = None
        try:
            cls._manager = sync_playwright().start()
            cls._browser, _channel = qa_browser.launch_browser(
                cls._manager, headless=True
            )
            cls._context = cls._browser.new_context()
        except qa_browser.PrerequisiteError as exc:
            cls._shutdown()
            raise unittest.SkipTest(
                f"no system Chromium-based browser for the DOM tests: {exc}"
            )

    @classmethod
    def _shutdown(cls):
        for closer in (getattr(cls, "_context", None), getattr(cls, "_browser", None)):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # noqa: BLE001 - best effort teardown
                pass
        try:
            if cls._manager is not None:
                cls._manager.stop()
        except Exception:  # noqa: BLE001 - best effort teardown
            pass

    @classmethod
    def tearDownClass(cls):
        cls._shutdown()

    def _open(self, css_body: str):
        """Load index.html with the given stylesheet body, served locally."""
        page = self._context.new_page()

        def handler(route):
            path = route.request.url.split("ui.test", 1)[1].split("?", 1)[0]
            path = path.lstrip("/")
            if path == "":
                path = "index.html"
            if path == "styles.css":
                route.fulfill(
                    status=200, content_type="text/css; charset=utf-8", body=css_body
                )
                return
            candidate = (STATIC_DIR / path).resolve()
            if STATIC_DIR in candidate.parents and candidate.is_file():
                content_type = (
                    "text/html; charset=utf-8"
                    if candidate.suffix == ".html"
                    else "application/javascript; charset=utf-8"
                )
                route.fulfill(
                    status=200, content_type=content_type, body=candidate.read_bytes()
                )
                return
            route.fulfill(status=404, body="")

        page.route("http://ui.test/**", handler)
        page.goto("http://ui.test/index.html", wait_until="load")
        return page

    def _anchor_style(self, css_body: str):
        page = self._open(css_body)
        try:
            self.assertEqual(
                page.evaluate("() => typeof window.renderMarkdownInto"),
                "function",
                "the real index.html must load the Markdown renderer",
            )
            return page.evaluate(LINK_STYLE_SNIPPET, LINK_MARKDOWN)
        finally:
            page.close()

    def test_chat_link_is_light_on_the_dark_panel(self):
        result = self._anchor_style(STYLES_CSS)
        self.assertTrue(result["found"], "the Markdown link must become an anchor")
        self.assertEqual(result["text"], "the docs")
        luminance = _luminance(result["color"])
        self.assertGreater(
            luminance,
            LIGHT_LUMINANCE_THRESHOLD,
            f"chat link {result['color']} is too dark for the dark panel",
        )

    def test_chat_link_is_underlined(self):
        result = self._anchor_style(STYLES_CSS)
        self.assertEqual(result["decoration_line"], "underline")

    def test_brightness_threshold_rejects_the_legacy_stylesheet(self):
        # An empty stylesheet is the pre-fix state: the browser then paints the
        # default dark blue link, which is exactly what the user reported.
        result = self._anchor_style("")
        self.assertTrue(result["found"])
        self.assertLess(
            _luminance(result["color"]),
            LIGHT_LUMINANCE_THRESHOLD,
            "the legacy/default link colour must stay below the readability threshold",
        )


class LinkStyleSourceGuardTest(unittest.TestCase):
    """Fast, browser-free guards for the cache-busting wiring."""

    def test_index_versions_the_stylesheet_link(self):
        index_html = (PROJECT_DIR / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertRegex(
            index_html,
            r'href="/styles\.css\?v=[^"]+"',
            "index.html must request a versioned stylesheet so a stale copy is not reused",
        )

    def test_stylesheet_covers_the_visited_state(self):
        self.assertIn(
            "a:visited",
            STYLES_CSS,
            "the link colour must also cover :visited, not just the default state",
        )


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
