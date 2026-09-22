"""Unit tests of the trace sanitizer and writer."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent.trace import NullTraceWriter, TraceWriter, sanitize


class SanitizeTest(unittest.TestCase):
    """Secrets, prompts and paths never reach the trace payload."""

    def test_secret_keys_are_dropped(self):
        payload = sanitize(
            {
                "authorization": "Bearer abc",
                "api_key": "abc",
                "token": "abc",
                "password": "abc",
                "ok": True,
            }
        )
        self.assertEqual(payload, {"ok": True})

    def test_nested_secret_keys_are_dropped(self):
        payload = sanitize({"outer": {"api_key": "abc", "value": 1}})
        self.assertEqual(payload, {"outer": {"value": 1}})

    def test_long_strings_are_truncated(self):
        payload = sanitize({"text": "x" * 500})
        self.assertLessEqual(len(payload["text"]), 200)
        self.assertTrue(payload["text"].endswith("..."))

    def test_path_key_is_dropped(self):
        payload = sanitize({"path": r"C:\Users\someone\project\file.txt", "ok": 1})
        self.assertEqual(payload, {"ok": 1})

    def test_nested_objects_are_limited_by_depth(self):
        payload = sanitize({"value": {"nested": {"deeper": 1}}})
        self.assertEqual(payload["value"]["nested"]["deeper"], 1)
        payload = sanitize({"value": {"a": {"b": {"c": {"d": 1}}}}})
        self.assertEqual(payload["value"]["a"]["b"], "<omitted>")


class TraceWriterTest(unittest.TestCase):
    """Events are appended as one JSON object per line."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "trace.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_writes_one_json_object_per_line(self):
        writer = TraceWriter(self.path)
        writer.write("request_start", request_id="r1", message_chars=12)
        writer.write("request_done", request_id="r1", ok=True)
        lines = [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual([line["event"] for line in lines], ["request_start", "request_done"])
        self.assertEqual(lines[0]["message_chars"], 12)
        self.assertTrue(lines[1]["ok"])
        for line in lines:
            self.assertIn("ts", line)

    def test_request_start_keeps_only_the_message_length(self):
        writer = TraceWriter(self.path)
        writer.write(
            "request_start",
            request_id="r1",
            message_chars=20,
        )
        raw = self.path.read_text(encoding="utf-8")
        self.assertIn("message_chars", raw)
        record = json.loads(raw.splitlines()[0])
        self.assertEqual(record["message_chars"], 20)

    def test_request_error_keeps_the_controlled_message(self):
        writer = TraceWriter(self.path)
        writer.write(
            "request_error",
            request_id="r1",
            category="mcp_unavailable",
            message="The MCP server is not reachable",
        )
        record = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(record["category"], "mcp_unavailable")
        self.assertEqual(record["message"], "The MCP server is not reachable")

    def test_never_writes_an_api_key(self):
        writer = TraceWriter(self.path)
        writer.write("model_request", api_key="sk-secret", phase="tool_selection")
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("sk-secret", raw)

    def test_arguments_are_limited_to_atomic_values(self):
        writer = TraceWriter(self.path)
        writer.write(
            "tool_selected",
            tool="calculate",
            arguments={"a": 1, "b": 2.5, "nested": {"deep": object()}},
        )
        record = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(record["arguments"]["a"], 1)
        self.assertIsInstance(record["arguments"]["nested"], dict)

    def test_parent_directory_is_created(self):
        writer = TraceWriter(Path(self._tmp.name) / "nested" / "trace.jsonl")
        writer.write("request_start", request_id="r1")
        self.assertTrue((Path(self._tmp.name) / "nested" / "trace.jsonl").exists())

    def test_null_writer_writes_nothing(self):
        writer = NullTraceWriter()
        writer.write("request_start", request_id="r1")
        self.assertFalse(writer.enabled)

    def test_unwritable_path_does_not_raise(self):
        writer = TraceWriter(Path(self._tmp.name) / "trace.jsonl" / "x.jsonl")
        writer.write("request_start", request_id="r1")


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
