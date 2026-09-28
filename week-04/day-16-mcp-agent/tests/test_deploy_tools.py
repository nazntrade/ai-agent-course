"""The VPS health gate must reject a stale MCP service with too few tools."""

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.check_deploy_tools import (  # noqa: E402
    registered_tool_names,
    servers_match,
    tools_match,
)


class DeployToolContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "mcp_server" / "server.py").read_text(encoding="utf-8")
        cls.expected = registered_tool_names(cls.source)

    def response(self, names):
        tools = [{"name": name} for name in names]
        return {"connected": True, "count": len(tools), "tools": tools}

    def test_deploy_accepts_all_registered_tools_in_any_order(self):
        self.assertTrue({"search_web", "digest_search_results", "save_report"} <= self.expected)
        self.assertTrue(tools_match(self.source, self.response(sorted(self.expected, reverse=True))))

    def test_deploy_rejects_stale_service_even_with_three_tools(self):
        stale = {"calculate", "get_server_info", "search_web"}
        self.assertFalse(tools_match(self.source, self.response(stale)))
        self.assertFalse(tools_match(self.source, self.response(self.expected - {"save_report"})))

    def test_deploy_rejects_bad_status_and_duplicate_or_extra_tools(self):
        complete = self.response(sorted(self.expected))
        for change in ({"connected": False}, {"count": 1}):
            self.assertFalse(tools_match(self.source, {**complete, **change}))
        self.assertFalse(tools_match(self.source, self.response(sorted(self.expected) + ["save_report"])))
        self.assertFalse(tools_match(self.source, self.response(sorted(self.expected) + ["unknown_tool"])))

    def test_unsupported_registration_shape_fails_closed(self):
        changed = self.source.replace("server.tool()(tools.save_report)", "for tool in dynamic_tools:\n            server.tool()(tool)")
        with self.assertRaises(ValueError):
            registered_tool_names(changed)


class DeployServersContractTests(unittest.TestCase):
    """The day-20 gate checks both MCP servers through /api/mcp/servers."""

    @classmethod
    def setUpClass(cls):
        cls.a_source = (ROOT / "mcp_server" / "server.py").read_text(encoding="utf-8")
        cls.b_source = (ROOT / "notifier_server" / "server.py").read_text(encoding="utf-8")
        cls.sources = {"A": cls.a_source, "B": cls.b_source}

    def servers(self, a_names, b_names, *, a_connected=True, b_connected=True, labels=("A", "B")):
        entries = [
            {
                "label": labels[0],
                "connected": a_connected,
                "tool_names": list(a_names),
            },
            {
                "label": labels[1],
                "connected": b_connected,
                "tool_names": list(b_names),
            },
        ]
        return {"servers": entries, "checked_at": 0}

    def test_accepts_both_servers_with_matching_tool_names(self):
        a_expected = sorted(registered_tool_names(self.a_source))
        b_expected = sorted(registered_tool_names(self.b_source))
        response = self.servers(a_expected, b_expected)
        self.assertTrue(servers_match(response, self.sources))
        # Order inside ``tool_names`` must not matter.
        response = self.servers(list(reversed(a_expected)), list(reversed(b_expected)))
        self.assertTrue(servers_match(response, self.sources))

    def test_rejects_disconnected_server(self):
        a_expected = sorted(registered_tool_names(self.a_source))
        b_expected = sorted(registered_tool_names(self.b_source))
        self.assertFalse(
            servers_match(self.servers(a_expected, b_expected, b_connected=False), self.sources)
        )
        self.assertFalse(
            servers_match(self.servers(a_expected, b_expected, a_connected=False), self.sources)
        )

    def test_rejects_stale_or_extra_tool_names(self):
        a_expected = sorted(registered_tool_names(self.a_source))
        b_expected = sorted(registered_tool_names(self.b_source))
        self.assertFalse(
            servers_match(
                self.servers(a_expected, b_expected[:-1]), self.sources
            )
        )
        self.assertFalse(
            servers_match(
                self.servers(a_expected + ["bogus_tool"], b_expected), self.sources
            )
        )
        self.assertFalse(
            servers_match(
                self.servers(a_expected, b_expected + [b_expected[0]]), self.sources
            )
        )

    def test_rejects_missing_or_duplicated_labels(self):
        a_expected = sorted(registered_tool_names(self.a_source))
        b_expected = sorted(registered_tool_names(self.b_source))
        only_a = {"servers": [{"label": "A", "connected": True, "tool_names": a_expected}]}
        self.assertFalse(servers_match(only_a, self.sources))
        duplicated = self.servers(a_expected, b_expected, labels=("A", "A"))
        self.assertFalse(servers_match(duplicated, self.sources))
        self.assertFalse(servers_match({"servers": []}, self.sources))
        self.assertFalse(servers_match({"servers": "no"}, self.sources))


if __name__ == "__main__":
    unittest.main()
