"""Behavioural UI tests for the Notification watches panel (day 20).

The real ``static/index.html`` and its scripts are loaded in a real headless
browser through request interception (no server, no network, no model), and every
API answer is a deterministic in-process stub. Because the tests drive the real
DOM handlers of ``static/app.js``, they fail on the reported regression — a watch
created by a finished turn that only appeared after a page reload — without a
live backend.

The class skips itself when Playwright or a system Chromium-based browser is not
available, exactly like ``tests/test_link_style_ui.py``.
"""

from __future__ import annotations

import json
import unittest

from harness.qa_bridge import PROJECT_DIR, qa_browser

STATIC_DIR = (PROJECT_DIR / "static").resolve()
UI_ORIGIN = "http://ui.test"

CHAT_A = {"id": "chat-a", "title": "Chat A"}
CHAT_B = {"id": "chat-b", "title": "Chat B"}

EMPTY_WATCHES = {"available": True, "watches": [], "deliveries": []}

WATCH_ACTIVE = {
    "watch_id": "w-1",
    "query": "python documentation",
    "status": "active",
    "keywords": ["python"],
    "interval_seconds": 60,
    "next_check_at": "2026-01-01 00:00",
    "last_delivery": None,
}

WATCH_STOPPED = {**WATCH_ACTIVE, "watch_id": "w-2", "status": "stopped"}

# The chat stream only has to be a valid SSE turn: one delta and a done event,
# separated by the ``\n\n`` blocks the client parser expects.
STREAM_BODY = (
    'event: delta\ndata: {"text":"Done."}\n\n'
    "event: done\ndata: {}\n\n"
)


def _json(route, payload) -> None:
    route.fulfill(
        status=200,
        content_type="application/json; charset=utf-8",
        body=json.dumps(payload),
    )


def _serve_static(route, path: str) -> None:
    relative = path.lstrip("/") or "index.html"
    candidate = (STATIC_DIR / relative).resolve()
    if STATIC_DIR in candidate.parents and candidate.is_file():
        if candidate.suffix == ".html":
            content_type = "text/html; charset=utf-8"
        elif candidate.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        else:
            content_type = "application/javascript; charset=utf-8"
        route.fulfill(status=200, content_type=content_type, body=candidate.read_bytes())
        return
    route.fulfill(status=404, body="")


class _StubBackend:
    """Mutable, deterministic API answers for one page.

    ``watches`` maps a chat id to the payload of ``GET /api/chats/{id}/watches``.
    A test changes the mapping between UI actions; ``on_stream`` lets a test make
    the watch appear exactly when the chat stream is requested, which is how the
    post-turn regression is reproduced.
    """

    def __init__(self, chats, watches=None):
        self.chats = list(chats)
        self.watches = dict(watches or {})
        self.watch_requests: list = []
        self.stream_requests = 0
        self.on_stream = None

    def _watches_for(self, chat_id: str) -> dict:
        return self.watches.get(chat_id, EMPTY_WATCHES)

    def handle(self, route, path: str, method: str) -> None:
        if path == "/api/chats" and method == "GET":
            _json(
                route,
                {
                    "chats": self.chats,
                    "count": len(self.chats),
                    "limit": 5,
                    "limit_reached": False,
                },
            )
            return
        if path == "/api/mcp/servers" and method == "GET":
            _json(
                route,
                {
                    "servers": [{"label": "A", "connected": True, "tools_count": 9}],
                    "checked_at": 0,
                },
            )
            return
        if path == "/api/chat/stream" and method == "POST":
            self.stream_requests += 1
            if self.on_stream is not None:
                self.on_stream()
            route.fulfill(
                status=200,
                content_type="text/event-stream; charset=utf-8",
                body=STREAM_BODY,
            )
            return
        if path.startswith("/api/chats/"):
            chat_id, _, sub = path[len("/api/chats/") :].partition("/")
            if sub == "messages":
                _json(route, {"messages": []})
                return
            if sub == "tasks":
                _json(route, {"tasks": []})
                return
            if sub == "reports":
                _json(route, {"reports": []})
                return
            if sub == "watches":
                self.watch_requests.append(chat_id)
                _json(route, self._watches_for(chat_id))
                return
        route.fulfill(status=404, body='{"detail":"not found"}')


class NotificationWatchesBehaviourTest(unittest.TestCase):
    """The real page runs against a stubbed API in a real browser."""

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise unittest.SkipTest(f"playwright is not installed: {exc}")

        cls._manager = None
        cls._browser = None
        try:
            cls._manager = sync_playwright().start()
            cls._browser, _channel = qa_browser.launch_browser(
                cls._manager, headless=True
            )
        except qa_browser.PrerequisiteError as exc:
            cls._shutdown()
            raise unittest.SkipTest(
                f"no system Chromium-based browser for the DOM tests: {exc}"
            )

    @classmethod
    def _shutdown(cls):
        try:
            if cls._browser is not None:
                cls._browser.close()
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

    def setUp(self):
        # A fresh context keeps localStorage (the remembered active chat) empty.
        self._context = self._browser.new_context()
        self._context.set_default_timeout(15000)
        self._page = self._context.new_page()

    def tearDown(self):
        try:
            self._context.close()
        except Exception:  # noqa: BLE001 - best effort teardown
            pass

    # -- helpers ------------------------------------------------------------

    def _open(self, chats=(CHAT_A,), watches=None) -> _StubBackend:
        backend = _StubBackend(chats, watches)
        page = self._page

        def handler(route):
            path = route.request.url.split(UI_ORIGIN, 1)[1].split("?", 1)[0]
            if path.startswith("/api/"):
                backend.handle(route, path, route.request.method)
                return
            _serve_static(route, path)

        page.route(f"{UI_ORIGIN}/**", handler)
        page.goto(f"{UI_ORIGIN}/index.html", wait_until="load")
        page.wait_for_selector("#chats-list .chat-item", timeout=15000)
        return backend

    def _wait_empty_state(self):
        self._page.wait_for_function(
            """() => {
                const el = document.querySelector('#watches-list');
                return !!el && el.textContent.includes(
                    'No notification watches in this chat.'
                );
            }""",
            timeout=15000,
        )

    def _wait_card(self, count=1):
        self._page.wait_for_function(
            "(n) => document.querySelectorAll('#watches-list .watch-card').length === n",
            arg=count,
            timeout=15000,
        )

    def _card_count(self) -> int:
        return self._page.locator("#watches-list .watch-card").count()

    # -- tests --------------------------------------------------------------

    def test_watch_created_by_a_finished_turn_appears_without_reload(self):
        backend = self._open()
        self._wait_empty_state()
        self.assertEqual(self._card_count(), 0)

        # The watch becomes visible only once the turn's stream is requested.
        backend.on_stream = lambda: backend.watches.__setitem__(
            CHAT_A["id"],
            {"available": True, "watches": [WATCH_ACTIVE], "deliveries": []},
        )
        self._page.fill("#message-input", "Create a notification watch.")
        self._page.click("#send-button")

        # No Refresh click and no reload: the post-turn reload must show it.
        self._wait_card(1)
        self.assertEqual(self._card_count(), 1)
        self.assertEqual(backend.stream_requests, 1)
        self.assertGreaterEqual(
            len(backend.watch_requests),
            2,
            "the finished turn must re-read the watches endpoint",
        )

    def test_refresh_shows_a_new_watch_without_reload(self):
        backend = self._open()
        self._wait_empty_state()
        self.assertEqual(self._card_count(), 0)

        backend.watches[CHAT_A["id"]] = {
            "available": True,
            "watches": [WATCH_ACTIVE],
            "deliveries": [],
        }
        self._page.click("#watches-refresh-button")
        self._wait_card(1)
        self.assertEqual(self._card_count(), 1)

    def test_switching_chats_isolates_the_watches_panel(self):
        self._open(
            chats=(CHAT_A, CHAT_B),
            watches={
                CHAT_A["id"]: {
                    "available": True,
                    "watches": [WATCH_ACTIVE],
                    "deliveries": [],
                },
                CHAT_B["id"]: EMPTY_WATCHES,
            },
        )
        self._wait_card(1)

        self._page.click(
            '#chats-list .chat-item[data-chat-id="chat-b"] .chat-select'
        )
        self._page.wait_for_selector(
            '#chats-list .chat-item.active[data-chat-id="chat-b"]'
        )
        self._wait_empty_state()
        self.assertEqual(self._card_count(), 0)

        self._page.click(
            '#chats-list .chat-item[data-chat-id="chat-a"] .chat-select'
        )
        self._wait_card(1)
        self.assertEqual(self._card_count(), 1)

    def test_stopped_watch_status_is_shown(self):
        self._open(
            watches={
                CHAT_A["id"]: {
                    "available": True,
                    "watches": [WATCH_STOPPED],
                    "deliveries": [],
                }
            }
        )
        self._wait_card(1)
        status = (
            self._page.locator("#watches-list .watch-card .pill").first.text_content()
        )
        self.assertEqual(status.strip(), "stopped")

    def test_unavailable_notifier_shows_the_unavailable_state(self):
        self._open(
            watches={
                CHAT_A["id"]: {
                    "available": False,
                    "error": {
                        "category": "notifier_unavailable",
                        "message": "server B is down",
                    },
                }
            }
        )
        self._page.wait_for_function(
            "() => document.querySelector('#watches-list')"
            ".textContent.includes('unavailable')",
            timeout=15000,
        )
        self.assertEqual(self._card_count(), 0)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
