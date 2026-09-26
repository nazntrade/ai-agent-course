"""Check a deployed MCP tool list against its checked-in server assembly.

Usage: python3 check_deploy_tools.py PATH_TO_SERVER_PY < tools_response.json
Only Python's standard library is required on the VPS.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path


def registered_tool_names(source: str) -> set[str]:
    """Extract direct server.tool()(tools.function) calls from MCPServer._build.

    Fail closed if registration is refactored: the deploy check must be updated
    when there is no longer a statically verifiable list in the assembly.
    """
    module = ast.parse(source)
    classes = [node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "MCPServer"]
    if len(classes) != 1:
        raise ValueError("MCPServer class missing or ambiguous")
    builders = [node for node in classes[0].body if isinstance(node, ast.FunctionDef) and node.name == "_build"]
    if len(builders) != 1:
        raise ValueError("MCPServer._build missing or ambiguous")

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


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("expected exactly one server.py path")
        source = Path(sys.argv[1]).read_text(encoding="utf-8")
        response = json.load(sys.stdin)
        if not tools_match(source, response):
            raise ValueError("MCP disconnected or tool list does not match the checked-in server")
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        print(f"MCP deploy check failed: {exc}", file=sys.stderr)
        sys.exit(1)
