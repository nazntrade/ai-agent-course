"""Unit tests of the six notifier tool wrappers (no server, no network).

The wrappers only delegate to the watch service and propagate its controlled
``ToolError``; the service itself is replaced by a scripted double so the tests
assert the exact argument mapping and the structured return contract.
"""

from __future__ import annotations

import typing
import unittest
from unittest import mock

from mcp.server.mcpserver.exceptions import ToolError

from notifier_server import tools
from notifier_server import watches as watch_module


class _StubWatchService:
    """A scripted ``WatchService`` that records every call."""

    def __init__(self):
        self.calls: list = []
        self.result: dict = {"ok": True}

    def create_watch(
        self,
        chat_id,
        query,
        keywords,
        exclude=None,
        interval_seconds=0,
        summary_interval_seconds=0,
        source_task_id="",
    ):
        self.calls.append(
            (
                "create",
                chat_id,
                query,
                keywords,
                exclude,
                interval_seconds,
                summary_interval_seconds,
                source_task_id,
            )
        )
        return self.result

    def list_watches(self, chat_id=""):
        self.calls.append(("list", chat_id))
        return self.result

    def evaluate_run(self, watch_id, run, chat_id=""):
        self.calls.append(("evaluate", watch_id, run, chat_id))
        return self.result

    def send_notification(self, watch_id, chat_id="", kind="new_items", items=None):
        self.calls.append(("send", watch_id, chat_id, kind, items))
        return self.result

    def get_delivery_status(self, watch_id="", chat_id=""):
        self.calls.append(("status", watch_id, chat_id))
        return self.result

    def stop_watch(self, watch_id, chat_id=""):
        self.calls.append(("stop", watch_id, chat_id))
        return self.result


def _patch(service):
    return mock.patch.object(
        watch_module, "default_watch_service", return_value=service
    )


class ToolDelegationTest(unittest.TestCase):
    """Each tool forwards its arguments to the service unchanged."""

    def test_create_notification_watch_delegates(self):
        service = _StubWatchService()
        with _patch(service):
            result = tools.create_notification_watch(
                "xbox news", ["Xbox"], ["used"], 60, 3600, "task-1", "chat-1"
            )
        self.assertIs(result, service.result)
        self.assertEqual(
            service.calls,
            [
                (
                    "create",
                    "chat-1",
                    "xbox news",
                    ["Xbox"],
                    ["used"],
                    60,
                    3600,
                    "task-1",
                )
            ],
        )

    def test_create_defaults_for_optional_arguments(self):
        service = _StubWatchService()
        with _patch(service):
            tools.create_notification_watch("news", ["xbox"])
        self.assertEqual(
            service.calls[0],
            ("create", "", "news", ["xbox"], [], 0, 0, ""),
        )

    def test_list_notification_watches_delegates(self):
        service = _StubWatchService()
        with _patch(service):
            result = tools.list_notification_watches("chat-1")
        self.assertIs(result, service.result)
        self.assertEqual(service.calls, [("list", "chat-1")])

    def test_list_notification_watches_defaults(self):
        service = _StubWatchService()
        with _patch(service):
            tools.list_notification_watches()
        self.assertEqual(service.calls, [("list", "")])

    def test_evaluate_run_delegates(self):
        service = _StubWatchService()
        run = {"status": "ok", "results": []}
        with _patch(service):
            result = tools.evaluate_run("w1", run, "chat-1")
        self.assertIs(result, service.result)
        self.assertEqual(service.calls, [("evaluate", "w1", run, "chat-1")])

    def test_send_notification_delegates(self):
        service = _StubWatchService()
        items = [{"title": "t", "url": "https://a.test/1"}]
        with _patch(service):
            result = tools.send_notification("w1", "chat-1", "new_items", items)
        self.assertIs(result, service.result)
        self.assertEqual(service.calls, [("send", "w1", "chat-1", "new_items", items)])

    def test_send_notification_defaults(self):
        service = _StubWatchService()
        with _patch(service):
            tools.send_notification("w1")
        self.assertEqual(service.calls, [("send", "w1", "", "new_items", [])])

    def test_get_delivery_status_delegates(self):
        service = _StubWatchService()
        with _patch(service):
            result = tools.get_delivery_status("w1", "chat-1")
        self.assertIs(result, service.result)
        self.assertEqual(service.calls, [("status", "w1", "chat-1")])

    def test_stop_notification_watch_delegates(self):
        service = _StubWatchService()
        with _patch(service):
            result = tools.stop_notification_watch("w1", "chat-1")
        self.assertIs(result, service.result)
        self.assertEqual(service.calls, [("stop", "w1", "chat-1")])


class ToolErrorTest(unittest.TestCase):
    """A controlled service failure propagates as ``ToolError``."""

    def test_tool_error_propagates(self):
        class Failing(_StubWatchService):
            def create_watch(self, *args, **kwargs):
                raise ToolError("This tool needs an active chat context")

        with _patch(Failing()):
            with self.assertRaises(ToolError) as caught:
                tools.create_notification_watch("news", ["xbox"], chat_id="")
        self.assertEqual(str(caught.exception), "This tool needs an active chat context")


class ToolContractTest(unittest.TestCase):
    """The six tools are named and annotated as the MCP contract requires."""

    TOOL_NAMES = (
        "create_notification_watch",
        "list_notification_watches",
        "evaluate_run",
        "send_notification",
        "get_delivery_status",
        "stop_notification_watch",
    )

    def test_exactly_six_named_tools_exist(self):
        for name in self.TOOL_NAMES:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(tools, name)))

    def test_returns_are_annotated_as_dicts(self):
        for name in self.TOOL_NAMES:
            with self.subTest(name=name):
                hints = typing.get_type_hints(getattr(tools, name))
                self.assertIn("return", hints)
                self.assertIn("dict", str(hints["return"]))

    def test_every_tool_has_a_model_facing_docstring(self):
        for name in self.TOOL_NAMES:
            with self.subTest(name=name):
                self.assertTrue(getattr(tools, name).__doc__.strip())


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
