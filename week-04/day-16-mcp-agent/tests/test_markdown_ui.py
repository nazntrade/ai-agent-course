"""Tests of the safe Markdown renderer used by the chat UI.

``MarkdownRendererDomTest`` runs the real ``static/markdown-render.js`` inside a
real headless browser (no server, no network): the file is injected into a blank
page and the resulting DOM is inspected. ``MarkdownSourceGuardTest`` is a fast,
browser-free guard that keeps the security properties of the sources, so a
regression is caught even when no system browser is available.
"""

from __future__ import annotations

import unittest

from harness.qa_bridge import PROJECT_DIR, qa_browser

RENDER_JS = (PROJECT_DIR / "static" / "markdown-render.js").read_text(encoding="utf-8")
APP_JS = (PROJECT_DIR / "static" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (PROJECT_DIR / "static" / "index.html").read_text(encoding="utf-8")

# Renders into a persistent #probe container; renderMarkdownInto replaces the
# whole content on every call, which is exactly the streaming behaviour.
RENDER_SNIPPET = r"""
(raw) => {
  const container = document.getElementById("probe");
  window.renderMarkdownInto(container, raw);
  const markers = container.textContent.match(/\*\*/g);
  return {
    text: container.textContent,
    tags: Array.from(container.querySelectorAll("*")).map((el) => el.tagName.toLowerCase()),
    strong: Array.from(container.querySelectorAll("strong")).map((el) => el.textContent),
    em: Array.from(container.querySelectorAll("em")).map((el) => el.textContent),
    code: Array.from(container.querySelectorAll("code")).map((el) => el.textContent),
    anchors: Array.from(container.querySelectorAll("a")).map((el) => ({
      href: el.getAttribute("href"),
      rel: el.getAttribute("rel"),
      target: el.getAttribute("target"),
    })),
    scripts: container.querySelectorAll("script").length,
    imgs: container.querySelectorAll("img").length,
    ul: container.querySelectorAll("ul").length,
    ol: container.querySelectorAll("ol").length,
    items: Array.from(container.querySelectorAll("li")).map((el) => el.textContent),
    literal_marker_count: markers ? markers.length : 0,
  };
}
"""


class MarkdownRendererDomTest(unittest.TestCase):
    """The renderer is exercised against a real DOM in a real browser."""

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
            cls.page = cls._context.new_page()
            cls.page.set_content(
                "<!DOCTYPE html><html><body><div id='probe'></div></body></html>"
            )
            cls.page.add_script_tag(content=RENDER_JS)
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

    def _render(self, raw):
        return self.page.evaluate(RENDER_SNIPPET, raw)

    def test_renderer_exposes_a_single_function(self):
        self.assertEqual(
            self.page.evaluate("() => typeof window.renderMarkdownInto"), "function"
        )

    def test_bold_becomes_strong_without_literal_markers(self):
        result = self._render("23 × 17 = **391**")
        self.assertEqual(result["text"], "23 × 17 = 391")
        self.assertEqual(result["strong"], ["391"])
        self.assertEqual(result["literal_marker_count"], 0)

    def test_plain_text_is_unchanged(self):
        result = self._render("Hello world, nothing to render.")
        self.assertEqual(result["text"], "Hello world, nothing to render.")
        self.assertEqual(result["tags"], [])

    def test_newlines_are_preserved_as_text(self):
        result = self._render("first line\nsecond line")
        self.assertEqual(result["text"], "first line\nsecond line")

    def test_script_tag_is_text_and_not_executed(self):
        result = self._render("<script>window.__pwned = 1;</script>")
        self.assertEqual(result["scripts"], 0)
        self.assertIn("<script>", result["text"])
        self.assertEqual(self.page.evaluate("() => window.__pwned || 0"), 0)

    def test_image_error_handler_is_not_created(self):
        result = self._render('<img src=x onerror="window.__pwned = 2">')
        self.assertEqual(result["imgs"], 0)
        self.assertEqual(self.page.evaluate("() => window.__pwned || 0"), 0)

    def test_javascript_and_data_links_are_not_anchors(self):
        javascript = self._render("[x](javascript:window.__pwned=3)")
        self.assertEqual(javascript["anchors"], [])
        self.assertEqual(javascript["text"], "x")

        data = self._render("[x](data:text/html,<script>window.__pwned=4</script>)")
        self.assertEqual(data["anchors"], [])
        self.assertEqual(data["text"], "x")

        self.assertEqual(self.page.evaluate("() => window.__pwned || 0"), 0)

    def test_protocol_relative_link_is_not_an_anchor(self):
        result = self._render("[x](//evil.example.test/path)")
        self.assertEqual(result["anchors"], [])
        self.assertEqual(result["text"], "x")

    def test_http_and_mailto_links_get_safe_attributes(self):
        https = self._render("[docs](https://example.com/docs?q=1)")
        self.assertEqual(len(https["anchors"]), 1)
        self.assertEqual(https["anchors"][0]["href"], "https://example.com/docs?q=1")
        self.assertEqual(https["anchors"][0]["rel"], "noopener noreferrer")
        self.assertEqual(https["anchors"][0]["target"], "_blank")
        self.assertEqual(https["text"], "docs")

        mailto = self._render("[mail me](mailto:test@example.com)")
        self.assertEqual(len(mailto["anchors"]), 1)
        self.assertEqual(mailto["anchors"][0]["href"], "mailto:test@example.com")
        self.assertEqual(mailto["anchors"][0]["target"], "_blank")

    def test_unordered_and_ordered_lists(self):
        result = self._render("- one\n- two\n1. first\n2. second")
        self.assertEqual(result["ul"], 1)
        self.assertEqual(result["ol"], 1)
        self.assertEqual(result["items"], ["one", "two", "first", "second"])

    def test_inline_code_and_italic(self):
        result = self._render("Use `**literal**` and *italic* and _also_")
        self.assertEqual(result["code"], ["**literal**"])
        self.assertEqual(result["em"], ["italic", "also"])

    def test_snake_case_is_not_italic(self):
        result = self._render("snake_case_name")
        self.assertEqual(result["text"], "snake_case_name")
        self.assertEqual(result["em"], [])

    def test_streaming_prefixes_do_not_duplicate(self):
        first = self._render("23 × 17 = ")
        self.assertEqual(first["text"], "23 × 17 = ")
        self.assertEqual(first["strong"], [])

        second = self._render("23 × 17 = **39")
        self.assertEqual(second["text"], "23 × 17 = **39")
        self.assertEqual(second["strong"], [])

        third = self._render("23 × 17 = **391**")
        self.assertEqual(third["text"], "23 × 17 = 391")
        self.assertEqual(third["strong"], ["391"])
        self.assertNotIn("391391", third["text"])
        self.assertEqual(third["literal_marker_count"], 0)

    def test_nested_bold_and_italic(self):
        result = self._render("**bold *italic***")
        self.assertEqual(result["text"], "bold italic")
        self.assertEqual(result["strong"], ["bold italic"])
        self.assertEqual(result["em"], ["italic"])

    def test_unclosed_markers_stay_literal(self):
        result = self._render("a **b")
        self.assertEqual(result["text"], "a **b")
        self.assertEqual(result["strong"], [])

    def test_non_string_input_is_handled(self):
        self.assertEqual(self._render(None)["text"], "")
        self.assertEqual(self._render(123)["text"], "123")


class MarkdownSourceGuardTest(unittest.TestCase):
    """Fast source-level guards that keep the security properties."""

    HTML_SINKS = (
        "innerHTML",
        "insertAdjacentHTML",
        "outerHTML",
        "document.write",
        "createContextualFragment",
        "eval(",
        "new Function",
    )

    def test_renderer_has_no_html_sinks(self):
        for sink in self.HTML_SINKS:
            self.assertNotIn(
                sink, RENDER_JS, msg=f"markdown-render.js must not contain {sink!r}"
            )

    def test_renderer_exposes_exactly_one_global(self):
        self.assertIn("window.renderMarkdownInto = renderMarkdownInto", RENDER_JS)

    def test_app_js_inner_html_is_only_the_loader(self):
        offenders = [
            line
            for line in APP_JS.splitlines()
            if ".innerHTML" in line and "loader" not in line
        ]
        self.assertEqual(offenders, [])

    def test_index_loads_the_renderer_before_app(self):
        renderer_position = INDEX_HTML.find("/markdown-render.js")
        app_position = INDEX_HTML.find("/app.js")
        self.assertNotEqual(renderer_position, -1)
        self.assertNotEqual(app_position, -1)
        self.assertLess(renderer_position, app_position)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
