"""Check a deployed MCP tool list against its checked-in server assembly.

Two modes, both reading the response JSON from stdin:

* ``check_deploy_tools.py PATH_TO_SERVER_PY`` checks one ``/api/mcp/tools``
  response against server A (legacy single-server gate).
* ``check_deploy_tools.py A_SERVER_PY B_SERVER_PY`` checks an
  ``/api/mcp/servers`` response: label ``A`` against server A's source and label
  ``B`` against server B's source, comparing both ``connected`` and the
  additive ``tool_names`` field.

Only Python's standard library is required on the VPS.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path


def registered_tool_names(source: str) -> set[str]:
    """Extract direct server.tool()(tools.function) calls from a server._build.

    The assembly class is located by its single ``_build`` method, so both
    server A (``MCPServer``) and server B (``NotifierServer``) are checked with
    the same parser. Fail closed if registration is refactored: the deploy check
    must be updated when there is no longer a statically verifiable list in the
    assembly.
    """
    module = ast.parse(source)
    builders = [
        method
        for node in module.body
        if isinstance(node, ast.ClassDef)
        for method in node.body
        if isinstance(method, ast.FunctionDef) and method.name == "_build"
    ]
    if len(builders) != 1:
        raise ValueError("MCP server assembly class missing or ambiguous")

    statements = builders[0].body
    if len(statements) < 3 or not isinstance(statements[0], ast.Assign):
        raise ValueError("unexpected MCP server assembly")
    first = statements[0]
    if len(first.targets) != 1 or not isinstance(first.targets[0], ast.Name) or first.targets[0].id != "server":
        raise ValueError("unexpected MCP server assignment")
    if not isinstance(first.value, ast.Call) or not isinstance(first.value.func, ast.Name) or first.value.func.id != "SdkMCPServer":
        raise ValueError("unexpected MCP server constructor")
    if not isinstance(statements[-1], ast.Return) or not isinstance(statements[-1].value, ast.Name) or statements[-1].value.id != "server":
        raise ValueError("unexpected MCP server return")

    names: list[str] = []
    for statement in statements[1:-1]:
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            raise ValueError("unrecognized MCP tool registration")
        call = statement.value
        if (
            not isinstance(call.func, ast.Call)
            or not isinstance(call.func.func, ast.Attribute)
            or not isinstance(call.func.func.value, ast.Name)
            or call.func.func.value.id != "server"
            or call.func.func.attr != "tool"
            or call.func.args
            or call.func.keywords
            or len(call.args) != 1
            or call.keywords
            or not isinstance(call.args[0], ast.Attribute)
            or not isinstance(call.args[0].value, ast.Name)
            or call.args[0].value.id != "tools"
        ):
            raise ValueError("unrecognized MCP tool registration")
        names.append(call.args[0].attr)
    if not names or len(names) != len(set(names)):
        raise ValueError("empty or duplicate MCP registrations")
    return set(names)


def tools_match(source: str, response: object) -> bool:
    expected = registered_tool_names(source)
    if not isinstance(response, dict) or response.get("connected") is not True:
        return False
    tools = response.get("tools")
    if not isinstance(tools, list) or response.get("count") != len(tools):
        return False
    names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
    return (
        len(names) == len(tools)
        and all(isinstance(name, str) for name in names)
        and len(set(names)) == len(names)
        and set(names) == expected
    )


def servers_match(response: object, sources: dict) -> bool:
    """Check an ``/api/mcp/servers`` response against ``{label: source_text}``.

    Every expected label must be present exactly once, ``connected`` must be
    true and its additive ``tool_names`` list must equal the names registered in
    that server's checked-in source (order-independent, no duplicates).
    """
    if not isinstance(response, dict) or not isinstance(sources, dict):
        return False
    servers = response.get("servers")
    if not isinstance(servers, list):
        return False
    by_label: dict = {}
    for entry in servers:
        if not isinstance(entry, dict):
            return False
        label = entry.get("label")
        if not isinstance(label, str) or label in by_label:
            return False
        by_label[label] = entry
    for label, source in sources.items():
        entry = by_label.get(label)
        if entry is None or entry.get("connected") is not True:
            return False
        expected = registered_tool_names(source)
        names = entry.get("tool_names")
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            return False
        if len(names) != len(set(names)) or set(names) != expected:
            return False
    return True


if __name__ == "__main__":
    try:
        arguments = sys.argv[1:]
        if len(arguments) == 1:
            source = Path(arguments[0]).read_text(encoding="utf-8")
            response = json.load(sys.stdin)
            if not tools_match(source, response):
                raise ValueError(
                    "MCP disconnected or tool list does not match the checked-in server"
                )
        elif len(arguments) == 2:
            sources = {
                "A": Path(arguments[0]).read_text(encoding="utf-8"),
                "B": Path(arguments[1]).read_text(encoding="utf-8"),
            }
            response = json.load(sys.stdin)
            if not servers_match(response, sources):
                raise ValueError(
                    "MCP servers disconnected or their tool names do not match "
                    "the checked-in sources"
                )
        else:
            raise ValueError("expected one server.py path or two (A and B)")
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        print(f"MCP deploy check failed: {exc}", file=sys.stderr)
        sys.exit(1)
