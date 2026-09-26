"""The VPS health gate must reject a stale MCP service with too few tools."""

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.check_deploy_tools import registered_tool_names, tools_match  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
