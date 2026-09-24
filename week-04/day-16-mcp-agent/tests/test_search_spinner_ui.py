"""Regression tests for the web-search spinner in the chat UI.

The real ``static/index.html``, ``static/app.js`` and ``static/styles.css`` are
loaded in a real headless browser (served from disk, no network). The chat
endpoint is replaced by a scripted SSE stream installed inside the page, so a
web-search call can be delayed and failed deterministically without an MCP
server, a model, a key or a socket.

``SearchSpinnerSourceGuardTest`` is a fast, browser-free guard that still
catches removed wiring when no system browser is installed.
"""

from __future__ import annotations

import unittest

from harness.qa_bridge import PROJECT_DIR, qa_browser

STATIC_DIR = (PROJECT_DIR / "static").resolve()
APP_JS = (PROJECT_DIR / "static" / "app.js").read_text(encoding="utf-8")
STYLES_CSS = (PROJECT_DIR / "static" / "styles.css").read_text(encoding="utf-8")

ACTIVE_SPINNER = ".bubble.assistant .search-spinner.active"

# Each script item is ``{"wait": ms}`` or ``{"event": name, "data": {...}}``.
# The wire format is the real one: ``event: <name>\ndata: <json>\n\n``.
INSTALL_FAKE_STREAM = r"""
(script) => {
  const encoder = new TextEncoder();
  window.fetch = async (url) => {
    if (!String(url).includes("/api/chat/stream")) {
      return new Response("not found", { status: 404 });
    }
    const stream = new ReadableStream({
      async start(controller) {
        for (const step of script) {
          if (typeof step.wait === "number") {
            await new Promise((resolve) => setTimeout(resolve, step.wait));
            continue;
          }
          controller.enqueue(
            encoder.encode(
              "event: " + step.event + "\ndata: " + JSON.stringify(step.data) + "\n\n"
            )
          );
        }
        controller.close();
      },
    });
    return new Response(stream, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
  };
}
"""

SPINNER_STYLE = r"""
() => {
  const spinner = document.querySelector(".bubble.assistant .search-spinner.active");
  if (!spinner) {
    return { found: false };
  }
  const style = window.getComputedStyle(spinner);
  return {
    found: true,
    display: style.display,
    animation_name: style.animationName,
    animations: spinner.getAnimations().length,
    aria_label: spinner.getAttribute("aria-label"),
    tag_name: spinner.querySelector("svg") ? "svg" : "none",
  };
}
"""


def tool_call(round_index: int, query: str = "python documentation") -> dict:
    """One ``tool_call`` SSE event for the web-search MCP tool."""
    return {
        "event": "tool_call",
        "data": {
            "tool": "search_web",
            "arguments": {"query": query},
            "round": round_index,
        },
    }


def tool_result(ok: bool, summary: str = "2 results") -> dict:
    """One ``tool_result`` SSE event for the web-search MCP tool."""
    return {
        "event": "tool_result",
        "data": {
            "tool": "search_web",
            "ok": ok,
            "summary": summary,
            "duration_ms": 1200,
        },
    }


def done_event() -> dict:
    """The closing ``done`` SSE event of a successful stream."""
    return {
        "event": "done",
        "data": {"request_id": "r1", "finish_reason": "stop", "total_ms": 1600},
    }


class SearchSpinnerBrowserTest(unittest.TestCase):
    """The real page, script and stylesheet are exercised in a real browser."""

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise unittest.SkipTest(f"playwright is not installed: {exc}")

        cls._manager = None
        cls._browser = None
        cls._context = None
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

    def _open(self):
        """Load the real UI with the real static files served from disk."""
        errors: list = []
        page = self._context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))

        def handler(route):
            path = route.request.url.split("spinner.test", 1)[1].split("?", 1)[0]
            path = path.lstrip("/") or "index.html"
            candidate = (STATIC_DIR / path).resolve()
            if STATIC_DIR in candidate.parents and candidate.is_file():
                content_type = (
                    "text/html; charset=utf-8"
                    if candidate.suffix == ".html"
                    else "text/css; charset=utf-8"
                    if candidate.suffix == ".css"
                    else "application/javascript; charset=utf-8"
                )
                route.fulfill(
                    status=200, content_type=content_type, body=candidate.read_bytes()
                )
                return
            route.fulfill(status=404, body="")

        page.route("http://spinner.test/**", handler)
        page.goto("http://spinner.test/index.html", wait_until="load")
        page.wait_for_selector("#message-input")
        return page, errors

    def _send(self, page, script):
        page.evaluate(INSTALL_FAKE_STREAM, script)
        page.fill("#message-input", "please search the web")
        page.click("#send-button")

    def test_spinner_is_visible_and_animated_while_the_search_runs(self):
        page, errors = self._open()
        try:
            self._send(
                page,
                [
                    tool_call(1),
                    {"wait": 1500},
                    tool_result(True),
                    {"event": "delta", "data": {"text": "Two links found"}},
                    done_event(),
                ],
            )
            page.wait_for_selector(ACTIVE_SPINNER, state="visible", timeout=8000)
            style = page.evaluate(SPINNER_STYLE)
            self.assertTrue(style["found"])
            self.assertEqual(style["display"], "inline-block")
            self.assertNotEqual(style["animation_name"], "none")
            self.assertGreaterEqual(style["animations"], 1)
            self.assertEqual(style["aria_label"], "Searching the web")
            self.assertEqual(style["tag_name"], "svg")
            self.assertEqual(errors, [])
        finally:
            page.close()

    def test_spinner_is_hidden_after_the_search_completes(self):
        page, errors = self._open()
        try:
            self._send(
                page,
                [
                    tool_call(1),
                    {"wait": 1500},
                    tool_result(True),
                    {"event": "delta", "data": {"text": "Two links found"}},
                    done_event(),
                ],
            )
            page.wait_for_selector(ACTIVE_SPINNER, state="visible", timeout=8000)
            page.wait_for_selector(ACTIVE_SPINNER, state="hidden", timeout=10000)
            page.wait_for_function(
                "() => { const t = document.querySelector('.bubble.assistant .text');"
                " return !!t && t.textContent.includes('Two links found'); }",
                timeout=10000,
            )
            self.assertEqual(page.locator(ACTIVE_SPINNER).count(), 0)
            self.assertEqual(page.locator(".bubble.assistant .loader").count(), 0)
            self.assertEqual(errors, [])
        finally:
            page.close()

    def test_spinner_is_hidden_after_the_search_fails(self):
        page, errors = self._open()
        try:
            self._send(
                page,
                [
                    tool_call(1),
                    {"wait": 1500},
                    {
                        "event": "error",
                        "data": {
                            "request_id": "r1",
                            "category": "mcp_unavailable",
                            "message": "The web search service is unreachable.",
                        },
                    },
                ],
            )
            page.wait_for_selector(ACTIVE_SPINNER, state="visible", timeout=8000)
            page.wait_for_selector(".bubble.assistant.error", timeout=8000)
            page.wait_for_selector(ACTIVE_SPINNER, state="hidden", timeout=10000)
            self.assertEqual(page.locator(ACTIVE_SPINNER).count(), 0)
            self.assertIn(
                "unreachable",
                page.locator(".bubble.assistant.error .text").inner_text(),
            )
            self.assertEqual(errors, [])
        finally:
            page.close()

    def test_no_spinner_for_a_plain_answer_without_a_search(self):
        page, errors = self._open()
        try:
            self._send(
                page,
                [
                    {"event": "delta", "data": {"text": "Plain answer, no tool."}},
                    done_event(),
                ],
            )
            page.wait_for_function(
                "() => { const t = document.querySelector('.bubble.assistant .text');"
                " return !!t && t.textContent.includes('Plain answer'); }",
                timeout=8000,
            )
            self.assertEqual(
                page.locator(".bubble.assistant .search-spinner").count(), 0
            )
            self.assertEqual(errors, [])
        finally:
            page.close()

    def test_spinner_toggles_across_consecutive_search_calls(self):
        page, errors = self._open()
        try:
            self._send(
                page,
                [
                    tool_call(1),
                    {"wait": 1200},
                    tool_result(True),
                    {"wait": 1200},
                    tool_call(2, query="asyncio documentation"),
                    {"wait": 1200},
                    tool_result(True),
                    {"event": "delta", "data": {"text": "All done"}},
                    done_event(),
                ],
            )
            page.wait_for_selector(ACTIVE_SPINNER, state="visible", timeout=8000)
            page.wait_for_selector(ACTIVE_SPINNER, state="hidden", timeout=8000)
            page.wait_for_selector(ACTIVE_SPINNER, state="visible", timeout=8000)
            page.wait_for_selector(ACTIVE_SPINNER, state="hidden", timeout=8000)
            self.assertEqual(page.locator(ACTIVE_SPINNER).count(), 0)
            self.assertEqual(errors, [])
        finally:
            page.close()


class SearchSpinnerSourceGuardTest(unittest.TestCase):
    """Fast checks of the wiring that do not need a browser."""

    def test_app_js_wires_the_spinner_to_the_search_tool(self):
        self.assertIn("search_web", APP_JS)
        self.assertIn("search-spinner", APP_JS)

    def test_stylesheet_animates_the_globe(self):
        self.assertIn(".search-spinner", STYLES_CSS)
        self.assertIn("@keyframes search-spin", STYLES_CSS)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
