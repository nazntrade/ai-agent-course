"""Playwright browser session and Streamlit widget helpers.

A real browser is used (Microsoft Edge or Google Chrome through the Playwright
``channel`` option); the Playwright browser bundle is never downloaded, because
a system browser is expected on the machine. Elements are addressed by the
Streamlit widget-key class ``st-key-<key>`` exactly like the application's own
CSS; radio options and expanders are addressed by their visible text.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from lib.modes import PrerequisiteError

CHROMIUM_CHANNELS = ("msedge", "chrome")

DEFAULT_TIMEOUT_MS = 30000
DEFAULT_VIEWPORT = {"width": 1680, "height": 1050}


def key_selector(key) -> str:
    """CSS selector of the Streamlit container carrying a widget key."""
    return f'div[class~="st-key-{key}"]'


def key_locator(page, key):
    return page.locator(key_selector(key))


class BrowserSession:
    """A live Playwright browser, context and page."""

    def __init__(self, manager, browser, context, page, channel):
        self._manager = manager
        self.browser = browser
        self.context = context
        self.page = page
        self.channel = channel

    def close(self) -> None:
        for closer in (self.context, self.browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:
                pass
        try:
            if self._manager is not None:
                self._manager.stop()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def launch_browser(playwright, *, headless=True, timeout_ms=DEFAULT_TIMEOUT_MS):
    """Launch a system Chromium-based browser, preferring Edge over Chrome."""
    errors = []
    for channel in CHROMIUM_CHANNELS:
        try:
            return (
                playwright.chromium.launch(channel=channel, headless=headless),
                channel,
            )
        except Exception as exc:  # noqa: BLE001 - reported as one prerequisite
            errors.append(f"{channel}: {exc}")
    raise PrerequisiteError(
        "no system Chromium-based browser (msedge/chrome) could be launched: "
        + "; ".join(errors)
    )


def open_session(
    url,
    *,
    headless=True,
    timeout_ms=DEFAULT_TIMEOUT_MS,
    viewport=None,
    wait_for="ui_mode",
) -> BrowserSession:
    """Open a browser session on ``url`` and wait for the app to render."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - reported as a prerequisite
        raise PrerequisiteError(
            f"playwright is not installed in this environment: {exc}"
        ) from exc

    manager = sync_playwright().start()
    try:
        browser, channel = launch_browser(manager, headless=headless, timeout_ms=timeout_ms)
        context = browser.new_context(
            viewport=viewport or DEFAULT_VIEWPORT, ignore_https_errors=True
        )
        context.set_default_timeout(timeout_ms)
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        wait_for_key(page, wait_for, timeout=timeout_ms)
        return BrowserSession(manager, browser, context, page, channel)
    except Exception:
        try:
            manager.stop()
        except Exception:
            pass
        raise


def wait_for_key(page, key, timeout=DEFAULT_TIMEOUT_MS):
    """Wait until the widget container of ``key`` is visible."""
    locator = key_locator(page, key).first
    locator.wait_for(state="visible", timeout=timeout)
    return locator


def key_visible(page, key, timeout=5000) -> bool:
    """Whether the widget container of ``key`` becomes visible in time."""
    try:
        key_locator(page, key).first.wait_for(state="visible", timeout=timeout)
        return True
    except Exception:
        return False


def click_key(page, key, timeout=DEFAULT_TIMEOUT_MS, retries=3) -> None:
    """Click the button inside the widget container of ``key``."""
    last_error = None
    for _attempt in range(max(retries, 1)):
        try:
            button = key_locator(page, key).locator("button").first
            button.wait_for(state="visible", timeout=timeout)
            button.click(timeout=timeout)
            return
        except Exception as exc:  # noqa: BLE001 - a Streamlit rerun can detach
            last_error = exc
            time.sleep(0.5)
    raise last_error


def button_enabled(page, key) -> bool:
    """Whether the key's button exists and is not disabled."""
    button = key_locator(page, key).locator("button").first
    try:
        if button.count() == 0:
            return False
        return button.is_enabled()
    except Exception:
        return False


def fill_key(page, key, value, *, area=False, timeout=DEFAULT_TIMEOUT_MS) -> None:
    """Fill a Streamlit text input or text area identified by its key."""
    selector = "textarea" if area else "input"
    field = key_locator(page, key).locator(selector).first
    field.wait_for(state="visible", timeout=timeout)
    field.fill(str(value))


def click_text(page, text, *, exact=False, timeout=DEFAULT_TIMEOUT_MS) -> None:
    """Click a visible element by its text (radios, expander headers)."""
    locator = page.get_by_text(text, exact=exact).first
    locator.wait_for(state="visible", timeout=timeout)
    locator.click(timeout=timeout)


def click_button(page, text, *, timeout=DEFAULT_TIMEOUT_MS) -> None:
    """Click a button by its accessible name (used for keyless buttons)."""
    locator = page.get_by_role("button", name=re.compile(re.escape(text))).first
    locator.wait_for(state="visible", timeout=timeout)
    locator.click(timeout=timeout)


def select_radio(page, group_key, option_text, timeout=DEFAULT_TIMEOUT_MS) -> None:
    """Select one option of a Streamlit radio group by its label text."""
    group = key_locator(page, group_key).first
    group.wait_for(state="visible", timeout=timeout)
    option = group.locator("label").filter(has_text=option_text).first
    option.wait_for(state="visible", timeout=timeout)
    option.click(timeout=timeout)


def open_expander(page, title, timeout=DEFAULT_TIMEOUT_MS) -> None:
    """Expand a Streamlit expander by its summary text."""
    summary = page.locator("details summary").filter(has_text=title).first
    if summary.count() == 0:
        summary = page.get_by_text(title, exact=True).first
    summary.wait_for(state="visible", timeout=timeout)
    summary.click(timeout=timeout)


def wait_for_text(page, text, timeout=DEFAULT_TIMEOUT_MS, interval=0.25):
    """Wait until an element containing ``text`` is visible."""
    deadline = time.monotonic() + timeout / 1000.0
    pattern = re.compile(re.escape(text))
    while time.monotonic() < deadline:
        try:
            if page.get_by_text(pattern).first.is_visible():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def text_visible(page, text) -> bool:
    """Whether an element containing ``text`` is currently visible."""
    try:
        return page.get_by_text(re.compile(re.escape(text))).first.is_visible()
    except Exception:
        return False


def screenshot(page, path) -> Path:
    """Write a full-page screenshot into the run directory."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(target), full_page=True)
    return target
