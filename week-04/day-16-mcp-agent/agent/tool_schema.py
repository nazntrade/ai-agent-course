"""Conversion and validation of MCP tool schemas for the model provider.

The MCP server is an external boundary: its ``inputSchema`` values are
validated, recursively sanitized to known JSON-Schema keywords and finally
converted into the OpenAI ``tools`` payload. An unusable tool is dropped rather
than forwarded to the model, and model-provided arguments are parsed and type
checked before they reach the MCP client.
"""

from __future__ import annotations

import json
import re
from typing import Any

NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

ALLOWED_SCHEMA_KEYS = frozenset(
    {
        "type",
        "description",
        "title",
        "properties",
        "required",
        "enum",
        "items",
        "default",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "additionalProperties",
    }
)

ALLOWED_TYPES = frozenset(
    {"object", "array", "string", "number", "integer", "boolean", "null"}
)

MAX_SCHEMA_DEPTH = 6
MAX_PROPERTIES = 64
DEFAULT_SCHEMA: dict = {"type": "object", "properties": {}}

# Arguments the backend injects into a chat-scoped tool call. They stay in the
# server-side schema (so validation accepts them) but are removed from the
# model-facing schema, because the model must never see or supply them.
HIDDEN_INJECTED_ARGUMENTS = frozenset({"chat_id"})


def name_is_allowed(name: Any) -> bool:
    """Whether a tool name is safe to forward to the provider."""
    return isinstance(name, str) and bool(NAME_PATTERN.match(name))


def sanitize_schema(schema: Any, depth: int = 0) -> dict:
    """Return a sanitized copy of a JSON-Schema fragment.

    Unknown keywords are dropped, an unusable fragment degrades to an empty
    object schema, and the recursion is bounded.
    """
    if depth > MAX_SCHEMA_DEPTH or not isinstance(schema, dict):
        return dict(DEFAULT_SCHEMA) if depth == 0 else {"type": "string"}

    cleaned: dict = {}
    schema_type = schema.get("type")
    if isinstance(schema_type, str) and schema_type in ALLOWED_TYPES:
        cleaned["type"] = schema_type

    description = schema.get("description")
    if isinstance(description, str) and description:
        cleaned["description"] = description[:1000]

    enum = schema.get("enum")
    if isinstance(enum, (list, tuple)) and enum:
        cleaned["enum"] = [
            item
            for item in enum
            if isinstance(item, (str, int, float, bool)) or item is None
        ][:MAX_PROPERTIES]

    for key in ("default", "minimum", "maximum", "minLength", "maxLength"):
        value = schema.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            cleaned[key] = value

    if cleaned.get("type") == "object" or "properties" in schema:
        cleaned["type"] = "object"
        properties = schema.get("properties")
        result_properties: dict = {}
        if isinstance(properties, dict):
            for index, (key, value) in enumerate(properties.items()):
                if index >= MAX_PROPERTIES or not isinstance(key, str):
                    break
                result_properties[key] = sanitize_schema(value, depth + 1)
        cleaned["properties"] = result_properties
        required = schema.get("required")
        if isinstance(required, (list, tuple)):
            cleaned["required"] = [
                str(item)
                for item in required
                if isinstance(item, str) and item in result_properties
            ]

    if cleaned.get("type") == "array" or "items" in schema:
        cleaned["type"] = "array"
        cleaned["items"] = sanitize_schema(schema.get("items"), depth + 1)

    additional = schema.get("additionalProperties")
    if isinstance(additional, bool):
        cleaned["additionalProperties"] = additional

    return cleaned


def hide_properties(schema: dict, hidden) -> dict:
    """Remove ``hidden`` properties (and their ``required`` entries) from a schema."""
    names = frozenset(str(name) for name in (hidden or ()))
    if not names:
        return schema
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return schema
    cleaned = dict(schema)
    cleaned["properties"] = {
        key: value for key, value in properties.items() if key not in names
    }
    required = schema.get("required")
    if isinstance(required, list):
        cleaned["required"] = [name for name in required if name not in names]
    return cleaned


def to_openai_tools(tools, hidden_properties=None) -> list:
    """Convert MCP tools into the OpenAI ``tools`` payload.

    ``hidden_properties`` names server-side arguments that must not be offered
    to the model; they are dropped from ``properties`` and ``required``.
    """
    converted: list = []
    seen: set = set()
    for tool in tools or []:
        name = getattr(tool, "name", None)
        if not name_is_allowed(name) or name in seen:
            continue
        seen.add(name)
        description = (getattr(tool, "description", "") or "").strip()
        if not description:
            description = f"MCP tool {name}"
        schema = sanitize_schema(getattr(tool, "input_schema", None))
        if schema.get("type") != "object":
            schema = dict(DEFAULT_SCHEMA)
        schema = hide_properties(schema, hidden_properties)
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description[:1000],
                    "parameters": schema,
                },
            }
        )
    return converted


def parse_arguments(raw: Any) -> dict:
    """Parse model-provided tool arguments.

    Returns a dictionary or raises ``ValueError`` with a message that is safe
    to return to the model as a tool error.
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str):
        raise ValueError("Tool arguments must be a JSON object")
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("Tool arguments are not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Tool arguments must be a JSON object")
    return parsed


def validate_arguments(schema: Any, arguments: dict) -> dict:
    """Check required properties and basic types against ``schema``.

    Only the subset of JSON Schema produced by :func:`sanitize_schema` is
    enforced. Unknown extra properties are dropped, so a model cannot smuggle
    arbitrary payloads into the tool call.
    """
    if not isinstance(schema, dict):
        return dict(arguments)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return dict(arguments)

    validated: dict = {}
    for name, value in (arguments or {}).items():
        if name not in properties:
            continue
        validated[name] = value

    for name in schema.get("required") or []:
        if name not in validated:
            raise ValueError(f"Missing required argument '{name}'")

    for name, value in validated.items():
        spec = properties.get(name) or {}
        expected = spec.get("type")
        if not _matches_type(value, expected):
            raise ValueError(f"Argument '{name}' must be of type {expected}")

    return validated


def _matches_type(value: Any, expected: Any) -> bool:
    if expected is None:
        return True
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True
