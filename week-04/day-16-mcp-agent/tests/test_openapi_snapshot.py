"""Drift test between the generated OpenAPI 3.1 contract and the snapshot.

The committed ``docs/openapi.json`` is the source of truth for HTTP consumers.
This test fails when the application's routes, methods, identity or component
models change without refreshing the snapshot (``test.bat openapi``).
"""

from __future__ import annotations

import json
import unittest

from harness.openapi_snapshot import SNAPSHOT_PATH, generate_spec, skeleton


class OpenApiSnapshotTest(unittest.TestCase):
    """The committed contract matches the application."""

    def test_snapshot_exists(self):
        self.assertTrue(
            SNAPSHOT_PATH.exists(),
            f"missing {SNAPSHOT_PATH.name}; run test.bat openapi to create it",
        )

    def test_generated_contract_is_openapi_3_1(self):
        spec = generate_spec()
        self.assertTrue(
            str(spec.get("openapi", "")).startswith("3.1"),
            f"expected OpenAPI 3.1, got {spec.get('openapi')}",
        )

    def test_documented_paths_and_models_match(self):
        if not SNAPSHOT_PATH.exists():
            self.skipTest("no snapshot yet")
        committed = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            skeleton(generate_spec()),
            skeleton(committed),
            "the HTTP contract drifted from docs/openapi.json",
        )

    def test_expected_endpoints_are_present(self):
        spec = generate_spec()
        paths = spec.get("paths") or {}
        for path in (
            "/api/health",
            "/api/mcp/status",
            "/api/mcp/tools",
            "/api/chat/stream",
            # Day 18: saved chats and their scheduled-task panel.
            "/api/chats",
            "/api/chats/{chat_id}",
            "/api/chats/{chat_id}/messages",
            "/api/chats/{chat_id}/clear",
            "/api/chats/{chat_id}/tasks",
        ):
            self.assertIn(path, paths)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
