"""Unit tests of the MCP-schema → OpenAI-tools conversion and validation."""

from __future__ import annotations

import unittest

from agent.mcp_adapter import McpTool
from agent import tool_schema


def _tool(name="calculate", description="Do math", schema=None):
    return McpTool(
        name=name,
        title=None,
        description=description,
        input_schema=schema
        if schema is not None
        else {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["add", "divide"]},
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["operation", "a", "b"],
        },
    )


class ToOpenAiToolsTest(unittest.TestCase):
    """MCP tools become a valid OpenAI ``tools`` payload."""

    def test_basic_conversion(self):
        converted = tool_schema.to_openai_tools([_tool()])
        self.assertEqual(len(converted), 1)
        function = converted[0]["function"]
        self.assertEqual(converted[0]["type"], "function")
        self.assertEqual(function["name"], "calculate")
        self.assertEqual(function["description"], "Do math")
        self.assertEqual(function["parameters"]["type"], "object")
        self.assertEqual(function["parameters"]["required"], ["operation", "a", "b"])

    def test_invalid_tool_name_is_dropped(self):
        converted = tool_schema.to_openai_tools([_tool(name="bad name!")])
        self.assertEqual(converted, [])

    def test_duplicate_names_are_deduplicated(self):
        converted = tool_schema.to_openai_tools([_tool(), _tool()])
        self.assertEqual(len(converted), 1)

    def test_empty_description_gets_a_fallback(self):
        converted = tool_schema.to_openai_tools([_tool(description="")])
        self.assertIn("calculate", converted[0]["function"]["description"])

    def test_missing_schema_degrades_to_an_empty_object(self):
        tool = McpTool(name="x", title=None, description="d", input_schema=None)
        converted = tool_schema.to_openai_tools([tool])
        self.assertEqual(
            converted[0]["function"]["parameters"], {"type": "object", "properties": {}}
        )

    def test_unknown_schema_keywords_are_dropped(self):
        schema = {
            "type": "object",
            "properties": {"a": {"type": "number", "$comment": "drop me"}},
            "additionalProperties": False,
            "$defs": {"x": {}},
        }
        cleaned = tool_schema.sanitize_schema(schema)
        self.assertNotIn("$defs", cleaned)
        self.assertNotIn("$comment", cleaned["properties"]["a"])

    def test_unknown_type_is_dropped(self):
        cleaned = tool_schema.sanitize_schema(
            {"type": "object", "properties": {"a": {"type": "weird"}}}
        )
        self.assertEqual(cleaned["properties"]["a"], {})

    def test_deep_schema_is_bounded(self):
        schema = {"type": "object", "properties": {"a": {"type": "object", "properties": {"b": {"type": "object", "properties": {"c": {"type": "object", "properties": {"d": {"type": "number"}}}}}}}}}
        cleaned = tool_schema.sanitize_schema(schema)
        self.assertIsInstance(cleaned, dict)

    def test_non_object_root_schema_becomes_an_object(self):
        converted = tool_schema.to_openai_tools([_tool(schema={"type": "string"})])
        self.assertEqual(
            converted[0]["function"]["parameters"], {"type": "object", "properties": {}}
        )


class ParseArgumentsTest(unittest.TestCase):
    """Model arguments are parsed defensively."""

    def test_valid_json_object(self):
        self.assertEqual(
            tool_schema.parse_arguments('{"a": 1}'), {"a": 1}
        )

    def test_empty_string_is_an_empty_object(self):
        self.assertEqual(tool_schema.parse_arguments(""), {})

    def test_none_is_an_empty_object(self):
        self.assertEqual(tool_schema.parse_arguments(None), {})

    def test_broken_json_raises_value_error(self):
        with self.assertRaises(ValueError):
            tool_schema.parse_arguments('{"a": ')

    def test_non_object_json_raises_value_error(self):
        with self.assertRaises(ValueError):
            tool_schema.parse_arguments("[1, 2]")

    def test_non_string_input_raises_value_error(self):
        with self.assertRaises(ValueError):
            tool_schema.parse_arguments(42)


class ValidateArgumentsTest(unittest.TestCase):
    """Arguments are checked against the sanitized schema."""

    def setUp(self):
        self.schema = tool_schema.sanitize_schema(_tool().input_schema)

    def test_valid_arguments_pass_through(self):
        validated = tool_schema.validate_arguments(
            self.schema, {"operation": "add", "a": 1, "b": 2}
        )
        self.assertEqual(validated, {"operation": "add", "a": 1, "b": 2})

    def test_unknown_property_is_dropped(self):
        validated = tool_schema.validate_arguments(
            self.schema, {"operation": "add", "a": 1, "b": 2, "extra": "x"}
        )
        self.assertNotIn("extra", validated)

    def test_missing_required_property_raises(self):
        with self.assertRaises(ValueError):
            tool_schema.validate_arguments(self.schema, {"operation": "add"})

    def test_wrong_type_raises(self):
        with self.assertRaises(ValueError):
            tool_schema.validate_arguments(
                self.schema, {"operation": "add", "a": "one", "b": 2}
            )

    def test_schema_without_properties_passes_through(self):
        self.assertEqual(
            tool_schema.validate_arguments({"type": "object"}, {"anything": 1}),
            {"anything": 1},
        )


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
