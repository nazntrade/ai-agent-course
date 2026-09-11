"""Pure, Streamlit-free helpers for the chat UI.

These helpers keep the UI logic that must be unit-tested out of ``app.py``,
which imports Streamlit and is therefore hard to test in isolation. Nothing
here imports Streamlit or touches storage, so it can be tested directly.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent import AgentConfig


def configs_equal(current: AgentConfig, updated: AgentConfig) -> bool:
    """Return True when two configs are semantically identical.

    ``demo_context_limit`` is normalized on both sides so that ``None`` and
    ``0`` both mean "disabled": this avoids a spurious config save on every
    rerun when the demo limit is turned off. ``temperature`` is compared with
    a small tolerance to survive float round-trips through the UI widget.
    Every other field is compared exactly.
    """
    if current.model != updated.model:
        return False
    if current.system_prompt != updated.system_prompt:
        return False
    if current.max_tokens != updated.max_tokens:
        return False
    if bool(current.stream) != bool(updated.stream):
        return False
    if abs(current.temperature - updated.temperature) > 1e-9:
        return False
    if (current.demo_context_limit or None) != (updated.demo_context_limit or None):
        return False
    return True


class WaitingIndicator:
    """Show a spinner while waiting, then replace it with streamed text.

    The spinner is a context manager entered immediately, so the indicator is
    visible before the first chunk. ``ExitStack.close()`` is idempotent, so the
    spinner can be closed both by the first chunk and by an error path.
    ``placeholder`` receives the streamed text (a Streamlit skeleton/empty
    placeholder in the app, a fake in tests).
    """

    def __init__(self, spinner, placeholder):
        self._stack = ExitStack()
        self._stack.enter_context(spinner)
        self._placeholder = placeholder

    def show_chunk(self, text: str) -> None:
        """Close the spinner and replace the placeholder with accumulated text."""
        self._stack.close()
        self._placeholder.markdown(text)

    def clear(self) -> None:
        """Close the spinner and remove the placeholder (error path)."""
        self._stack.close()
        self._placeholder.empty()
